# AGENTS.md

This file tells the agent about your project. Copy it into your project root and fill in the sections below. The agent reads it at every session start.

## Project

A one-line description of what your project does.

```
Example: "FastAPI backend for a task management app"
```

## Tech Stack

List exact versions and frameworks so the agent writes correct code.

| Category | Example |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI 0.115 |
| Database | PostgreSQL 16 + SQLAlchemy 2.0 |
| Testing | pytest 8.x, pytest-asyncio |
| CI / CD | GitHub Actions |

## Repository Structure

Describe your directory layout so the agent knows where to find things.

```
Example:
src/          — application code
tests/        — test files
migrations/   — Alembic migrations
docker/       — Docker configs
```

## Code Conventions

Rules that prevent bugs and review comments.

- **Style** — Follow PEP 8. Line length 100.
- **Naming** — `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_CASE` for constants.
- **Imports** — Standard library, third-party, local. One import per line.
- **Error Handling** — Use custom exception classes. Always log errors.
- **Configuration** — Settings via Pydantic `BaseSettings`, loaded from `.env`.

## Commands

| Action | Command |
|---|---|
| Run | `uvicorn app.main:app --reload` |
| Test (all) | `pytest` |
| Test (single) | `pytest tests/test_file.py::test_name -v` |
| Lint | `ruff check .` |
| Typecheck | `mypy src/` |
| Format | `ruff format .` |

## Boundaries

Files, directories, and operations the agent must never touch.

- `migrations/` — auto-generated, modify via Alembic only
- `secrets/` — contains credentials
- `vendor/` — third-party code, not ours

## Common Patterns

Code snippets the agent should follow for repetitive tasks.

### Adding a new API endpoint

Create a new router file in `src/routers/`, register it in `app.py`, add Pydantic schemas, and write tests in `tests/test_routers/`.

### Adding a new test

Create a file in `tests/` mirroring the source path. Use pytest fixtures from `conftest.py`. Name test functions `test_<feature>_<scenario>`.
