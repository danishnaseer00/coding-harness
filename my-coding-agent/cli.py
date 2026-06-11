import argparse

from textual.app import App, ComposeResult
from textual.widgets import Header, Input, RichLog, Static
from rich.text import Text
from rich.markdown import Markdown
from rich.panel import Panel

from agent import Agent
from memory import SessionStore
from providers import PROVIDER_REGISTRY, create_provider, load_config, save_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coding Harness — an AI coding agent")
    parser.add_argument("--cwd", default=".", help="Working directory")
    parser.add_argument("--resume", nargs="?", const="latest",
                        help="Resume a session (pass 'latest' or a session ID)")
    parser.add_argument("--provider", default=None, help="LLM provider")
    parser.add_argument("--model", default=None, help="Model name")
    parser.add_argument("--policy", default="ask", choices=["ask", "auto", "never"],
                        help="Approval policy for risky tools")
    return parser.parse_args()


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
        padding: 0 1;
        overflow-y: auto;
    }

    #input-container {
        width: 100%;
        height: 3;
        padding: 0 1;
        background: $surface;
        border-top: solid $primary;
    }

    #chat-input {
        width: 100%;
        height: 3;
        border: solid $primary;
        margin: 0;
    }

    #status-bar {
        width: 100%;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        content-align: left middle;
    }
    """

    def __init__(self, agent: Agent, session_store: SessionStore):
        super().__init__()
        self.agent = agent
        self.session_store = session_store

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
        yield Input(id="chat-input", placeholder="Type a message...")
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
        text = f" {self.agent.provider.name}/{self.agent.provider.model}  |  steps: {steps}"
        if msg:
            text += f"  |  {msg}"
        self.query_one("#status-bar", Static).update(text)

    def _write_user(self, text: str):
        self.query_one("#chat-log", RichLog).write(
            Panel(Text(text, style="bold"), title="you", border_style="blue", width=None)
        )

    def _write_assistant(self, text: str):
        md = Markdown(text)
        self.query_one("#chat-log", RichLog).write(
            Panel(md, title="assistant", border_style="green", width=None)
        )

    def _write_tool_call(self, name: str, args: dict):
        args_str = " ".join(f"{k}={v}" for k, v in args.items())
        self.query_one("#chat-log", RichLog).write(
            Text(f"  >> {name}({args_str})", style="bold cyan")
        )

    def _write_tool_result(self, result: str, duration: float = 0):
        preview = result.strip()[:200]
        dur = f" ({duration:.1f}s)" if duration else ""
        self.query_one("#chat-log", RichLog).write(
            Text(f"  └─ {preview}{dur}", style="dim")
        )

    def _write_system(self, text: str):
        self.query_one("#chat-log", RichLog).write(Text(f"  ■ {text}", style="dim"))

    def _write_error(self, text: str):
        self.query_one("#chat-log", RichLog).write(Text(f"  x {text}", style="bold red"))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        input_widget = self.query_one("#chat-input", Input)
        text = input_widget.value.strip()
        input_widget.clear()

        if not text:
            return

        if text.startswith("/"):
            await self._handle_slash(text)
            return

        self._write_user(text)
        self._update_status("thinking...")

        try:
            await self.agent.run(text)
        except Exception as e:
            self._write_error(f"Error: {e}")

        self._update_status()

    async def _handle_slash(self, cmd: str):
        parts = cmd.strip().lstrip("/").split()
        command = parts[0].lower()

        if command in ("exit", "quit"):
            await self.action_quit()
            return

        if command == "help":
            self._write_system("Available commands:")
            self._write_system("  /help              - Show this help")
            self._write_system("  /exit              - Exit the harness")
            self._write_system("  /model             - Show current provider/model")
            self._write_system("  /model list        - List available providers")
            self._write_system("  /model set <p>/<m> - Switch provider/model")
            self._write_system("  /key <api_key>     - Set API key and save to config")
            self._write_system("  /resume            - Resume latest session")
            self._write_system("  /resume <id>       - Resume specific session")
            self._write_system("  /clear             - Clear conversation history")
            return

        if command == "model":
            if len(parts) == 1:
                self._write_system(f"Provider: {self.agent.provider.name}")
                self._write_system(f"Model: {self.agent.provider.model}")
            elif parts[1] == "list":
                for name, cls in PROVIDER_REGISTRY.items():
                    p = cls()
                    models = p.get_available_models()
                    self._write_system(f"  {name}: {', '.join(models[:4])}")
            elif parts[1] == "set" and len(parts) >= 3:
                val = parts[2]
                if "/" in val:
                    pname, model = val.split("/", 1)
                else:
                    pname = val
                    model = parts[3] if len(parts) > 3 else None
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

        if command == "key":
            if len(parts) < 2:
                self._write_error("Usage: /key <api_key>")
                return
            api_key = parts[1]
            cfg = load_config()
            cfg["api_key"] = api_key
            save_config(cfg)
            self._write_system("API key saved. Run /model set <provider>/<model> to activate")
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

        self._write_error(f"Unknown command: {command}. Type /help")

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
        approval_policy=args.policy,
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
