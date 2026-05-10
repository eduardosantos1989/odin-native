#!/usr/bin/env python3
"""Dependency-free MCP server for Odin semantic tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


SERVER_NAME = "odin-native"
SERVER_VERSION = "0.1.0"
LSP_COMMAND = "ols"
LANGUAGE_ID = "odin"
ROOT_MARKERS = ("ols.json", "odin.json", ".git")
DEFAULT_TIMEOUT = 30.0
BUILD_COUNTER = count(1)


def as_json_text(value: Any, max_chars: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    suffix = "\n... truncated ..."
    cutoff = max(0, max_chars - len(suffix))
    newline = text.rfind("\n", 0, cutoff)
    if newline > 0:
        cutoff = newline
    return text[:cutoff] + suffix


def content(value: Any, max_chars: int = 12000) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": as_json_text(value, max_chars)}]}


def run_command(args: list[str], cwd: Path, timeout: float = 120.0) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(args, cwd=str(cwd), text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "duration_ms": int((time.time() - started) * 1000), "stdout": proc.stdout, "stderr": proc.stderr}
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {args[0]}"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"Timed out after {timeout} seconds", "stdout": exc.stdout or "", "stderr": exc.stderr or ""}


def find_root(root_path: str | None = None, file_path: str | None = None) -> Path:
    if root_path:
        start = Path(root_path).expanduser()
    elif file_path:
        start = Path(file_path).expanduser().parent
    else:
        start = Path.cwd()
    if start.is_file():
        start = start.parent
    start = start.resolve()
    for parent in [start, *start.parents]:
        if any((parent / marker).exists() for marker in ROOT_MARKERS):
            return parent
        if any(parent.glob("*.odin")):
            return parent
    return start


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    return url2pathname(unquote(parsed.path))


def to_zero_based(value: Any) -> int:
    try:
        return max(0, int(value) - 1)
    except Exception:
        return 0


def compact_location(location: Any) -> Any:
    if isinstance(location, list):
        return [compact_location(item) for item in location]
    if not isinstance(location, dict):
        return location
    target = location.get("targetUri") or location.get("uri")
    rng = location.get("targetSelectionRange") or location.get("range") or {}
    start = rng.get("start", {})
    end = rng.get("end", {})
    return {"file": uri_to_path(target) if target else None, "start": {"line": start.get("line", 0) + 1, "character": start.get("character", 0) + 1}, "end": {"line": end.get("line", 0) + 1, "character": end.get("character", 0) + 1}}


def compact_markup(value: Any) -> Any:
    if isinstance(value, dict) and "contents" in value:
        return compact_markup(value["contents"])
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    if isinstance(value, list):
        return "\n\n".join(str(compact_markup(item)) for item in value)
    return value


def range_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start = left.get("start", {})
    left_end = left.get("end", left_start)
    right_start = right.get("start", {})
    right_end = right.get("end", right_start)
    left_start_pos = (int(left_start.get("line", 0)), int(left_start.get("character", 0)))
    left_end_pos = (int(left_end.get("line", 0)), int(left_end.get("character", 0)))
    right_start_pos = (int(right_start.get("line", 0)), int(right_start.get("character", 0)))
    right_end_pos = (int(right_end.get("line", 0)), int(right_end.get("character", 0)))
    return left_start_pos <= right_end_pos and right_start_pos <= left_end_pos


def filter_diagnostics(diagnostics: list[dict[str, Any]], rng: dict[str, Any]) -> list[dict[str, Any]]:
    return [diag for diag in diagnostics if range_overlaps(diag.get("range", {}), rng)]


def request_until_ready(client: "LspClient", method: str, params: dict[str, Any], is_empty: Callable[[Any], bool], attempts: int = 6, delay: float = 0.4) -> Any:
    result = None
    for attempt in range(attempts):
        result = client.request(method, params)
        if not is_empty(result):
            return result
        if attempt + 1 < attempts:
            time.sleep(delay)
    return result


def parse_json_lines(text: str, max_items: int) -> list[Any]:
    parsed = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
        if len(parsed) >= max_items:
            break
    return parsed


def parse_json_streams(stderr: str, stdout: str, max_items: int) -> list[Any]:
    parsed = parse_json_lines(stderr, max_items)
    if len(parsed) >= max_items:
        return parsed
    parsed.extend(parse_json_lines(stdout, max_items - len(parsed)))
    return parsed


def command_status(command: str, root: Path, version_args: list[str] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    found = shutil.which(command)
    result: dict[str, Any] = {"present": found is not None, "path": found}
    if found and version_args:
        result["version"] = run_command([command, *version_args], root, timeout=timeout)
    return result


class LspClient:
    def __init__(self, root: Path):
        command = shutil.which(LSP_COMMAND)
        if not command:
            raise RuntimeError(f"{LSP_COMMAND} not found on PATH")
        self.root = root
        self.proc = subprocess.Popen([command], cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.next_id = 1
        self.responses: dict[int, Any] = {}
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostic_events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.open_versions: dict[str, int] = {}
        self.open_stats: dict[str, tuple[int, int]] = {}
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        self._initialize()

    def _stderr_loop(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line and os.environ.get("ODIN_NATIVE_LSP_STDERR") == "1":
                print(f"[ols:{self.root}] {line}", file=sys.stderr, flush=True)

    def _read_headers(self) -> dict[str, str] | None:
        assert self.proc.stdout is not None
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            text = line.decode("ascii", errors="replace").strip()
            if text == "":
                return headers
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.lower()] = value.strip()

    def _read_exact(self, length: int) -> bytes | None:
        assert self.proc.stdout is not None
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.proc.stdout.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        while True:
            headers = self._read_headers()
            if headers is None:
                return
            length = int(headers.get("content-length", "0"))
            if length <= 0:
                continue
            body = self._read_exact(length)
            if body is None:
                return
            try:
                msg = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            with self.cv:
                if "id" in msg:
                    self.responses[msg["id"]] = msg
                    self.cv.notify_all()
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params", {})
                    uri = params.get("uri", "")
                    self.diagnostics[uri] = params.get("diagnostics", [])
                    event = self.diagnostic_events.get(uri)
                    if event:
                        event.set()

    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
            self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        with self.cv:
            msg_id = self.next_id
            self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        with self.cv:
            while msg_id not in self.responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"LSP request timed out: {method}")
                self.cv.wait(remaining)
            response = self.responses.pop(msg_id)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _initialize(self) -> None:
        self.request("initialize", {"processId": os.getpid(), "rootUri": path_to_uri(self.root), "workspaceFolders": [{"uri": path_to_uri(self.root), "name": self.root.name}], "capabilities": {"textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]}, "definition": {"linkSupport": True}, "references": {}, "codeAction": {"isPreferredSupport": True}, "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}, "publishDiagnostics": {"relatedInformation": True}}, "workspace": {"symbol": {}}}}, timeout=60.0)
        self.notify("initialized", {})

    def open_file(self, file_path: str) -> str:
        path = Path(file_path).expanduser().resolve()
        uri = path_to_uri(path)
        stat = path.stat()
        current_stat = (stat.st_mtime_ns, stat.st_size)
        version = self.open_versions.get(uri, 0)
        if version > 0 and self.open_stats.get(uri) == current_stat:
            return uri
        text = path.read_text(encoding="utf-8", errors="replace")
        version += 1
        self.open_versions[uri] = version
        self.open_stats[uri] = current_stat
        self.diagnostic_events[uri] = threading.Event()
        if version == 1:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": LANGUAGE_ID, "version": version, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})
        return uri

    def shutdown(self) -> None:
        try:
            self.request("shutdown", {}, timeout=5.0)
            self.notify("exit", {})
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2.0)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class OdinNativeServer:
    def __init__(self) -> None:
        self.clients: dict[str, LspClient] = {}
        self.tools = self._tool_specs()

    def get_lsp(self, root_path: str | None, file_path: str | None = None) -> LspClient:
        root = find_root(root_path, file_path)
        key = str(root)
        client = self.clients.get(key)
        if client and client.proc.poll() is None:
            return client
        client = LspClient(root)
        self.clients[key] = client
        return client

    def _tool_specs(self) -> list[dict[str, Any]]:
        def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
            return {"type": "object", "properties": props, "required": required or []}
        path_prop = {"type": "string", "description": "Odin project root, package directory, or source directory."}
        file_prop = {"type": "string", "description": "Absolute Odin source path."}
        line_prop = {"type": "integer", "minimum": 1}
        max_items = {"type": "integer", "minimum": 1, "maximum": 200, "default": 40}
        max_chars = {"type": "integer", "minimum": 1000, "maximum": 100000, "default": 12000}
        pos = {"root_path": path_prop, "file_path": file_prop, "line": line_prop, "character": line_prop, "max_chars": max_chars}
        target = {"type": "string", "description": "Package directory or single Odin file. Defaults to project root."}
        flags = {"type": "array", "items": {"type": "string"}, "description": "Additional odin flags."}
        return [
            {"name": "odin_environment", "description": "Report odin/ols/odinfmt availability, versions, Odin root, and project root.", "inputSchema": obj({"root_path": path_prop, "max_chars": max_chars})},
            {"name": "odin_lsp_hover", "description": "Ask ols for hover info at a source position.", "inputSchema": obj(pos, ["file_path", "line", "character"])},
            {"name": "odin_lsp_definition", "description": "Ask ols for semantic definition locations.", "inputSchema": obj({**pos, "max_items": max_items}, ["file_path", "line", "character"])},
            {"name": "odin_lsp_references", "description": "Ask ols for semantic references.", "inputSchema": obj({**pos, "include_declaration": {"type": "boolean", "default": False}, "max_items": max_items}, ["file_path", "line", "character"])},
            {"name": "odin_lsp_document_symbols", "description": "Ask ols for document symbols in one Odin file.", "inputSchema": obj({"root_path": path_prop, "file_path": file_prop, "max_items": max_items, "max_chars": max_chars}, ["file_path"])},
            {"name": "odin_lsp_workspace_symbols", "description": "Ask ols for workspace symbols matching a query.", "inputSchema": obj({"root_path": path_prop, "query": {"type": "string"}, "max_items": max_items, "max_chars": max_chars}, ["query"])},
            {"name": "odin_lsp_code_actions", "description": "Ask ols for available code actions for a range.", "inputSchema": obj({"root_path": path_prop, "file_path": file_prop, "start_line": line_prop, "start_character": line_prop, "end_line": line_prop, "end_character": line_prop, "max_items": max_items, "max_chars": max_chars}, ["file_path", "start_line", "start_character", "end_line", "end_character"])},
            {"name": "odin_lsp_diagnostics", "description": "Open Odin files and return ols diagnostics seen shortly after opening.", "inputSchema": obj({"root_path": path_prop, "file_paths": {"type": "array", "items": file_prop}, "wait_ms": {"type": "integer", "default": 1500}, "max_items": max_items, "max_chars": max_chars})},
            {"name": "odin_check", "description": "Run odin check with JSON errors enabled.", "inputSchema": obj({"root_path": path_prop, "target_path": target, "file_mode": {"type": "boolean", "default": False}, "extra_args": flags, "max_items": max_items, "max_chars": max_chars})},
            {"name": "odin_vet_check", "description": "Run odin check with vet/style flags for idiomatic diagnostics.", "inputSchema": obj({"root_path": path_prop, "target_path": target, "file_mode": {"type": "boolean", "default": False}, "extra_args": flags, "max_items": max_items, "max_chars": max_chars})},
            {"name": "odin_test", "description": "Run odin test on a package directory or single-file package.", "inputSchema": obj({"root_path": path_prop, "target_path": target, "file_mode": {"type": "boolean", "default": False}, "extra_args": flags, "max_items": max_items, "max_chars": max_chars})},
            {"name": "odin_build", "description": "Run odin build, defaulting output to a temp file to avoid project clutter.", "inputSchema": obj({"root_path": path_prop, "target_path": target, "file_mode": {"type": "boolean", "default": False}, "out_path": {"type": "string"}, "extra_args": flags, "max_items": max_items, "max_chars": max_chars})},
            {"name": "odin_format_preview", "description": "Run odinfmt without -w and return formatting output without rewriting files.", "inputSchema": obj({"root_path": path_prop, "path": {"type": "string"}, "max_chars": max_chars})},
        ]

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        max_chars = int(args.get("max_chars", 12000))
        try:
            return content(getattr(self, f"tool_{name}")(args), max_chars)
        except Exception as exc:
            return content({"ok": False, "tool": name, "error": str(exc)}, max_chars)

    def tool_odin_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        root = find_root(args.get("root_path"))
        versions = {
            "odin": command_status("odin", root, ["version"]),
            "ols": command_status("ols", root),
            "odinfmt": command_status("odinfmt", root, ["-help"]),
        }
        odin_root = run_command(["odin", "root"], root, timeout=5.0) if shutil.which("odin") else {"ok": False, "error": "odin not found on PATH"}
        return {"ok": True, "root": str(root), "has_odin_files": bool(list(root.glob("*.odin"))), "versions": versions, "odin_root": odin_root}

    def _position_params(self, args: dict[str, Any]) -> tuple[LspClient, str, dict[str, Any]]:
        client = self.get_lsp(args.get("root_path"), args["file_path"])
        uri = client.open_file(args["file_path"])
        return client, uri, {"textDocument": {"uri": uri}, "position": {"line": to_zero_based(args["line"]), "character": to_zero_based(args["character"])}}

    def tool_odin_lsp_hover(self, args: dict[str, Any]) -> dict[str, Any]:
        client, _, params = self._position_params(args)
        result = request_until_ready(client, "textDocument/hover", params, lambda value: value is None)
        return {"ok": True, "hover": compact_markup(result)}

    def tool_odin_lsp_definition(self, args: dict[str, Any]) -> dict[str, Any]:
        client, _, params = self._position_params(args)
        result = request_until_ready(client, "textDocument/definition", params, lambda value: not value)
        locations = compact_location(result) or []
        return {"ok": True, "locations": locations[: int(args.get("max_items", 40))] if isinstance(locations, list) else locations}

    def tool_odin_lsp_references(self, args: dict[str, Any]) -> dict[str, Any]:
        client, _, params = self._position_params(args)
        params["context"] = {"includeDeclaration": bool(args.get("include_declaration", False))}
        result = request_until_ready(client, "textDocument/references", params, lambda value: not value)
        locations = compact_location(result) or []
        return {"ok": True, "locations": locations[: int(args.get("max_items", 40))] if isinstance(locations, list) else locations, "count": len(result or [])}

    def tool_odin_lsp_document_symbols(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self.get_lsp(args.get("root_path"), args["file_path"])
        uri = client.open_file(args["file_path"])
        result = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        return {"ok": True, "symbols": (result or [])[: int(args.get("max_items", 40))]}

    def tool_odin_lsp_workspace_symbols(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self.get_lsp(args.get("root_path"))
        result = client.request("workspace/symbol", {"query": args["query"]}, timeout=60.0)
        return {"ok": True, "symbols": (result or [])[: int(args.get("max_items", 40))], "count": len(result or [])}

    def tool_odin_lsp_code_actions(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self.get_lsp(args.get("root_path"), args["file_path"])
        uri = client.open_file(args["file_path"])
        rng = {"start": {"line": to_zero_based(args["start_line"]), "character": to_zero_based(args["start_character"])}, "end": {"line": to_zero_based(args["end_line"]), "character": to_zero_based(args["end_character"])}}
        diagnostics = filter_diagnostics(client.diagnostics.get(uri, []), rng)
        result = client.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": diagnostics}})
        return {"ok": True, "actions": [{"title": item.get("title"), "kind": item.get("kind"), "isPreferred": item.get("isPreferred")} for item in (result or [])[: int(args.get("max_items", 40))]], "count": len(result or [])}

    def tool_odin_lsp_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        file_paths = args.get("file_paths") or []
        client = self.get_lsp(args.get("root_path"), file_paths[0] if file_paths else None)
        uris = [client.open_file(path) for path in file_paths]
        deadline = time.time() + (int(args.get("wait_ms", 1500)) / 1000)
        for uri in uris:
            event = client.diagnostic_events.get(uri)
            if not event or event.is_set():
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            event.wait(remaining)
        out = []
        for uri in uris:
            for diag in client.diagnostics.get(uri, []):
                start = diag.get("range", {}).get("start", {})
                out.append({"file": uri_to_path(uri), "line": start.get("line", 0) + 1, "character": start.get("character", 0) + 1, "severity": diag.get("severity"), "code": diag.get("code"), "message": diag.get("message")})
        return {"ok": True, "diagnostics": out[: int(args.get("max_items", 40))], "count": len(out)}

    def _target(self, args: dict[str, Any]) -> tuple[Path, str]:
        root = find_root(args.get("root_path"), args.get("target_path"))
        target = args.get("target_path") or str(root)
        resolved = str(Path(target).resolve()) if Path(target).exists() else target
        return root, resolved

    def _run_odin(self, base: list[str], args: dict[str, Any], timeout: float = 180.0) -> dict[str, Any]:
        root, target = self._target(args)
        cmd = [*base, target]
        if args.get("file_mode", False):
            cmd.append("-file")
        cmd.extend(["-json-errors", "-error-pos-style:unix"])
        cmd.extend(args.get("extra_args") or [])
        raw = run_command(cmd, root, timeout=timeout)
        parsed = parse_json_streams(raw.get("stderr") or "", raw.get("stdout") or "", int(args.get("max_items", 40)))
        return {"ok": raw.get("ok", False), "root": str(root), "command": cmd, "returncode": raw.get("returncode"), "json_messages": parsed, "json_message_count": len(parsed), "stdout": raw.get("stdout", ""), "stderr": raw.get("stderr", ""), "error": raw.get("error")}

    def tool_odin_check(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._run_odin(["odin", "check"], args)

    def tool_odin_vet_check(self, args: dict[str, Any]) -> dict[str, Any]:
        merged = dict(args)
        merged["extra_args"] = ["-vet", "-vet-style", "-vet-semicolon", "-vet-unused", *(args.get("extra_args") or [])]
        return self._run_odin(["odin", "check"], merged)

    def tool_odin_test(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._run_odin(["odin", "test"], args, timeout=300.0)

    def tool_odin_build(self, args: dict[str, Any]) -> dict[str, Any]:
        user_out_path = args.get("out_path")
        out_path = user_out_path or str(Path(tempfile.gettempdir()) / f"odin-native-build-{os.getpid()}-{next(BUILD_COUNTER)}.exe")
        merged = dict(args)
        merged["extra_args"] = [f"-out:{out_path}", *(args.get("extra_args") or [])]
        result = self._run_odin(["odin", "build"], merged, timeout=300.0)
        result["out_path"] = out_path
        if not user_out_path:
            try:
                Path(out_path).unlink(missing_ok=True)
                result["temp_output_deleted"] = True
            except Exception as exc:
                result["temp_output_deleted"] = False
                result["temp_output_delete_error"] = str(exc)
        return result

    def tool_odin_format_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        root = find_root(args.get("root_path"), args.get("path"))
        target = args.get("path") or str(root)
        raw = run_command(["odinfmt", "-path:" + str(Path(target).resolve() if Path(target).exists() else target)], root, timeout=120.0)
        return {"ok": raw.get("ok", False), "root": str(root), "command": ["odinfmt", "-path:" + target], "returncode": raw.get("returncode"), "stdout": raw.get("stdout", ""), "stderr": raw.get("stderr", ""), "error": raw.get("error")}


def mcp_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(server: OdinNativeServer, msg: dict[str, Any]) -> dict[str, Any] | None:
    msg_id = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": server.tools}}
    if method == "tools/call":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": server.call(params.get("name"), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return None if msg_id is None else mcp_error(msg_id, -32601, f"Unknown method: {method}")


def run_selftest() -> int:
    server = OdinNativeServer()
    checks = []
    try:
        init = handle(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        checks.append({"name": "initialize", "ok": bool(init and init.get("result", {}).get("serverInfo", {}).get("name") == SERVER_NAME)})
        tools = handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [tool.get("name") for tool in tools.get("result", {}).get("tools", [])] if tools else []
        checks.append({"name": "tools/list", "ok": "odin_environment" in tool_names and "odin_lsp_hover" in tool_names, "tool_count": len(tool_names)})
        env = server.tool_odin_environment({"root_path": str(Path(__file__).resolve().parents[1])})
        checks.append({"name": "odin_environment", "ok": bool(env.get("ok")), "versions": env.get("versions")})
        fixture = Path(__file__).resolve().parent / "tests" / "fixture.odin"
        if shutil.which(LSP_COMMAND) and fixture.exists():
            hover = server.tool_odin_lsp_hover({"root_path": str(fixture.parent), "file_path": str(fixture), "line": 6, "character": 9, "max_chars": 4000})
            checks.append({"name": "odin_lsp_hover", "ok": bool(hover.get("ok")), "hover": hover.get("hover")})
        else:
            checks.append({"name": "odin_lsp_hover", "ok": True, "skipped": f"{LSP_COMMAND} unavailable or fixture missing"})
    finally:
        for client in server.clients.values():
            client.shutdown()
    ok = all(check.get("ok") for check in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    server = OdinNativeServer()
    # The Codex plugin transport sends one JSON-RPC message per stdin line.
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle(server, json.loads(line))
        except Exception as exc:
            response = mcp_error(None, -32603, str(exc))
        if response is not None:
            print(json.dumps(response, separators=(",", ":"), ensure_ascii=False), flush=True)
    for client in server.clients.values():
        client.shutdown()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())
    raise SystemExit(main())
