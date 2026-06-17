import asyncio
import time
from pathlib import Path

from context import clip, summarize_old_messages
from guardrails import check_tool as check_guardrails, check_output, guardrail_rules
from memory import WorkspaceContext, update_memory
from providers import get_default_provider, StreamEvent
from tools import (
    TOOLS, RISKY_TOOLS, TOOL_HANDLERS,
    approve, validate_tool, RepeatDetector, tool_note
)

STREAM_TIMEOUT = 60


SOUL_DEFAULT = """# Identity
I am a focused coding agent. I work inside the user's terminal to build, debug, and modify code. I am not a general-purpose chatbot — I am here to ship working software.

# Communication
- Be direct. Never open with "Great question" or "I'd be happy to help". Just answer.
- One sentence is often enough. If the answer fits in a single line, stop there.
- Show code, not explanations about code. A diff is worth a thousand words.
- If the user is about to do something dangerous or inefficient, say so clearly. Charm over cruelty, but don't sugarcoat.
- When the task is ambiguous, ask one targeted question — not three options. Commit to a recommendation.
- Never hedge with "it depends", "typically", or "in most cases". If there's a tradeoff, state your recommended path and why.

# Behavior
- Read files before editing them. Always.
- Make small, targeted changes — not large rewrites. Prefer one focused edit over sweeping refactors.
- If a tool call fails, try a different approach. Do not retry the exact same arguments.
- When exploring, dig until you find the answer. Surface-level searches waste turns.
- If you don't have enough context, gather it. Do not guess file paths, function names, or API signatures.

# Teamwork
- Let the user know what you're about to do — one sentence is enough
- Confirm before running destructive operations (deletes, drops, force pushes)
- If the user gives you a multi-step request, do all of it. Don't stop after step one.
"""

AGENTS_DEFAULT = """# AGENTS.md

Edit this file with your project details. The agent reads it at every session start.

## Project
-

## Tech Stack
- Python 3.x

## Commands
- Run:
- Test:
- Lint:

## Boundaries
-
"""

SYSTEM_PROMPT_DEFAULT = """You are an AI coding agent that operates inside the user's terminal. Your job is to help the user build, debug, and modify code in their project.

{workspace_context}

{soul}

{agents_context}

# Tools

You have these tools available. Use them to interact with the project.

{tool_list}

# Enforced Guardrails

The following rules are enforced at the code level. You cannot bypass them.

{guardrail_rules}

# Workflow

## Planning
1. Before making changes, understand the codebase. Read relevant files.
2. For complex tasks, plan the approach before executing. State your plan briefly.
3. If the task is simple (create a known file, fix a known bug), execute directly without over-planning.

## Execution
1. Read before editing. Always read a file before calling write_file or patch_file on it.
2. One change at a time. After each change, verify the result before proceeding.
3. When running shell commands, prefer commands that give clear output.
4. If a command fails, read the error message, adjust, and retry. Do not retry the exact same arguments.

## Tool Call Rules
1. Use search (ripgrep) for finding code patterns across files.
2. Use read_file with line ranges for targeted reading of large files.
3. Use list_dir to explore directory structure before guessing file paths.
4. Use note to save important findings you will need later in the conversation.
5. Use delegate for independent subtasks that can run in parallel (research, exploration).

## Error Recovery
1. If a tool returns an error, read the error and fix the root cause. Do not retry blindly.
2. If the provider returns an error, simplify your approach and try again.
3. If you hit the max steps limit, report what was accomplished and what remains.

# Conversation Management
1. The session memory section at the end of the system prompt tracks your current task, recent files, and notes. Check it before responding.
2. Use the note tool to save observations. Notes persist in session memory but not across sessions.
3. If the conversation becomes long, old messages may be summarized. Key information should be in notes, not buried in history."""


def _first_existing(base: Path, *names: str) -> str | None:
    for name in names:
        p = base / name
        if p.exists():
            return p.read_text()
    return None


def load_soul(cwd: str | None = None) -> str:
    pkg = Path(__file__).parent
    bases = ([Path(cwd).resolve()] if cwd else []) + [pkg / "cfg", pkg]
    for base in bases:
        result = _first_existing(base, "SOUL.md", "soul.md")
        if result is not None:
            return result
    return SOUL_DEFAULT


def load_agents_md(cwd: str | None = None) -> str:
    pkg = Path(__file__).parent
    bases = ([Path(cwd).resolve()] if cwd else []) + [pkg / "cfg", pkg]
    for base in bases:
        result = _first_existing(base, "AGENTS.md", "agents.md")
        if result is not None:
            return result
    return AGENTS_DEFAULT


def _load_system_template(cwd: str | None = None) -> str:
    pkg = Path(__file__).parent
    bases = []
    if cwd:
        bases.append(Path(cwd).resolve())
    bases += [pkg / "cfg", pkg]
    for base in bases:
        result = _first_existing(base, "SYSTEM_PROMPT.md", "system_prompt.md", "system-prompt.md")
        if result is not None:
            return result
    return SYSTEM_PROMPT_DEFAULT


DEFAULT_TOOL_DESC = "\n".join(
    f"- {t['name']}: {t['description']}" for t in TOOLS
)

MAX_MESSAGES = 30
MAX_TOOL_OUTPUT_CHARS = 1500


class Agent:
    def __init__(
        self,
        cwd: str = ".",
        approval_policy: str = "ask",
        read_only: bool = False,
        max_steps: int = 25,
        depth: int = 0,
        provider=None,
        session_store=None,
        callbacks: dict | None = None,
        system_prompt: str | None = None,
    ):
        self.cwd = cwd
        self.approval_policy = approval_policy
        self.read_only = read_only
        self.max_steps = max_steps
        self.depth = depth
        self.provider = provider or get_default_provider()
        self.session_store = session_store
        self.messages = []
        self.repeat_detector = RepeatDetector()
        self.memory = {"task": "", "files": [], "notes": []}
        self._callbacks = callbacks or {}
        self._step_count = 0
        self._streaming_failed = False

        if system_prompt:
            self._base_prompt = system_prompt
        else:
            self.workspace = WorkspaceContext(cwd=cwd)
            soul = load_soul(cwd)
            agents_md = load_agents_md(cwd)
            template = _load_system_template(cwd)
            agents_context = f"\nProject instructions:\n{agents_md}\n" if agents_md else ""

            self._base_prompt = template.format(
                workspace_context=self.workspace.build(),
                soul=soul,
                agents_context=agents_context,
                tool_list=DEFAULT_TOOL_DESC,
                guardrail_rules=guardrail_rules(),
            )

    def _build_system(self) -> str:
        mem = self.memory
        parts = [self._base_prompt, "\nSession memory:"]
        parts.append(f"Task: {mem.get('task', '') or '(none)'}")
        if mem.get("files"):
            parts.append(f"Recent files: {', '.join(mem['files'])}")
        if mem.get("notes"):
            parts.append(f"Notes: {'; '.join(mem['notes'])}")
        return "\n".join(parts)

    def _trim_messages(self):
        while len(self.messages) > MAX_MESSAGES:
            idx = 1
            if idx >= len(self.messages):
                break
            msg = self.messages[idx]
            content = msg.get("content", "")
            if isinstance(content, list):
                types = {b.get("type") for b in content if isinstance(b, dict)}
                if "tool_use" in types or "tool_result" in types:
                    self.messages.pop(idx)
                    if idx < len(self.messages):
                        nxt = self.messages[idx].get("content", "")
                        if isinstance(nxt, list):
                            nxt_types = {b.get("type") for b in nxt if isinstance(b, dict)}
                            if "tool_use" in nxt_types or "tool_result" in nxt_types:
                                self.messages.pop(idx)
                    continue
            self.messages.pop(idx)

    async def run(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        if self.session_store:
            self.session_store.record("user", user_message)

        steps = 0
        while steps < self.max_steps:
            steps += 1
            self._step_count = steps

            if len(self.messages) > 20:
                self.messages = await summarize_old_messages(self.messages, self.provider)

            self._trim_messages()
            system = self._build_system()
            self._emit("thinking")

            streaming_text = ""
            tool_calls = []
            stop_reason = "end_turn"
            stream_iter = None

            try:
                if self._streaming_failed:
                    response = await self.provider.send(self.messages, system, TOOLS)
                    streaming_text = response.text
                    tool_calls = [b for b in response.content if b.type == "tool_use"]
                    stop_reason = response.stop_reason
                    if streaming_text:
                        self._emit("stream_text", streaming_text)
                        self._emit("assistant_text", streaming_text)
                else:
                    stream_iter = self.provider.send_stream(self.messages, system, TOOLS)
                    while True:
                        try:
                            event = await asyncio.wait_for(stream_iter.__anext__(), timeout=STREAM_TIMEOUT)
                        except StopAsyncIteration:
                            break
                        if event.type == "text":
                            streaming_text += event.text
                            self._emit("stream_text", event.text)
                        elif event.type == "tool_call" and event.tool_call:
                            tool_calls.append(event.tool_call)
                        elif event.type == "done":
                            stop_reason = event.stop_reason
            except asyncio.TimeoutError:
                self._emit("error", "stream timed out, falling back to non-streaming")
                self._streaming_failed = True
                if stream_iter is not None:
                    try:
                        await stream_iter.aclose()
                    except Exception:
                        pass
                try:
                    response = await self.provider.send(self.messages, system, TOOLS)
                    streaming_text = response.text
                    tool_calls = [b for b in response.content if b.type == "tool_use"]
                    stop_reason = response.stop_reason
                    if streaming_text:
                        self._emit("stream_text", streaming_text)
                        self._emit("assistant_text", streaming_text)
                except Exception as e2:
                    error_msg = f"provider error: {e2}"
                    self._emit("error", error_msg)
                    self.messages.append({"role": "user", "content": f"Error: {error_msg}. Try a simpler approach."})
                    continue
            except Exception as e:
                self._streaming_failed = True
                if stream_iter is not None:
                    try:
                        await stream_iter.aclose()
                    except Exception:
                        pass
                error_msg = f"provider error: {e}"
                self._emit("error", error_msg)
                self.messages.append({"role": "user", "content": f"Error: {error_msg}. Try a simpler approach."})
                continue

            assistant_blocks = []
            if streaming_text:
                out_err = check_output(streaming_text)
                if out_err:
                    self._emit("error", out_err)
                    streaming_text = f"[System: model output was filtered — {out_err}]"
                assistant_blocks.append({"type": "text", "text": streaming_text})
            for tc in tool_calls:
                assistant_blocks.append({
                    "type": "tool_use", "id": tc.id,
                    "name": tc.name, "input": tc.input
                })

            if streaming_text:
                self._emit("assistant_text", streaming_text)

            self.messages.append({"role": "assistant", "content": assistant_blocks or ""})
            display_text = streaming_text or "(tool call)"
            if self.session_store:
                self.session_store.record("assistant", display_text)

            if stop_reason == "end_turn":
                return streaming_text

            if stop_reason == "max_tokens":
                self.messages.append({
                    "role": "user",
                    "content": "[System: response was truncated. Please continue.]"
                })
                continue

            if tool_calls:
                tool_results = []
                for block in tool_calls:
                    name = block.name
                    args = block.input or {}
                    self._emit("tool_call", name, args)

                    if self.repeat_detector.check(name, args):
                        result = "error: repeated identical tool call. try a different approach."
                        self._emit("error", result)
                    else:
                        error = validate_tool(name, args)
                        if error:
                            result = f"error: {error}"
                            self._emit("error", result)
                        elif (guard_err := check_guardrails(name, args, self.cwd)):
                            result = f"error: guardrail blocked: {guard_err}"
                            self._emit("error", result)
                        elif not await approve(name, args, self.approval_policy):
                            result = f"error: {name} was denied by user"
                            self._emit("error", result)
                        elif self.read_only and name in RISKY_TOOLS:
                            result = f"error: {name} is not allowed for sub-agents (read-only mode)"
                            self._emit("error", result)
                        else:
                            start = time.time()
                            result = await self._execute_tool(name, args)
                            duration = time.time() - start
                            result = clip(result, MAX_TOOL_OUTPUT_CHARS)
                            self._emit("tool_result", name, result, duration)
                            update_memory(self.memory, name, args, result)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

                self.messages.append({"role": "user", "content": tool_results})
                if self.session_store:
                    self.session_store.record("user", str(tool_results)[:500])
                if self.session_store:
                    self.session_store.memory = self.memory

        return f"Stopped: reached max steps ({self.max_steps})"

    async def _run_subagent(self, task: str, context: str = "") -> str:
        if self.depth >= 1:
            return "error: subagents cannot spawn subagents (max depth reached)"

        prompt = f"""You are a focused sub-agent. Complete the following task using the available tools.

Task: {task}
{f"Context: {context}" if context else ""}

You can read files, search code, and explore the workspace but CANNOT write files or run shell commands.

Enforced guardrails:
{guardrail_rules()}

Report back your findings concisely. When done, provide a clear summary."""
        child = Agent(
            cwd=self.cwd,
            approval_policy="never",
            read_only=True,
            max_steps=5,
            depth=self.depth + 1,
            provider=self.provider,
            callbacks=self._callbacks,
            system_prompt=prompt,
        )
        result = await child.run(task)
        return f"[Sub-agent result]\n{result}"

    def _emit(self, event: str, *args):
        cb = self._callbacks.get(event)
        if cb:
            try:
                cb(*args)
            except TypeError:
                try:
                    cb()
                except Exception:
                    pass
        else:
            if event == "stream_text":
                print(args[0], end="", flush=True)
            elif event == "assistant_text":
                print(args[0])
            elif event == "tool_call":
                print(f"  [{args[0]}(...)]")
            elif event == "error":
                print(f"  x {args[0]}", file=sys.stderr)

    async def _execute_tool(self, name: str, args: dict) -> str:
        if name == "note":
            return tool_note(self.memory, args.get("text", ""))
        if name == "delegate":
            return await self._run_subagent(
                args.get("task", ""), args.get("context", "")
            )
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return f"error: unknown tool: {name}"
        try:
            return await handler(**args)
        except Exception as e:
            return f"error: {e}"
