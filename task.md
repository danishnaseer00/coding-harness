# SKILL: Build a Coding Agent Harness

## What This Is
A step-by-step guide to building a coding agent harness from scratch — like Sebastian Raschka's
mini-coding-agent — with all 6 core features. Read this fully before writing any code.

---

## File Structure (build in this order)

```
my-coding-agent/
├── agent.py          ← Step 3: main loop, API calls, tool dispatch
├── tools.py          ← Step 2: tool schemas + handlers
├── memory.py         ← Step 1: session store, persistent memory
├── context.py        ← Step 4: history compression, clipping
├── display.py        ← Step 5: terminal output, colors
├── cli.py            ← Step 6: REPL, slash commands, entry point
├── SOUL.md           ← agent identity and personality
├── AGENTS.md         ← project-level conventions (read at startup)
├── pyproject.toml    ← packaging + entry point
└── install.sh        ← one-command install
```

---

## Build Order

```
Step 1 → memory.py       (no dependencies, start here)
Step 2 → tools.py        (depends on memory)
Step 3 → agent.py        (depends on tools)
Step 4 → context.py      (plug into agent)
Step 5 → display.py      (plug into agent)
Step 6 → cli.py          (wraps everything)
Step 7 → packaging       (last, once working)
```

Do NOT skip ahead. Get each step working before moving to the next.

---

## Feature 1: Live Repo Context

### What it does
On startup, the agent reads the current workspace — directory tree, git state, project docs —
and injects it all into the system prompt. The model always knows where it is.

### What to read at startup

```python
import subprocess
from pathlib import Path

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
        # List files, skip noise
        IGNORE = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
        lines = []
        for p in sorted(self.cwd.rglob("*")):
            if any(part in IGNORE for part in p.parts):
                continue
            rel = p.relative_to(self.cwd)
            prefix = "  " * (len(rel.parts) - 1)
            lines.append(f"{prefix}{'📁' if p.is_dir() else '📄'} {p.name}")
        return f"Directory: {self.cwd}\n" + "\n".join(lines[:100])  # cap at 100

    def _git_state(self) -> str:
        def git(args):
            try:
                return subprocess.check_output(
                    ["git"] + args, cwd=self.cwd,
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
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
        for name in ["AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml"]:
            p = self.cwd / name
            if p.exists():
                content = p.read_text()[:2000]  # cap at 2000 chars
                docs.append(f"--- {name} ---\n{content}")
        return "\n\n".join(docs)
```

### How to inject it

```python
# In agent.py, build the system prompt once at startup
workspace = WorkspaceContext(cwd=args.cwd)
system_prompt = f"""
You are a coding agent. You help the user build and modify code.

{workspace.build()}

{SOUL_md_content}

Rules:
- Before writing tests for existing code, read the implementation first
- New files must be complete and runnable
- Do not repeat the same tool call with the same args if it didn't help
"""
```

### Key constraint
Cap the directory tree at 100 entries and project docs at 2000 chars each.
A huge codebase will overflow the context. Trim aggressively.

---

## Feature 2: Prompt Shape and Cache Reuse

### What it does
Splits the prompt into two parts: a stable prefix (never changes) and a dynamic suffix
(changes every turn). The stable prefix can be reused across turns without paying
re-processing cost.

### The Split

```
STABLE PREFIX (build once, inject into every call)
────────────────────────────────────────────────────
  - System prompt (rules, identity, tool descriptions)
  - Workspace context (repo tree, git state, project docs)

DYNAMIC SUFFIX (changes every turn)
────────────────────────────────────────────────────
  - Session memory (task, recent files, notes)
  - Conversation history (compressed)
  - Current user message
```

### Implementation with Anthropic API cache_control

```python
messages = [
    {
        "role": "user",
        "content": [
            {
                # STABLE — mark this for caching
                "type": "text",
                "text": system_prompt + workspace_context,
                "cache_control": {"type": "ephemeral"}
            },
            {
                # DYNAMIC — never cached
                "type": "text",
                "text": f"Memory:\n{memory_text}\n\nHistory:\n{history_text}\n\nTask:\n{user_message}"
            }
        ]
    }
]
```

### With Ollama (no cache_control support)
Just build one big prompt string and send it. The split still matters for YOUR logic
(knowing what to rebuild vs what to keep), even if Ollama doesn't cache it.

```python
prompt = stable_prefix + "\n\n" + dynamic_suffix
```

### Key rule
The stable prefix must NEVER contain the conversation history or current message.
The moment you put dynamic content in the stable part, caching breaks.

---

## Feature 3: Tools, Validation, and Permissions

### What it does
Defines what actions the agent can take, validates inputs before running them,
and gates risky operations behind user approval.

### Tool list (start with these 6)

```
list_dir      — see what files exist             (safe)
read_file     — read file contents               (safe)
search        — grep/find in files               (safe)
run_shell     — execute bash commands            (RISKY)
write_file    — create or overwrite a file       (RISKY)
patch_file    — edit part of an existing file    (RISKY)
```

### Tool schema (what you send to the API)

```python
TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Optionally specify line range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "run_shell",
        "description": "Run a bash command. Use for tests, installs, git operations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"}
            },
            "required": ["command"]
        }
    },
    # ... add others
]

# Mark which tools are risky
RISKY_TOOLS = {"run_shell", "write_file", "patch_file"}
```

### Approval system

```python
def approve(tool_name: str, args: dict, policy: str = "ask") -> bool:
    """
    policy = "ask"   → prompt user before risky tools (default, recommended)
    policy = "auto"  → allow everything automatically (dangerous)
    policy = "never" → deny all risky tools (for subagents)
    """
    if tool_name not in RISKY_TOOLS:
        return True  # safe tools always allowed

    if policy == "auto":
        return True

    if policy == "never":
        return False

    # policy == "ask": show the user and prompt
    print(f"\n⚠️  Agent wants to run: {tool_name}")
    print(f"   Args: {args}")
    answer = input("   Allow? [y/N]: ").strip().lower()
    return answer == "y"
```

### Validation before running

```python
def validate_tool(name: str, args: dict) -> str | None:
    """Returns an error string if invalid, None if ok."""
    if name == "read_file":
        if not args.get("path"):
            return "path is required"
        if not Path(args["path"]).exists():
            return f"file not found: {args['path']}"
    if name == "run_shell":
        if not args.get("command"):
            return "command is required"
    return None  # all good
```

### Repeated call detection (prevents infinite loops)

```python
# Track the last tool call
last_call = None

def check_repeated(name: str, args: dict) -> bool:
    global last_call
    current = (name, str(args))
    if current == last_call:
        return True  # repeated!
    last_call = current
    return False
```

If the model calls the same tool with the same args twice in a row — stop it.
Return an error: "Repeated identical tool call. Choose a different approach."

### Tool handlers (actual Python functions)

```python
import subprocess
from pathlib import Path

def tool_read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    p = Path(path)
    if not p.exists():
        return f"error: file not found: {path}"
    lines = p.read_text().splitlines()
    if start_line or end_line:
        lines = lines[(start_line or 1) - 1 : end_line]
    return "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines))

def tool_run_shell(command: str, timeout: int = 30) -> str:
    result = subprocess.run(
        command, shell=True, capture_output=True,
        text=True, timeout=timeout
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    parts = [p for p in [out, err] if p]
    if result.returncode != 0:
        parts.append(f"exit code: {result.returncode}")
    return "\n".join(parts) or "(no output)"

def tool_write_file(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {path} ({len(content)} chars)"
```

---

## Feature 4: Context Management

### What it does
The conversation history grows every turn. Without trimming, you'll hit the context
window limit. Context management keeps history within limits without losing important info.

### Two techniques

**clip() — hard truncation for tool output**
```python
def clip(text: str, limit: int = 4000) -> str:
    """Truncate any string to avoid flooding the context."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]
```

Always run tool output through clip() before adding to history.

**history_text() — smart compression of past messages**
```python
def history_text(history: list, max_chars: int = 12000) -> str:
    """
    Build a compressed text representation of conversation history.
    Recent messages get more space. Old messages get truncated harder.
    """
    if not history:
        return ""

    # Split into recent (last 6) and older
    recent = history[-6:]
    older = history[:-6]

    parts = []

    # Older messages — very compressed
    for entry in older:
        role = entry["role"]
        content = clip(entry["content"], limit=500)  # tight limit
        parts.append(f"[{role}]: {content}")

    # Recent messages — more room
    for entry in recent:
        role = entry["role"]
        content = clip(entry["content"], limit=2000)
        parts.append(f"[{role}]: {content}")

    result = "\n\n".join(parts)

    # Final cap on the whole thing
    return clip(result, max_chars)
```

### Deduplication of file reads
If the model reads the same file multiple times, only show the first read in history.
Later reads in older messages get replaced with "(already read above)".

```python
def deduplicate_reads(history: list) -> list:
    seen_files = set()
    result = []
    for entry in history:
        if entry.get("tool") == "read_file":
            path = entry.get("args", {}).get("path")
            if path in seen_files:
                entry = {**entry, "content": f"(read of {path} deduplicated)"}
            else:
                seen_files.add(path)
        result.append(entry)
    return result
```

### When to trigger summarization
Instead of (or in addition to) compression, you can make a second API call to summarize
old messages when history exceeds a threshold.

```python
def summarize_old_messages(messages: list, client) -> list:
    if len(messages) <= 20:
        return messages

    # Take the oldest 10 messages
    to_summarize = messages[:10]
    keep = messages[10:]

    summary_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"Summarize these messages in 3-5 sentences:\n{str(to_summarize)}"
        }]
    )
    summary = summary_response.content[0].text

    # Replace old messages with one summary message
    summary_message = {
        "role": "user",
        "content": f"[Summary of earlier conversation]: {summary}"
    }
    return [summary_message] + keep
```

---

## Feature 5: Session Memory and Resumption

### What it does
Two separate things that people often confuse:
- **Session memory** → a compact in-memory dict updated after each turn (task, files, notes)
- **Resumption** → saves the full session to disk so you can restart and continue

### Memory structure

```python
# Keep this compact — it goes in every prompt
memory = {
    "task": "",           # current goal, 1 sentence
    "files": [],          # last 8 files touched
    "notes": []           # last 5 important things the agent noticed
}
```

### Updating memory after each tool call

```python
def update_memory(memory: dict, tool_name: str, args: dict, result: str):
    """Compact memory update after each tool call."""

    # Track recently touched files
    if tool_name in ("read_file", "write_file", "patch_file"):
        path = args.get("path", "")
        if path and path not in memory["files"]:
            memory["files"].insert(0, path)
            memory["files"] = memory["files"][:8]  # keep last 8

    # Let the model update notes via a note tool (see below)
    return memory
```

### Note tool — let the model update its own memory

```python
# Add this to your tool list
{
    "name": "note",
    "description": "Save an important observation to memory. Use for key findings you'll need later.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The thing to remember"}
        },
        "required": ["text"]
    }
}

def tool_note(memory: dict, text: str) -> str:
    memory["notes"].insert(0, text)
    memory["notes"] = memory["notes"][:5]  # keep last 5
    return "noted"
```

### Session store (disk persistence)

```python
import json
import uuid
from pathlib import Path
from datetime import datetime

SESSION_DIR = Path(".mini-coding-agent/sessions")

class SessionStore:
    def __init__(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self.session_id = str(uuid.uuid4())[:8]
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
        """Append a turn to history and save."""
        entry = {"role": role, "content": content}
        if tool:
            entry["tool"] = tool
            entry["args"] = args or {}
        self.data["history"].append(entry)
        self.save()

    @classmethod
    def resume(cls, session_id: str = "latest") -> "SessionStore":
        """Load an existing session from disk."""
        store = cls()
        if session_id == "latest":
            sessions = sorted(SESSION_DIR.glob("*.json"))
            if not sessions:
                raise FileNotFoundError("No sessions to resume")
            path = sessions[-1]
        else:
            path = SESSION_DIR / f"{session_id}.json"

        data = json.loads(path.read_text())
        store.data = data
        store.session_id = data["id"]
        store.path = path
        return store
```

### How to use it

```bash
# Normal start
python agent.py --cwd ./my-project

# Resume latest session
python agent.py --cwd ./my-project --resume latest

# Resume specific session
python agent.py --cwd ./my-project --resume a3f9b2c1
```

---

## Feature 6: Subagents

### What it does
The main agent can delegate a focused subtask to a child agent. The child has its own
tool loop and runs until completion. The result comes back as a string.

### Key design rules
1. Subagents are **read-only by default** — they cannot write files or run shell commands
2. Approval policy is **"never"** for risky tools — they just get denied
3. Max steps are **capped** (e.g. 5) — prevents runaway loops
4. Depth is **tracked** — subagents cannot spawn their own subagents (depth limit = 1)

### Implementation

```python
# Add to tools list
{
    "name": "delegate",
    "description": (
        "Spawn a read-only sub-agent to handle a focused subtask. "
        "Use when you need to gather information from multiple files "
        "or analyze something independently. "
        "The sub-agent cannot write files or run shell commands."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What the sub-agent should do"},
            "context": {"type": "string", "description": "Any relevant context to pass in"}
        },
        "required": ["task"]
    }
}

def tool_delegate(task: str, context: str = "", depth: int = 0) -> str:
    if depth >= 1:
        return "error: subagents cannot spawn subagents (max depth reached)"

    # Create a child agent
    child = Agent(
        system_prompt=f"You are a focused sub-agent. Complete this task only:\n{task}",
        approval_policy="never",   # no risky tools
        read_only=True,            # write_file and run_shell are disabled
        max_steps=5,
        depth=depth + 1
    )

    if context:
        task = f"Context:\n{context}\n\nTask:\n{task}"

    return child.run(task)
```

### read_only mode in tool execution

```python
def run_tool(name: str, args: dict, read_only: bool = False) -> str:
    if read_only and name in ("write_file", "patch_file", "run_shell"):
        return f"error: {name} is not allowed for sub-agents (read-only mode)"
    # ... rest of tool execution
```

---

## The Core Agent Loop

This is the most important piece. Get this right first.

```python
import anthropic

class Agent:
    def __init__(self, system_prompt, approval_policy="ask",
                 read_only=False, max_steps=20, depth=0):
        self.client = anthropic.Anthropic()
        self.system_prompt = system_prompt
        self.approval_policy = approval_policy
        self.read_only = read_only
        self.max_steps = max_steps
        self.depth = depth
        self.messages = []
        self.last_call = None  # for repeat detection

    def run(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        steps = 0

        while steps < self.max_steps:
            steps += 1

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8096,
                system=self.system_prompt,
                tools=TOOLS,
                messages=self.messages
            )

            # Add assistant turn to history
            self.messages.append({"role": "assistant", "content": response.content})

            # Done — extract and return final text
            if response.stop_reason == "end_turn":
                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                return "\n".join(text_blocks)

            # Handle tool calls
            if response.stop_reason == "tool_use":
                tool_results = []

                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    name = block.name
                    args = block.input

                    # Check for repeated call
                    current_call = (name, str(args))
                    if current_call == self.last_call:
                        result = "error: repeated identical tool call. try a different approach."
                    else:
                        self.last_call = current_call

                        # Validate
                        error = validate_tool(name, args)
                        if error:
                            result = f"error: {error}"

                        # Check approval for risky tools
                        elif not approve(name, args, self.approval_policy):
                            result = f"error: {name} was denied by user"

                        # Run the tool
                        else:
                            result = run_tool(name, args, read_only=self.read_only)
                            result = clip(result, 4000)  # always clip output

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

                # IMPORTANT: send ALL results in one message, not one at a time
                self.messages.append({"role": "user", "content": tool_results})

        return f"Stopped: reached max steps ({self.max_steps})"
```

---

## SOUL.md — Agent Identity

Create this file at the root of your agent. Load it into the system prompt.

```markdown
# SOUL.md

## Identity
I am a coding agent. I help users build, debug, and understand code.

## How I work
- I always read files before editing them
- I explain what I am about to do before doing it
- I prefer small, targeted changes over large rewrites
- I ask for clarification when the task is ambiguous

## What I never do
- I never run destructive commands (rm -rf, DROP TABLE) without explicit confirmation
- I never make up file paths or function names — I verify with list_dir or read_file first
- I never repeat a tool call that already failed

## Communication style
- Direct and concise
- Show relevant code, not walls of text
- If something is wrong, say so clearly
```

Load it:
```python
soul = Path("SOUL.md").read_text() if Path("SOUL.md").exists() else ""
system_prompt = base_prompt + "\n\n" + soul
```

---

## Packaging — pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "my-coding-agent"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "anthropic>=0.25.0"
]

[project.scripts]
agent = "agent:main"   # maps 'agent' command to main() in agent.py
```

---

## install.sh — One Command Install

```bash
#!/bin/bash
set -e

echo "Installing coding agent..."

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: Python 3.10+ required"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_VERSION" -lt 10 ]; then
    echo "Error: Python 3.10+ required (found 3.$PY_VERSION)"
    exit 1
fi

# Install
pip install git+https://github.com/YOUR_USERNAME/YOUR_REPO.git

# Check for API key
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo ""
    echo "No ANTHROPIC_API_KEY found."
    echo "Add this to your ~/.bashrc or ~/.zshrc:"
    echo "  export ANTHROPIC_API_KEY=your_key_here"
fi

echo ""
echo "Done. Run: agent"
```

Make it executable: `chmod +x install.sh`
Users install with: `curl -sSL https://raw.githubusercontent.com/YOU/REPO/main/install.sh | bash`

---

## Common Mistakes

**Sending tool results one at a time**
Wrong: send one result, wait for response, send next result.
Right: collect ALL tool results from one response, send them all in a single user message.

**Putting dynamic content in the stable prefix**
If the stable prefix contains history or the current message, caching never works.
Keep it strictly static: rules + workspace + tool descriptions only.

**Subagents with unlimited steps**
Without a step cap, a subagent can run indefinitely and cost a lot.
Always set max_steps for subagents (5 is a good default).

**Not clipping tool output**
A file with 10,000 lines will flood the context instantly.
Always run tool output through clip() before storing in history.

**Forgetting to handle multiple tool calls per response**
The API can return 2-3 tool calls in one response.
Your loop must handle all of them before making the next API call.

---

## Checklist Before You Call It Done

- [ ] Workspace context injected into system prompt at startup
- [ ] Stable vs dynamic prompt split in place
- [ ] All 6 tools working: list_dir, read_file, search, run_shell, write_file, patch_file
- [ ] Risky tools gated behind approval prompt
- [ ] Repeated tool call detection stops infinite loops
- [ ] Tool output clipped to 4000 chars max
- [ ] History compressed after 20+ messages
- [ ] Session saved to disk after every turn
- [ ] Resume works with --resume latest
- [ ] Subagents are read-only with max 5 steps
- [ ] SOUL.md loaded into system prompt
- [ ] install.sh works with one command