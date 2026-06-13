# AGENTS.md

## Project
Coding Harness — a Python async agent framework with streaming, multi-provider LLM support, and a prompt_toolkit/Rich TUI.

## Tech Stack
- Python 3.10+, async/await throughout
- prompt_toolkit for async REPL input with history
- Rich for terminal output (Panels, Tables, styled text)
- anthropic + openai SDKs for LLM providers

## Conventions
- Type hints on all function signatures
- No comments unless the code cannot be made self-explanatory
- Tools return strings (never mutate external state)

## Commands
- Run: `python my-coding-agent/cli.py`
- Lint: `ruff check my-coding-agent/`
- Typecheck: `mypy my-coding-agent/`

## Boundaries
- Never modify files outside `my-coding-agent/`
- Never add new dependencies without confirmation
