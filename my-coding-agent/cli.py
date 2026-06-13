import argparse
import re

from textual.app import App, ComposeResult
from textual.suggester import Suggester
from textual.widgets import Header, Input, RichLog, Static
from rich.text import Text
from rich.markdown import Markdown
from rich.panel import Panel

from agent import Agent
from memory import SessionStore
from providers import PROVIDER_REGISTRY, create_provider, load_config, save_config


SLASH_COMMANDS = ["/help", "/exit", "/model", "/providers", "/clear", "/resume"]


API_KEY_PROVIDERS = [
    (re.compile(r"^sk-ant-"), "anthropic"),
    (re.compile(r"^sk-or-"), "openrouter"),
    (re.compile(r"^sk-"), "openai"),
    (re.compile(r"^gsk_"), "groq"),
    (re.compile(r"^tr_"), "tokenrouter"),
]


def detect_provider_from_key(key: str) -> str | None:
    for pattern, provider in API_KEY_PROVIDERS:
        if pattern.match(key):
            return provider
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coding Harness — an AI coding agent")
    parser.add_argument("--cwd", default=".", help="Working directory")
    parser.add_argument("--resume", nargs="?", const="latest",
                        help="Resume a session")
    parser.add_argument("--provider", default=None, help="LLM provider")
    parser.add_argument("--model", default=None, help="Model name")
    parser.add_argument("--policy", default="ask", choices=["ask", "auto", "never"],
                        help="Approval policy for risky tools")
    return parser.parse_args()


class CommandSuggester(Suggester):
    async def get_suggestion(self, value: str) -> str | None:
        if value.startswith("/"):
            for cmd in SLASH_COMMANDS:
                if cmd.startswith(value) and cmd != value:
                    return cmd[len(value):]
        return None


class HarnessApp(App):
    TITLE = "Coding Harness"
    CSS = """
    Screen {
        layout: vertical;
    }

    #chat-log {
        width: 100%;
        height: 1fr;
        border: none;
        padding: 0;
        overflow-y: auto;
        overflow-x: hidden;
    }

    #chat-input {
        width: 100%;
        height: 3;
        border: solid $primary;
        margin: 0;
    }

    #chat-stream {
        width: 100%;
        height: auto;
        max-height: 8;
        background: $surface;
        color: $text;
        padding: 0 1;
        border: none;
        overflow-y: auto;
        overflow-x: hidden;
    }

    #status-bar {
        width: 100%;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0;
        content-align: left middle;
    }
    """

    def __init__(self, agent: Agent, session_store: SessionStore):
        super().__init__()
        self.agent = agent
        self.session_store = session_store
        self._stream_buffer = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
        yield Static(id="chat-stream")
        yield Input(id="chat-input", placeholder="Type a message or paste an API key...", suggester=CommandSuggester())
        yield Static(id="status-bar")

    def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()
        self.query_one("#chat-log", RichLog).write(
            Text(f"Session: {self.session_store.session_id}", style="dim")
        )
        self.query_one("#chat-log", RichLog).write(
            Text(f"Provider: {self.agent.provider.name} | Model: {self.agent.provider.model}", style="dim")
        )
        self.query_one("#chat-log", RichLog).write(
            Text(f"Working directory: {self.agent.cwd}", style="dim")
        )
        self.query_one("#chat-log", RichLog).write(Text("─" * 40, style="dim"))
        self._update_status()

    def _update_status(self, msg: str = ""):
        steps = getattr(self.agent, "_step_count", 0)
        base = f" {self.agent.provider.name}/{self.agent.provider.model}  |  steps: {steps}"
        if msg:
            t = Text(base)
            t.append("  |  ")
            t.append(msg, style="bold yellow")
            self.query_one("#status-bar", Static).update(t)
        else:
            self.query_one("#status-bar", Static).update(base)

    def _write_user(self, text: str):
        self.query_one("#chat-log", RichLog).write(
            Panel(Text(text, style="bold"), title="you", border_style="blue", expand=True)
        )

    def _write_assistant(self, text: str):
        md = Markdown(text)
        self.query_one("#chat-log", RichLog).write(
            Panel(md, title="assistant", border_style="green", expand=True)
        )

    def _write_tool_call(self, name: str, args: dict):
        args_str = " ".join(f"{k}={v}" for k, v in args.items())
        self.query_one("#chat-log", RichLog).write(
            Text(f"  ⚙️  {name}({args_str})", style="bold cyan")
        )

    def _write_tool_result(self, result: str, duration: float = 0):
        # Show first few lines of result
        lines = result.strip().split("\n")[:3]
        preview = "\n    ".join(lines)
        dur = f" ({duration:.1f}s)" if duration else ""
        self.query_one("#chat-log", RichLog).write(
            Text(f"  └─ {preview}{dur}", style="dim green")
        )

    def _write_system(self, text: str):
        if "\n" in text:
            lines = [f"  ■ {line}" for line in text.split("\n")]
            self.query_one("#chat-log", RichLog).write(Text("\n".join(lines), style="dim"))
        else:
            self.query_one("#chat-log", RichLog).write(Text(f"  ■ {text}", style="dim"))

    def _write_error(self, text: str):
        self.query_one("#chat-log", RichLog).write(Text(f"  x {text}", style="bold red"))

    def _write_slash_menu(self):
        """Show interactive slash command menu."""
        from rich.table import Table
        
        table = Table(title="Slash Commands", show_header=True, header_style="bold cyan")
        table.add_column("Command", style="bold yellow")
        table.add_column("Description", style="dim")
        
        commands = [
            ("/help", "Show all commands"),
            ("/model", "Show or change model"),
            ("/providers", "List available providers"),
            ("/clear", "Clear conversation history"),
            ("/resume [id]", "Resume a session"),
            ("/exit", "Exit the application"),
        ]
        
        for cmd, desc in commands:
            table.add_row(cmd, desc)
        
        self.query_one("#chat-log", RichLog).write(table)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        input_widget = self.query_one("#chat-input", Input)
        text = input_widget.value.strip()
        input_widget.clear()

        if not text:
            return

        if text.startswith("/"):
            await self._handle_slash(text)
            return

        provider = detect_provider_from_key(text)
        if provider:
            cfg = load_config()
            cfg["api_key"] = text
            cfg["provider"] = provider
            save_config(cfg)
            self.agent.provider = create_provider(provider, cfg.get("model"), text)
            self._write_system(f"API key saved — switched to {provider}")
            self._update_status()
            return

        self._write_user(text)
        self._stream_buffer = ""
        stream_widget = self.query_one("#chat-stream", Static)

        def on_stream_text(token: str):
            self._stream_buffer += token
            stream_widget.update(self._stream_buffer)
            stream_widget.scroll_end(animate=False)

        def on_assistant_text(full_text: str):
            if full_text:
                stream_widget.update("")
                self._write_assistant(full_text)

        def on_tool_call(name, args):
            if self._stream_buffer:
                stream_widget.update("")
                self._write_assistant(self._stream_buffer)
                self._stream_buffer = ""
            self._write_tool_call(name, args)
            self._update_status(f"⚙️  {name}...")

        def on_tool_result(name, result, duration):
            self._write_tool_result(result, duration)
            self._update_status("")

        callbacks = {
            "thinking": lambda: self._update_status("Thinking..."),
            "stream_text": on_stream_text,
            "assistant_text": on_assistant_text,
            "tool_call": on_tool_call,
            "tool_result": on_tool_result,
            "error": self._write_error,
        }

        self.agent._callbacks = callbacks
        self._update_status("thinking...")

        try:
            await self.agent.run(text)
        except Exception as e:
            self._write_error(f"Error: {e}")

        self._update_status()

    async def _handle_slash(self, cmd: str):
        parts = cmd.strip().lstrip("/").split()
        
        # If just "/" with no command, show menu
        if not parts or not parts[0]:
            self._write_slash_menu()
            return
        
        command = parts[0].lower()

        if command in ("exit", "quit"):
            await self.action_quit()
            return

        if command == "help":
            self._write_slash_menu()
            self._write_system("💡 Tip: just paste an API key — it auto-detects the provider")
            return

        if command == "model":
            if len(parts) == 1:
                self._write_system(f"Provider: {self.agent.provider.name}")
                self._write_system(f"Model: {self.agent.provider.model}")
            else:
                val = parts[1]
                if "/" in val:
                    pname, model = val.split("/", 1)
                else:
                    pname = parts[1] if len(parts) > 1 else self.agent.provider.name
                    model = parts[2] if len(parts) > 2 else None
                if pname not in PROVIDER_REGISTRY and model is None:
                    model = pname
                    pname = self.agent.provider.name
                if pname not in PROVIDER_REGISTRY:
                    self._write_error(f"Unknown provider: {pname}")
                    return
                try:
                    cfg = load_config()
                    self.agent.provider = create_provider(pname, model, cfg.get("api_key"))
                    cfg["provider"] = pname
                    if model:
                        cfg["model"] = model
                    save_config(cfg)
                    self._write_system(f"Switched to {pname}/{self.agent.provider.model}")
                    self._update_status()
                except Exception as e:
                    self._write_error(str(e))
            return

        if command == "providers":
            from rich.table import Table
            
            table = Table(title="Available Providers", show_header=True, header_style="bold cyan")
            table.add_column("Provider", style="bold yellow")
            table.add_column("Available Models", style="dim")
            
            for name, cls in sorted(PROVIDER_REGISTRY.items()):
                try:
                    p = cls()
                    models = p.get_available_models()
                    model_list = ", ".join(models[:3])
                    if len(models) > 3:
                        model_list += f" (+{len(models) - 3} more)"
                    table.add_row(name, model_list)
                except Exception:
                    table.add_row(name, "(unavailable)")
            
            self.query_one("#chat-log", RichLog).write(table)
            return

        if command == "clear":
            self.agent.messages = []
            self.agent.repeat_detector.last_call = None
            self.query_one("#chat-log", RichLog).clear()
            self._write_system("History cleared")
            return

        if command == "resume":
            sid = parts[1] if len(parts) > 1 else "latest"
            try:
                store = SessionStore.resume(sid)
                self.agent.messages = store.data.get("history", [])
                self.agent.memory = store.data.get("memory", {"task": "", "files": [], "notes": []})
                self.query_one("#chat-log", RichLog).clear()
                self._write_system(f"Resumed session {store.session_id} ({len(self.agent.messages)} turns)")
            except FileNotFoundError:
                self._write_error(f"Session not found: {sid}")
            return

        self._write_error(f"Unknown: {command}. Try /help")


def main():
    args = parse_args()

    if args.provider or args.model:
        cfg = load_config()
        if args.provider:
            cfg["provider"] = args.provider
        if args.model:
            cfg["model"] = args.model
        save_config(cfg)

    if args.resume:
        try:
            session_store = SessionStore.resume(args.resume)
        except FileNotFoundError:
            print(f"Session not found: {args.resume}. Starting fresh.")
            session_store = SessionStore()
    else:
        session_store = SessionStore()

    app = None

    def on_assistant_text(text: str):
        if app:
            app._write_assistant(text)

    def on_tool_call(name: str, args: dict):
        if app:
            app._write_tool_call(name, args)

    def on_tool_result(name: str, result: str, duration: float):
        if app:
            app._write_tool_result(result, duration)
            app._update_status(f"ran {name}")

    def on_error(msg: str):
        if app:
            app._write_error(msg)

    agent = Agent(
        cwd=args.cwd,
        approval_policy="auto",
        session_store=session_store,
        callbacks={
            "assistant_text": on_assistant_text,
            "tool_call": on_tool_call,
            "tool_result": on_tool_result,
            "error": on_error,
        },
    )
    agent._step_count = 0

    if args.resume:
        try:
            agent.messages = session_store.data.get("history", [])
            agent.memory = session_store.data.get("memory", {"task": "", "files": [], "notes": []})
        except Exception:
            pass

    app = HarnessApp(agent=agent, session_store=session_store)
    app.run()


if __name__ == "__main__":
    main()
