import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_DIR = Path.home() / ".coding-harness"
CONFIG_PATH = CONFIG_DIR / "config.json"


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
            if isinstance(m.get("content"), list) and all(
                isinstance(c, dict) and "type" in c for c in m["content"]
            ):
                api_messages.append(m)
            else:
                content = m.get("content", "")
                if isinstance(content, list):
                    api_messages.append(m)
                else:
                    api_messages.append({"role": m["role"], "content": str(content)})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=system_prompt,
            tools=api_tools,
            messages=api_messages
        )

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
            content = m.get("content", "")
            role = m.get("role", "user")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": item.get("content", "")
                        })
                    else:
                        api_messages.append({"role": role, "content": str(content)})
                        break
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
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            err = str(e)
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
            content = m.get("content", "")
            role = m.get("role", "user")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": item.get("content", "")
                        })
                    else:
                        api_messages.append({"role": role, "content": str(content)})
                        break
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
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
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
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
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
    pname = cfg.get("provider", "tokenrouter")
    model = cfg.get("model")
    api_key = cfg.get("api_key")
    return create_provider(pname, model, api_key)
