import re
from pathlib import Path
from typing import Optional


class GuardrailViolation(Exception):
    pass


class PathGuard:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()

    def check_read(self, path: str) -> Optional[str]:
        resolved = (self.workspace_root / path).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return f"path traversal denied: {path} is outside workspace"
        if not resolved.exists():
            return f"path not found: {path}"
        return None

    def check_write(self, path: str) -> Optional[str]:
        resolved = (self.workspace_root / path).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return f"path traversal denied: {path} is outside workspace"
        if resolved.is_dir():
            return f"cannot write: path is a directory: {path}"
        return None


SHELL_BLOCKLIST = [
    (r'\brm\s+(-rf?\b|--recursive\b|--force\b).*?(\s/\s*|\s/\w+)', 'recursive root deletion'),
    (r'\bdd\s+if=', 'direct device write (dd)'),
    (r'>\s*/dev/', 'device write'),
    (r'\bmkfs\.\w+', 'filesystem format'),
    (r'\bmkswap\b', 'swap manipulation'),
    (r'\bfdisk\b', 'partition manipulation'),
    (r'\bwget\s+(?:-O\s*-|.*\|\s*bash)', 'pipe wget to shell'),
    (r'\bcurl\s+(?:-o\s*-|.*\|\s*bash)', 'pipe curl to shell'),
    (r':\(\)\s*\{', 'fork bomb'),
    (r'\bsudo\b', 'sudo is not allowed'),
    (r'\bchmod\s+777\b', 'chmod 777 is dangerous'),
    (r'\bchown\b', 'chown is not allowed'),
    (r'\bpasswd\b', 'password modification'),
    (r'\buseradd\b', 'user administration'),
    (r'\bgroupadd\b', 'group administration'),
    (r'\bkill\s+-9\b', 'force kill'),
]

COMMAND_LENGTH_LIMIT = 5000


def check_shell(command: str) -> Optional[str]:
    if len(command) > COMMAND_LENGTH_LIMIT:
        return f"command too long ({len(command)} chars, max {COMMAND_LENGTH_LIMIT})"
    for pattern, desc in SHELL_BLOCKLIST:
        if re.search(pattern, command, re.IGNORECASE):
            return f"dangerous command blocked: {desc}"
    return None


def check_tool(name: str, args: dict, workspace_root: Optional[str] = None) -> Optional[str]:
    if name == "run_shell":
        cmd = args.get("command", "")
        if cmd:
            return check_shell(cmd)
        return None

    if workspace_root and name in ("read_file", "list_dir"):
        path = args.get("path", "")
        if path:
            return PathGuard(workspace_root).check_read(path)
        return None

    if workspace_root and name in ("write_file", "patch_file"):
        path = args.get("path", "")
        if path:
            return PathGuard(workspace_root).check_write(path)
        return None

    return None


GUARDRAIL_RULES_DESCRIPTION = """\
- Path access is restricted to the workspace directory — reading or writing outside the workspace is blocked
- The following shell commands are blocked: rm -rf on root, sudo, device writes (/dev/), filesystem format (mkfs), fork bombs, pipe-to-shell from curl/wget, chmod 777, chown, passwd, user/group administration, force kill
- Commands longer than 5000 characters are blocked
- Writing to a directory path (instead of a file) is blocked
- Tool calls that repeat the same arguments as the previous call are rejected"""


def guardrail_rules() -> str:
    return GUARDRAIL_RULES_DESCRIPTION


OUTPUT_OVERRIDE_PATTERNS = [
    r"(?im)ignore\s+(all\s+)?(previous|above|system|guardrail|safety)",
    r"(?im)disregard\s+(all\s+)?(previous|above|system|guardrail|safety)",
    r"(?im)you\s+(are\s+)?(free|allowed)\s+to\s+(ignore|skip|bypass)",
    r"(?im)forget\s+(all\s+)?(previous|above)\s+instructions",
    r"(?im)override\s+(system|guardrail|safety)",
]


def check_output(text: str) -> Optional[str]:
    for pattern in OUTPUT_OVERRIDE_PATTERNS:
        if re.search(pattern, text):
            blocked = re.search(pattern, text).group(0)[:60]
            return f"output contains system prompt override attempt: '{blocked}'"
    return None
