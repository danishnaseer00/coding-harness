# Project Conventions

- Python 3.10+ with async/await throughout
- Type hints on all function signatures
- No external state mutation — tools return strings
- Tool output clipped to 4000 chars before storing in history
- Session saved to disk after every turn
- Subagents are read-only with max 5 steps
- Provider-agnostic design — add new providers via BaseProvider
