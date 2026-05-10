# Odin Native

Semantic Odin tools for Codex, backed by `ols`, `odin`, and `odinfmt`.

This plugin exposes a local MCP server that gives Codex token-efficient Odin code intelligence without relying on text search alone. It starts `ols` per project root for semantic LSP queries and shells out to Odin command-line tools for checks, tests, builds, vet diagnostics, and formatting previews.

## Features

- `ols` hover, definitions, references, document symbols, workspace symbols, diagnostics, and code actions.
- `odin check` with JSON errors.
- Vet-style Odin checks.
- `odin test` and `odin build`.
- Non-mutating `odinfmt` preview.
- Lightweight `--selftest` fixture for smoke testing the MCP server.

## Requirements

- Python 3.11 or newer.
- `odin` on `PATH`.
- `ols` on `PATH`.
- `odinfmt` on `PATH`.

## Installation

Place this directory at:

```text
%USERPROFILE%\.codex\plugins\odin-native
```

The plugin metadata is in `.codex-plugin/plugin.json`, and the MCP server command is in `.mcp.json`.

## Smoke Test

Run from the plugin root:

```powershell
python scripts\odin_native_mcp.py --selftest
```

Expected result: JSON output with `"ok": true`.

## Tooling Notes

- LSP positions use 1-based `line` and 1-based `character` inputs.
- Call `odin_lsp_diagnostics` before `odin_lsp_code_actions` when asking for quick fixes on a problematic range.
- Use `odin_format_preview` for formatting checks; it does not rewrite files.

## License

MIT.
