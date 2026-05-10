---
name: odin-native
description: Use when working in Odin repositories where semantic accuracy matters. Prefer ols LSP tools for hover, definitions, references, symbols, diagnostics, and code actions, and odin/odinfmt tools for check, build, test, vet, and formatting preview.
---

# Odin Native

Use this skill for non-trivial Odin code reading, editing, refactoring, debugging, and review.

## When To Use

- Use for Odin repositories, `ols` language-server lookups, and compiler-backed checks.
- Prefer this over text search when symbol identity, type information, or references matter.

## What It Provides

- `ols` hover, definition, references, document symbols, workspace symbols, diagnostics, and code actions.
- `odin` check, vet-style check, test, and build tools with JSON diagnostics.
- `odinfmt` preview without rewriting files.

## Workflow

1. Resolve the Odin package or project root. Prefer the nearest directory with `ols.json`, `odin.json`, `.git`, or Odin source files.
2. Call `odin_environment` once to confirm `odin`, `ols`, and `odinfmt` availability.
3. Use ols before guessing semantic details:
   - `odin_lsp_hover` for symbol/type information.
   - `odin_lsp_definition` before following a symbol by text search.
   - `odin_lsp_references` for semantic references.
   - `odin_lsp_document_symbols` and `odin_lsp_workspace_symbols` for compact navigation.
   - `odin_lsp_diagnostics` before `odin_lsp_code_actions` when asking for quick fixes on a problematic range.
   - `odin_lsp_code_actions` when fixes may be available.
4. Use Odin command checks before finalizing edits:
   - `odin_check` for parser and type-checking, with JSON errors enabled.
   - `odin_vet_check` for stricter style and vet checks.
   - `odin_test` for package tests.
   - `odin_format_preview` for non-mutating formatting output.

## Output Discipline

- Keep `max_items` and `max_chars` low until more detail is needed.
- Treat LSP and compiler output as evidence, not as a replacement for reading surrounding code.
- Do not run `odin strip-semicolon` or `odinfmt -w` unless the user explicitly asks for a mutating format pass.
- Use `wait_ms` around 1500 for cold `odin_lsp_diagnostics` calls; subsequent calls can usually use lower waits.

## Position Convention

The LSP tools accept 1-based `line` and 1-based `character` values. The MCP server converts them to zero-based LSP positions internally and clamps invalid values to zero.
