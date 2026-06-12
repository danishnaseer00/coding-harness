import json
import os
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_DIR = Path.home() / ".coding-harness"
CONFIG_PATH = CONFIG_DIR / "config.json"


async def retry_with_backoff(coro, max_retries=3, initial_delay=1.0, backoff=2.0):
    """Retry async operation with exponential backoff for 503/rate-limit errors."""
    for attempt in range(max_retries):
        try:
            return await coro()
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = any(x in err_str for x in ["503", "overloaded", "rate limit", "rate_limit", "too many requests"])
            
            if not is_rate_limit or attempt == max_retries - 1:
                raise
            
            delay = initial_delay * (backoff ** attempt)
            import sys
            print(f"⏳ Retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})...", file=sys.stderr)
            await asyncio.sleep(delay)


@dataclass
class ContentBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None


@dataclass
class ProviderResponse:
    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"

    @property
    def has_tool_calls(self) -> bool:
        return any(b.type == "tool_use" for b in self.content)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if b.type == "text" and b.text)


class BaseProvider(ABC):
    name: str = ""

    @abstractmethod
    async def send(self, messages: list, system_prompt: str, tools: list) -> ProviderResponse:
        ...

    @abstractmethod
    def get_available_models(self) -> list[str]:
        ...


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-20250514", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def send(self, messages: list, system_prompt: str, tools: list) -> ProviderResponse:
        api_tools = []
        for t in tools:
            api_tools.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"]
            })

        api_messages = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            
            # Handle list content (text + tool_use or tool_result blocks)
            if isinstance(content, list):
                # Validate that all items have required type field
                if all(isinstance(c, dict) and "type" in c for c in content):
                    api_messages.append(m)
                else:
                    # Fallback: stringify if not valid blocks
                    api_messages.append({"role": role, "content": str(content)})
            else:
                # String content
                api_messages.append({"role": role, "content": str(content)})

        try:
            response = await retry_with_backoff(
                coro=lambda: self.client.messages.create(
                    model=self.model,
                    max_tokens=8192,
                    system=system_prompt,
                    tools=api_tools,
                    messages=api_messages
                ),
                max_retries=3
            )
        except Exception as e:
            # Log the problematic message for debugging
            import sys
            print(f"\n❌ Anthropic API Error: {e}", file=sys.stderr)
            print(f"   Messages sent: {len(api_messages)} messages", file=sys.stderr)
            if api_messages and isinstance(api_messages[-1].get("content"), list):
                print(f"   Last message has tool blocks: {[b.get('type') for b in api_messages[-1]['content']]}", file=sys.stderr)
            raise

        blocks = []
        for block in response.content:
            if block.type == "text":
                blocks.append(ContentBlock(type="text", text=block.text))
            elif block.type == "tool_use":
                blocks.append(ContentBlock(
                    type="tool_use", id=block.id,
                    name=block.name, input=block.input
                ))

        return ProviderResponse(content=blocks, stop_reason=response.stop_reason)

    def get_available_models(self) -> list[str]:
        return [
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ]


class OpenAICompatibleProvider(BaseProvider):
    name = ""

    def __init__(self, model: str = "gpt-4o", api_key: str | None = None,
                 base_url: str | None = None, name: str = "openai-compat"):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url
        self.name = name
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def send(self, messages: list, system_prompt: str, tools: list) -> ProviderResponse:
        api_tools = []
        for t in tools:
            api_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"]
                }
            })

        api_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_use":
                            tool_calls.append({
                                "id": item.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": json.dumps(item.get("input", {}))
                                }
                            })
                msg = {"role": "assistant"}
                if tool_calls:
                    msg["content"] = "\n".join(text_parts) if text_parts else None
                    msg["tool_calls"] = tool_calls
                else:
                    msg["content"] = "\n".join(text_parts) if text_parts else ""
                api_messages.append(msg)

            elif role == "user" and isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_call_id = item.get("tool_use_id") or item.get("id", "")
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": str(item.get("content", ""))
                        })

            else:
                api_messages.append({"role": role, "content": str(content)})

        kwargs = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": api_messages,
        }
        if api_tools:
            kwargs["tools"] = api_tools

        try:
            response = await retry_with_backoff(
                coro=lambda: self.client.chat.completions.create(**kwargs),
                max_retries=3
            )
        except Exception as e:
            err = str(e)
            
            # Log debugging info
            import sys
            print(f"\n❌ OpenAI-Compatible API Error: {err[:200]}", file=sys.stderr)
            print(f"   Messages: {len(api_messages)} total", file=sys.stderr)
            if api_messages:
                print(f"   Message types: {[m.get('role') for m in api_messages]}", file=sys.stderr)
                last = api_messages[-1]
                if last.get("role") == "tool":
                    print(f"   Last msg: tool result (id={last.get('tool_call_id')})", file=sys.stderr)
                elif isinstance(last.get("content"), list):
                    print(f"   Last msg: user with blocks {[b.get('type') for b in last['content']]}", file=sys.stderr)
            
            if "tool_use_failed" in err:
                kwargs_no_tools = {k: v for k, v in kwargs.items() if k != "tools"}
                try:
                    retry = await self.client.chat.completions.create(**kwargs_no_tools)
                    text = retry.choices[0].message.content or ""
                    return ProviderResponse(
                        content=[ContentBlock(type="text", text=text)],
                        stop_reason="end_turn"
                    )
                except Exception:
                    return ProviderResponse(
                        content=[ContentBlock(type="text", text=f"(tool call failed, try a simpler request)")],
                        stop_reason="end_turn"
                    )
            return ProviderResponse(
                content=[ContentBlock(type="text", text=f"provider error: {err[:300]}")],
                stop_reason="end_turn"
            )

        msg = response.choices[0].message
        blocks = []

        if msg.content:
            blocks.append(ContentBlock(type="text", text=msg.content))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                blocks.append(ContentBlock(
                    type="tool_use", id=tc.id,
                    name=tc.function.name, input=args
                ))

        stop = "end_turn"
        if msg.tool_calls:
            stop = "tool_use"
        elif msg.content and response.choices[0].finish_reason == "length":
            stop = "max_tokens"

        return ProviderResponse(content=blocks, stop_reason=stop)

    def get_available_models(self) -> list[str]:
        return [
            "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo",
        ]


class GroqProvider(OpenAICompatibleProvider):
    def __init__(self, model: str = "llama-3.3-70b-versatile",
                 api_key: str | None = None):
        super().__init__(
            model=model,
            api_key=api_key or os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
            name="groq"
        )

    def get_available_models(self) -> list[str]:
        return [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ]


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(self, model: str = "minimax/minimax-m3",
                 api_key: str | None = None):
        super().__init__(
            model=model,
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
            name="openrouter"
        )

    def get_available_models(self) -> list[str]:
        return [
            "minimax/minimax-m3",
            "anthropic/claude-sonnet-4",
            "openai/gpt-4o",
            "google/gemini-2.0-flash-001",
            "meta-llama/llama-3.3-70b-instruct",
            "deepseek/deepseek-chat",
        ]


class TokenRouterProvider(OpenAICompatibleProvider):
    def __init__(self, model: str = "MiniMax-M3",
                 api_key: str | None = None):
        super().__init__(
            model=model,
            api_key=api_key or os.environ.get("TOKENROUTER_API_KEY", ""),
            base_url="https://api.tokenrouter.com/v1",
            name="tokenrouter"
        )

    def get_available_models(self) -> list[str]:
        return [
            "MiniMax-M3",
        ]


class OllamaProvider(BaseProvider):
    name = "ollama"

    def __init__(self, model: str = "llama3.2", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=f"{self.base_url}/v1",
                api_key="ollama"
            )
        return self._client

    async def send(self, messages: list, system_prompt: str, tools: list) -> ProviderResponse:
        api_tools = []
        for t in tools:
            api_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"]
                }
            })

        api_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_use":
                            tool_calls.append({
                                "id": item.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": json.dumps(item.get("input", {}))
                                }
                            })
                msg = {"role": "assistant"}
                if tool_calls:
                    msg["content"] = "\n".join(text_parts) if text_parts else None
                    msg["tool_calls"] = tool_calls
                else:
                    msg["content"] = "\n".join(text_parts) if text_parts else ""
                api_messages.append(msg)

            elif role == "user" and isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_call_id = item.get("tool_use_id") or item.get("id", "")
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": str(item.get("content", ""))
                        })

            else:
                api_messages.append({"role": role, "content": str(content)})

        kwargs = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": api_messages,
        }
        if api_tools:
            kwargs["tools"] = api_tools

        try:
            response = await retry_with_backoff(
                coro=lambda: self.client.chat.completions.create(**kwargs),
                max_retries=3
            )
        except Exception as e:
            import sys
            print(f"\n❌ Ollama API Error: {e}", file=sys.stderr)
            print(f"   Messages: {len(api_messages)} total", file=sys.stderr)
            return ProviderResponse(
                content=[ContentBlock(type="text", text=f"error: ollama request failed: {e}")],
                stop_reason="end_turn"
            )

        msg = response.choices[0].message
        blocks = []

        if msg.content:
            blocks.append(ContentBlock(type="text", text=msg.content))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                blocks.append(ContentBlock(
                    type="tool_use", id=tc.id,
                    name=tc.function.name, input=args
                ))

        stop = "end_turn"
        if msg.tool_calls:
            stop = "tool_use"

        return ProviderResponse(content=blocks, stop_reason=stop)

    def get_available_models(self) -> list[str]:
        try:
            import httpx
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1)
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return ["llama3.2", "llama3.1", "mistral", "codellama", "qwen2.5-coder"]


PROVIDER_REGISTRY = {
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatibleProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
    "tokenrouter": TokenRouterProvider,
    "ollama": OllamaProvider,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def create_provider(provider_name: str, model: str | None = None, api_key: str | None = None) -> BaseProvider:
    cls = PROVIDER_REGISTRY.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown provider: {provider_name}. Available: {list(PROVIDER_REGISTRY.keys())}")
    provider = cls(api_key=api_key) if api_key else cls()
    if model:
        provider.model = model
    return provider


def get_default_provider() -> BaseProvider:
    cfg = load_config()
    # Check env var first, then config file, then default to tokenrouter
    pname = os.environ.get("CODING_HARNESS_PROVIDER") or cfg.get("provider", "tokenrouter")
    model = os.environ.get("CODING_HARNESS_MODEL") or cfg.get("model")
    api_key = os.environ.get("CODING_HARNESS_API_KEY") or cfg.get("api_key")
    return create_provider(pname, model, api_key)
