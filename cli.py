import argparse
import asyncio
import re
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import FuzzyWordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent import Agent
from memory import SessionStore
from providers import PROVIDER_REGISTRY, create_provider, load_config, save_config


console = Console()
HISTORY_PATH = Path.home() / ".coding-harness" / "history"

SLASH_COMMANDS = ["/help", "/exit", "/model", "/providers", "/clear", "/new", "/resume", "/sessions"]

SLASH_HELP = {
    "/help": "Show all commands",
    "/model": "Show or change model",
    "/providers": "List available providers",
    "/clear": "Clear conversation history",
    "/new": "Start a fresh session (previous session is saved)",
    "/resume [id]": "Resume a session (or /resume to pick from list)",
    "/sessions": "List sessions for this project",
    "/exit": "Exit the application",
}

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


def print_user(text: str):
    console.print(Panel(Text(text, style="bold"), title="you", border_style="blue"))


def print_tool_call(name: str, args: dict):
    args_str = " ".join(f"{k}={v}" for k, v in args.items())
    console.print(f"  [bold cyan]⚙️  {name}({args_str})[/bold cyan]")


def print_tool_result(result: str, duration: float = 0):
    lines = result.strip().split("\n")[:3]
    preview = "\n    ".join(lines)
    dur = f" ({duration:.1f}s)" if duration else ""
    console.print(f"  [dim green]└─ {preview}{dur}[/dim green]")


def print_system(msg: str):
    for line in msg.split("\n"):
        console.print(f"  [dim]■ {line}[/dim]")


def print_error(msg: str):
    console.print(f"  [bold red]x {msg}[/bold red]")


def print_slash_menu():
    table = Table(title="Slash Commands", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="bold yellow")
    table.add_column("Description", style="dim")
    for cmd, desc in SLASH_HELP.items():
        table.add_row(cmd, desc)
    console.print(table)


class Harness:
    def __init__(self, agent: Agent, session_store: SessionStore):
        self.agent = agent
        self.session_store = session_store
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            completer=FuzzyWordCompleter(SLASH_COMMANDS),
        )

    async def run(self):
        console.print(f"[dim]Session: {self.session_store.session_id}[/dim]")
        console.print(f"[dim]Provider: {self.agent.provider.name} | Model: {self.agent.provider.model}[/dim]")
        console.print(f"[dim]Working directory: {self.agent.cwd}[/dim]")
        console.print("[dim]────────────────────────────────────────[/dim]")

        while True:
            try:
                text = await self.session.prompt_async("> ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            text = text.strip()
            if not text:
                continue

            if text.startswith("/"):
                try:
                    await self._handle_slash(text)
                except EOFError:
                    break
                continue

            provider = detect_provider_from_key(text)
            if provider:
                cfg = load_config()
                cfg["api_key"] = text
                cfg["provider"] = provider
                save_config(cfg)
                self.agent.provider = create_provider(provider, cfg.get("model"), text)
                print_system(f"API key saved — switched to {provider}")
                continue

            print_user(text)

            buf = ""
            thinking_shown = False

            def on_thinking():
                nonlocal thinking_shown
                thinking_shown = True
                console.print("[bold yellow]Thinking...[/bold yellow]", end="")

            def on_stream_text(token: str):
                nonlocal buf, thinking_shown
                if thinking_shown:
                    console.print()
                    thinking_shown = False
                buf += token
                console.print(token, end="")

            def on_assistant_text(full_text: str):
                nonlocal thinking_shown
                if thinking_shown:
                    console.print("[bold yellow]Thinking...[/bold yellow]")
                    thinking_shown = False

            def on_tool_call(name: str, args: dict):
                nonlocal buf, thinking_shown
                if thinking_shown:
                    console.print("[bold yellow]Thinking...[/bold yellow]")
                    thinking_shown = False
                if buf:
                    console.print()
                    buf = ""
                print_tool_call(name, args)

            def on_tool_result(name: str, result: str, duration: float):
                print_tool_result(result, duration)

            def on_error(msg: str):
                nonlocal thinking_shown
                if thinking_shown:
                    console.print("[bold yellow]Thinking...[/bold yellow]")
                    thinking_shown = False
                print_error(msg)

            self.agent._callbacks = {
                "thinking": on_thinking,
                "stream_text": on_stream_text,
                "assistant_text": on_assistant_text,
                "tool_call": on_tool_call,
                "tool_result": on_tool_result,
                "error": on_error,
            }

            try:
                await self.agent.run(text)
                console.print()
            except Exception as e:
                print_error(f"Error: {e}")
                console.print()

    async def _handle_slash(self, cmd: str):
        parts = cmd.strip().lstrip("/").split()

        if not parts or not parts[0]:
            print_slash_menu()
            return

        command = parts[0].lower()

        if command in ("exit", "quit"):
            raise EOFError()

        if command == "help":
            print_slash_menu()
            print_system("Tip: just paste an API key — it auto-detects the provider")
            return

        if command == "model":
            if len(parts) == 1:
                print_system(f"Provider: {self.agent.provider.name}")
                print_system(f"Model: {self.agent.provider.model}")
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
                    print_error(f"Unknown provider: {pname}")
                    return
                try:
                    cfg = load_config()
                    self.agent.provider = create_provider(pname, model, cfg.get("api_key"))
                    cfg["provider"] = pname
                    if model:
                        cfg["model"] = model
                    save_config(cfg)
                    print_system(f"Switched to {pname}/{self.agent.provider.model}")
                except Exception as e:
                    print_error(str(e))
            return

        if command == "providers":
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
            console.print(table)
            return

        if command == "clear":
            self.agent.messages = []
            self.agent.repeat_detector.last_call = None
            console.print("[dim]■ History cleared[/dim]")
            return

        if command == "new":
            self.session_store.save()
            self.agent.messages = []
            self.agent.memory = {"task": "", "files": [], "notes": []}
            self.agent.repeat_detector.last_call = None
            new_store = SessionStore(project_path=self.agent.cwd)
            self.session_store = new_store
            self.agent.session_store = new_store
            console.clear()
            print_system(f"New session started: {new_store.session_id}")
            return

        if command == "resume":
            sid = parts[1] if len(parts) > 1 else ""
            if not sid:
                sessions = SessionStore.list_sessions(self.agent.cwd)
                if not sessions:
                    print_error("No sessions for this project")
                    return
                console.print("[bold]Sessions for this project:[/bold]")
                for i, s in enumerate(sessions, 1):
                    date = s.get("created", "")[:10]
                    print(f"  [{i}] {s['id']}  {date}  {s['summary'][:80]}")
                console.print("\n[dim]Type /resume 1 (or any number from the list) to resume that session[/dim]")
                return
            try:
                if sid.isdigit():
                    sessions = SessionStore.list_sessions(self.agent.cwd)
                    if not sessions:
                        print_error("No sessions for this project")
                        return
                    idx = int(sid) - 1
                    if idx < 0 or idx >= len(sessions):
                        print_error(f"Invalid number. Pick 1-{len(sessions)}")
                        return
                    sid = sessions[idx]["id"]
                store = SessionStore.resume(sid)
                self.agent.messages = store.data.get("history", [])
                self.agent.memory = store.data.get("memory", {"task": "", "files": [], "notes": []})
                self.agent.repeat_detector.last_call = None
                console.clear()
                print_system(f"Resumed session {store.session_id} ({len(self.agent.messages)} turns)")
            except FileNotFoundError:
                print_error(f"Session not found: {sid}")
            return

        if command == "sessions":
            sessions = SessionStore.list_sessions(self.agent.cwd)
            if not sessions:
                print_system("No sessions for this project")
                return
            table = Table(title=f"Sessions for {Path(self.agent.cwd).name}", show_header=True)
            table.add_column("#", style="bold")
            table.add_column("ID", style="dim")
            table.add_column("Date", style="cyan")
            table.add_column("Turns", style="yellow")
            table.add_column("Summary", style="white")
            for i, s in enumerate(sessions, 1):
                table.add_row(str(i), s["id"], s.get("created", "")[:10], str(s["turns"]), s["summary"][:60])
            console.print(table)
            return

        print_error(f"Unknown: {command}. Try /help")


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
            if args.resume == "latest":
                session_store = SessionStore.resume("latest", project_path=args.cwd)
            else:
                session_store = SessionStore.resume(args.resume)
        except FileNotFoundError:
            console.print(f"[yellow]Session not found: {args.resume}. Starting fresh.[/yellow]")
            session_store = SessionStore(project_path=args.cwd)
    else:
        session_store = SessionStore(project_path=args.cwd)

    agent = Agent(
        cwd=args.cwd,
        approval_policy="auto",
        session_store=session_store,
    )

    if args.resume:
        try:
            agent.messages = session_store.data.get("history", [])
            agent.memory = session_store.data.get("memory", {"task": "", "files": [], "notes": []})
        except Exception:
            pass

    harness = Harness(agent=agent, session_store=session_store)

    try:
        asyncio.run(harness.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
