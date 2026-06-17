import asyncio
from pathlib import Path


TOOLS = [
    {
        "name": "list_dir",
        "description": "List files and directories in a path. Use to explore the workspace structure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file. Optionally specify line range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "start_line": {"type": "integer", "description": "First line (1-indexed)"},
                "end_line": {"type": "integer", "description": "Last line (exclusive)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "search",
        "description": "Search for a pattern in files using regex. Supports glob filtering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "include": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
                "path": {"type": "string", "description": "Directory to search in (default: current)"}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "write_file",
        "description": "Create a new file or overwrite an existing file with content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Full file content"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "patch_file",
        "description": "Edit part of an existing file by replacing a specific string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_string": {"type": "string", "description": "Text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"}
            },
            "required": ["path", "old_string", "new_string"]
        }
    },
    {
        "name": "run_shell",
        "description": "Run a shell command. Use for tests, installs, git, builds, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30}
            },
            "required": ["command"]
        }
    },
    {
        "name": "note",
        "description": "Save an important observation to memory. Use for key findings you will need later in the conversation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The thing to remember"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "delegate",
        "description": "Spawn a read-only sub-agent for a focused subtask. Runs in parallel with other delegates. The sub-agent can explore, read, and search but cannot write files or run commands. Use for independent research, subtask investigation, or parallel exploration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The specific subtask to complete"},
                "context": {"type": "string", "description": "Background information or context the sub-agent needs"}
            },
            "required": ["task"]
        }
    }
]

RISKY_TOOLS = {"run_shell", "write_file", "patch_file"}


def validate_tool(name: str, args: dict) -> str | None:
    if name == "read_file":
        path = args.get("path", "")
        if not path:
            return "path is required"
        if not Path(path).exists():
            return f"file not found: {path}"
    if name == "write_file":
        if not args.get("path"):
            return "path is required"
        if not args.get("content"):
            return "content is required"
    if name == "patch_file":
        if not args.get("path"):
            return "path is required"
        if not args.get("old_string"):
            return "old_string is required"
        if not Path(args["path"]).exists():
            return f"file not found: {args['path']}"
    if name == "run_shell":
        if not args.get("command"):
            return "command is required"
    if name == "list_dir":
        path = args.get("path", ".")
        if not Path(path).exists():
            return f"directory not found: {path}"
    if name == "search":
        if not args.get("pattern"):
            return "pattern is required"
    return None


async def approve(name: str, args: dict, policy: str = "ask") -> bool:
    if name not in RISKY_TOOLS:
        return True
    if policy == "auto":
        return True
    if policy == "never":
        return False
    print(f"\n⚠️  Agent wants to run: {name}")
    print(f"   Args: {args}")
    answer = await asyncio.to_thread(lambda: input("   Allow? [y/N]: ").strip().lower())
    return answer == "y"


class RepeatDetector:
    def __init__(self):
        self.last_call = None

    def check(self, name: str, args: dict) -> bool:
        current = (name, str(sorted(args.items())))
        if current == self.last_call:
            return True
        self.last_call = current
        return False


async def tool_list_dir(path: str = ".") -> str:
    p = Path(path)
    if not p.exists():
        return f"error: directory not found: {path}"
    lines = []
    for entry in sorted(p.iterdir()):
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{entry.name}{suffix}")
    return "\n".join(lines) if lines else "(empty directory)"


async def tool_read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    p = Path(path)
    if not p.exists():
        return f"error: file not found: {path}"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return f"error reading file: {e}"
    if start_line or end_line:
        lines = lines[(start_line or 1) - 1 : end_line]
    return "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines)) if lines else "(empty file)"


async def tool_search(pattern: str, include: str = None, path: str = ".") -> str:
    cmd = ["rg", "-n"]
    if include:
        cmd.extend(["-g", include])
    cmd.extend([pattern, path])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        result = stdout.decode().strip()
        if not result:
            return "(no matches found)"
        lines = result.splitlines()
        return "\n".join(lines[:50]) + (f"\n... ({len(lines) - 50} more matches)" if len(lines) > 50 else "")
    except FileNotFoundError:
        return "error: ripgrep (rg) not found. Install it or use a different search approach."
    except asyncio.TimeoutError:
        return "error: search timed out"
    except Exception as e:
        return f"error: {e}"


async def tool_write_file(path: str, content: str) -> str:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {path} ({len(content)} chars)"
    except Exception as e:
        return f"error writing file: {e}"


async def tool_patch_file(path: str, old_string: str, new_string: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"error: file not found: {path}"
    try:
        content = p.read_text(encoding="utf-8")
        if old_string not in content:
            return f"error: old_string not found in {path}"
        new_content = content.replace(old_string, new_string, 1)
        p.write_text(new_content, encoding="utf-8")
        return f"patched {path}: replaced {len(old_string)} chars with {len(new_string)} chars"
    except Exception as e:
        return f"error patching file: {e}"


async def tool_run_shell(command: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        parts = [p for p in [out, err] if p]
        if proc.returncode != 0:
            parts.append(f"exit code: {proc.returncode}")
        return "\n".join(parts) if parts else "(no output)"
    except asyncio.TimeoutError:
        return f"error: command timed out after {timeout}s"
    except Exception as e:
        return f"error: {e}"


def tool_note(memory: dict, text: str) -> str:
    memory.setdefault("notes", []).insert(0, text)
    memory["notes"] = memory["notes"][:5]
    return "noted"


TOOL_HANDLERS = {
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "search": tool_search,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "run_shell": tool_run_shell,
}
