import asyncio
import time
from pathlib import Path

from context import clip, summarize_old_messages
from memory import WorkspaceContext, update_memory
from providers import get_default_provider, StreamEvent
from tools import (
    TOOLS, RISKY_TOOLS, TOOL_HANDLERS,
    approve, validate_tool, RepeatDetector, tool_note
)

STREAM_TIMEOUT = 60


SOUL_PATH = Path(__file__).parent / "SOUL.md"


def load_soul() -> str:
    if SOUL_PATH.exists():
        return SOUL_PATH.read_text()
    return ""


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

        self.workspace = WorkspaceContext(cwd=cwd)
        soul = load_soul()

        self._base_prompt = f"""You are a coding agent. You help the user build and modify code.

{self.workspace.build()}

{soul}

Available tools:
{DEFAULT_TOOL_DESC}

Rules:
- Before writing tests for existing code, read the implementation first
- New files must be complete and runnable
- Do not repeat the same tool call with the same args if it didn't help
- Use the note tool to save important observations you will need later
- Always read a file before editing it
- For simple tasks (creating a file, running a command), do it directly without unnecessary exploration"""

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
            if len(self.messages) > 2:
                self.messages.pop(1)
            else:
                break

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
                        elif not approve(name, args, self.approval_policy):
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
            from display import print_assistant, print_tool_call, print_tool_result, print_error
            if event == "stream_text":
                print(args[0], end="", flush=True)
            elif event == "assistant_text":
                print_assistant(args[0])
            elif event == "tool_call":
                print_tool_call(args[0], args[1])
            elif event == "tool_result":
                print_tool_result(args[0], args[1], args[2] if len(args) > 2 else 0)
            elif event == "error":
                print_error(args[0])

    async def _execute_tool(self, name: str, args: dict) -> str:
        if name == "note":
            return tool_note(self.memory, args.get("text", ""))
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return f"error: unknown tool: {name}"
        try:
            return await handler(**args)
        except Exception as e:
            return f"error: {e}"
