import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path


SESSION_DIR = Path.home() / ".coding-harness" / "sessions"
IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class WorkspaceContext:
    def __init__(self, cwd: str = "."):
        self.cwd = Path(cwd).resolve()

    def build(self) -> str:
        parts = []
        parts.append(self._directory_tree())
        parts.append(self._git_state())
        parts.append(self._project_docs())
        return "\n\n".join(filter(None, parts))

    def _directory_tree(self) -> str:
        lines = []
        try:
            paths = sorted(self.cwd.rglob("*"))
        except PermissionError:
            paths = []
        for p in paths:
            if any(part in IGNORE_DIRS for part in p.parts):
                continue
            rel = p.relative_to(self.cwd)
            prefix = "  " * (len(rel.parts) - 1)
            icon = "[DIR]" if p.is_dir() else "[FILE]"
            lines.append(f"{prefix}{icon} {p.name}")
        return f"Directory: {self.cwd}\n" + "\n".join(lines[:100])

    def _git_state(self) -> str:
        def git(args):
            try:
                out = subprocess.check_output(
                    ["git"] + args, cwd=self.cwd,
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
                return out
            except Exception:
                return ""
        branch = git(["branch", "--show-current"])
        status = git(["status", "--short"]) or "clean"
        commits = git(["log", "--oneline", "-5"])
        if not branch:
            return ""
        return f"Git branch: {branch}\nStatus: {status}\nRecent commits:\n{commits}"

    def _project_docs(self) -> str:
        docs = []
        for name in ["AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml", "SOUL.md"]:
            p = self.cwd / name
            if p.exists():
                try:
                    content = p.read_text()[:2000]
                    docs.append(f"--- {name} ---\n{content}")
                except Exception:
                    docs.append(f"--- {name} ---\n(unreadable)")
        return "\n\n".join(docs)


class SessionStore:
    def __init__(self, session_id: str | None = None):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.path = SESSION_DIR / f"{self.session_id}.json"
        self.data = {
            "id": self.session_id,
            "created": datetime.now().isoformat(),
            "memory": {"task": "", "files": [], "notes": []},
            "history": []
        }

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    def record(self, role: str, content: str, tool: str = None, args: dict = None):
        entry = {"role": role, "content": content}
        if tool:
            entry["tool"] = tool
            entry["args"] = args or {}
        self.data["history"].append(entry)
        self.save()

    @property
    def memory(self) -> dict:
        return self.data["memory"]

    @memory.setter
    def memory(self, value: dict):
        self.data["memory"] = value

    @classmethod
    def resume(cls, session_id: str = "latest") -> "SessionStore":
        if session_id == "latest":
            sessions = sorted(SESSION_DIR.glob("*.json"))
            if not sessions:
                raise FileNotFoundError("No sessions to resume")
            path = sessions[-1]
        else:
            path = SESSION_DIR / f"{session_id}.json"
        data = json.loads(path.read_text())
        store = cls(session_id=data["id"])
        store.data = data
        store.path = path
        return store


def update_memory(memory: dict, tool_name: str, args: dict, result: str):
    if tool_name in ("read_file", "write_file", "patch_file"):
        path = args.get("path", "")
        if path and path not in memory.get("files", []):
            memory.setdefault("files", []).insert(0, path)
            memory["files"] = memory["files"][:8]
    return memory
