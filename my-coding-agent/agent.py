import time
from pathlib import Path

from context import clip, deduplicate_reads, history_text, summarize_old_messages
from display import print_assistant, print_tool_call, print_tool_result, print_error
from memory import WorkspaceContext, update_memory
from providers import BaseProvider, get_default_provider
from tools import (
    TOOLS, RISKY_TOOLS, TOOL_HANDLERS,
    approve, validate_tool, RepeatDetector, tool_note
)


SOUL_PATH = Path(__file__).parent / "SOUL.md"


def load_soul() -> str:
    if SOUL_PATH.exists():
        return SOUL_PATH.read_text()
    return ""


DEFAULT_TOOL_DESC = "\n".join(
    f"- {t['name']}: {t['description']}" for t in TOOLS
)


class Agent:
    def __init__(
        self,
        cwd: str = ".",
        approval_policy: str = "ask",
        read_only: bool = False,
        max_steps: int = 25,
        depth: int = 0,
        provider: BaseProvider | None = None,
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

        self.workspace = WorkspaceContext(cwd=cwd)
        soul = load_soul()

        self.stable_prefix = f"""You are a coding agent. You help the user build and modify code.

{self.workspace.build()}

{soul}

Available tools:
{DEFAULT_TOOL_DESC}

Rules:
- Before writing tests for existing code, read the implementation first
- New files must be complete and runnable
- Do not repeat the same tool call with the same args if it didn't help
- Use the note tool to save important observations you will need later
- Always read a file before editing it"""

    def _build_dynamic_suffix(self, user_message: str) -> str:
        mem = self.memory
        memory_text = f"Task: {mem.get('task', '') or '(none)'}"
        if mem.get("files"):
            memory_text += f"\nRecent files: {', '.join(mem['files'])}"
        if mem.get("notes"):
            memory_text += f"\nNotes: {'; '.join(mem['notes'])}"

        history = history_text(self.messages)
        return f"Memory:\n{memory_text}\n\nHistory:\n{history}\n\nUser:\n{user_message}"

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

            dynamic = self._build_dynamic_suffix(user_message)
            api_messages = []
            
            # If last message is tool results, preserve the full conversation
            if (self.messages and isinstance(self.messages[-1].get("content"), list) and 
                any(item.get("type") == "tool_result" for item in self.messages[-1]["content"])):
                # Tool results are present - use all messages as-is
                api_messages = self.messages.copy()
            else:
                # Normal flow: append dynamic suffix
                for m in self.messages[:-1]:
                    api_messages.append(m)
                api_messages.append({"role": "user", "content": dynamic})

            # Emit thinking status
            self._emit("thinking")

            try:
                response = await self.provider.send(
                    messages=api_messages,
                    system_prompt=self.stable_prefix,
                    tools=TOOLS
                )
            except Exception as e:
                error_msg = f"provider error: {e}"
                self._emit("error", error_msg)
                self.messages.append({"role": "user", "content": f"Error: {error_msg}. Try a simpler approach."})
                continue

            text_response = response.text

            # Build assistant content blocks (text + tool_use)
            assistant_blocks = []
            if text_response:
                assistant_blocks.append({"type": "text", "text": text_response})
            for block in response.content:
                if block.type == "tool_use":
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input
                    })

            if text_response:
                self._emit("assistant_text", text_response)

            self.messages.append({"role": "assistant", "content": assistant_blocks if assistant_blocks else "(tool call)"})
            display_text = text_response or "(tool call)" if not assistant_blocks else text_response or "(tool call)"
            if self.session_store:
                self.session_store.record("assistant", display_text)

            if response.stop_reason == "end_turn":
                return text_response

            if response.stop_reason == "max_tokens":
                self.messages.append({
                    "role": "user",
                    "content": "[System: response was truncated. Please continue.]"
                })
                continue

            if response.has_tool_calls:
                tool_results = []

                for block in response.content:
                    if block.type != "tool_use":
                        continue

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
                        elif self.read_only and name in ("write_file", "patch_file", "run_shell"):
                            result = f"error: {name} is not allowed for sub-agents (read-only mode)"
                            self._emit("error", result)
                        else:
                            start = time.time()
                            result = await self._execute_tool(name, args)
                            duration = time.time() - start
                            result = clip(result, 4000)
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
                # If callback signature doesn't match, try without args
                try:
                    cb()
                except Exception:
                    pass
        else:
            from display import print_assistant, print_tool_call, print_tool_result, print_error
            if event == "assistant_text":
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
