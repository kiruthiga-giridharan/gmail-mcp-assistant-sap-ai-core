from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Sequence

import requests
from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

AUTH_URL = os.getenv("AUTH_URL", "").strip()
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

ORCHESTRATION_DEPLOYMENT = os.getenv(
    "ORCHESTRATION_DEPLOYMENT",
    "",
).strip()

MODEL_CONFIGS = {
    "claude": {
        "deployment_id": ORCHESTRATION_DEPLOYMENT,
        "provider": "claude",
        "model_name": os.getenv(
            "CLAUDE_MODEL_NAME",
            "anthropic--claude-4.6-sonnet",
        ),
    },
    "gemini": {
        "deployment_id": ORCHESTRATION_DEPLOYMENT,
        "provider": "gemini",
        "model_name": os.getenv(
            "GEMINI_MODEL_NAME",
            "gemini-3.1-flash-lite",
        ),
    },
    "gpt": {
        "deployment_id": ORCHESTRATION_DEPLOYMENT,
        "provider": "gpt",
        "model_name": os.getenv(
            "GPT_MODEL_NAME",
            "gpt-5.4",
        ),
    },
}

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, Any] = {
    "access_token": "",
    "expires_at": 0.0,
}


def _required_environment() -> None:
    missing = [
        name
        for name, value in {
            "AUTH_URL": AUTH_URL,
            "CLIENT_ID": CLIENT_ID,
            "CLIENT_SECRET": CLIENT_SECRET,
            "BASE_URL": BASE_URL,
            "ORCHESTRATION_DEPLOYMENT": ORCHESTRATION_DEPLOYMENT,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing SAP AI Core environment variables: "
            + ", ".join(missing)
        )


def get_oauth_token() -> str:
    """Get and briefly cache the SAP AI Core OAuth access token."""

    _required_environment()

    now = time.time()
    cached_token = str(_TOKEN_CACHE.get("access_token", ""))
    expires_at = float(_TOKEN_CACHE.get("expires_at", 0.0))

    if cached_token and now < expires_at - 60:
        return cached_token

    with _TOKEN_LOCK:
        now = time.time()
        cached_token = str(_TOKEN_CACHE.get("access_token", ""))
        expires_at = float(_TOKEN_CACHE.get("expires_at", 0.0))

        if cached_token and now < expires_at - 60:
            return cached_token

        response = requests.post(
            AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                "SAP AI Core authentication failed with HTTP "
                f"{response.status_code}: {response.text[:1000]}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("SAP AI Core authentication returned no access_token.")

        expires_in = int(payload.get("expires_in", 600))
        _TOKEN_CACHE["access_token"] = token
        _TOKEN_CACHE["expires_at"] = now + max(expires_in, 120)
        return str(token)


def _content_to_text(content: Any) -> str:
    """Convert LangChain content blocks into the text expected by orchestration."""

    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text is not None:
                    parts.append(str(text))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)

    return str(content)


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"value": value}

    return {}


class SAPAICoreChatModel(BaseChatModel):
    """LangChain chat-model adapter for SAP AI Core Orchestration."""

    deployment_id: str
    provider: str
    model_name: str
    max_tokens: int = 2048
    temperature: float = 0.0
    request_timeout: int = 120

    _bound_tools: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "sap_ai_core_orchestration"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "provider": self.provider,
            "model_name": self.model_name,
        }

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "SAPAICoreChatModel":
        """Return a copied model with OpenAI-compatible tool schemas attached."""

        copied = self.model_copy(deep=True)
        copied._bound_tools = [convert_to_openai_tool(tool) for tool in tools]

        # SAP orchestration accepts tool definitions. The agent decides whether to
        # call them, so tool_choice is intentionally not forced here.
        return copied

    def _orchestration_url(self) -> str:
        return (
            f"{BASE_URL}/v2/inference/deployments/"
            f"{self.deployment_id}/completion"
        )

    def _format_message(self, message: BaseMessage) -> dict[str, Any]:
        message_type = message.type

        if message_type == "system":
            return {
                "role": "system",
                "content": _content_to_text(message.content),
            }

        if message_type in {"human", "user"}:
            return {
                "role": "user",
                "content": _content_to_text(message.content),
            }

        if message_type == "tool":
            return {
                "role": "tool",
                "tool_call_id": getattr(message, "tool_call_id", ""),
                "content": _content_to_text(message.content),
            }

        if message_type == "ai":
            formatted: dict[str, Any] = {
                "role": "assistant",
                "content": _content_to_text(message.content),
            }

            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                formatted["tool_calls"] = [
                    {
                        "id": tool_call.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tool_call.get("name", ""),
                            "arguments": json.dumps(
                                _tool_arguments(tool_call.get("args", {})),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for tool_call in tool_calls
                ]

            return formatted

        # Preserve any uncommon LangChain message as a user-visible text message.
        return {
            "role": "user",
            "content": _content_to_text(message.content),
        }

    def _build_orchestration_body(
        self,
        messages: list[BaseMessage],
    ) -> dict[str, Any]:
        formatted_messages = [self._format_message(message) for message in messages]

        model_params: dict[str, Any] = {
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        templating_config: dict[str, Any] = {
            "template": formatted_messages,
        }
        if self._bound_tools:
            # SAP AI Core Orchestration expects function definitions in the
            # templating module, not inside provider model parameters.
            templating_config["tools"] = self._bound_tools

        return {
            "orchestration_config": {
                "module_configurations": {
                    "llm_module_config": {
                        "model_name": self.model_name,
                        "model_version": "latest",
                        "model_params": model_params,
                    },
                    "templating_module_config": templating_config,
                }
            },
            "input_params": {},
        }

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
        parsed_calls: list[dict[str, Any]] = []

        for item in raw_tool_calls or []:
            function = item.get("function", {})
            arguments = _tool_arguments(function.get("arguments", {}))

            parsed_calls.append(
                {
                    "id": item.get("id", ""),
                    "name": function.get("name", ""),
                    "args": arguments,
                    "type": "tool_call",
                }
            )

        return parsed_calls

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        token = get_oauth_token()
        body = self._build_orchestration_body(messages)

        response = requests.post(
            self._orchestration_url(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "AI-Resource-Group": "default",
            },
            json=body,
            timeout=self.request_timeout,
        )

        if not response.ok:
            raise RuntimeError(
                "SAP AI Core orchestration failed with HTTP "
                f"{response.status_code}: {response.text[:2000]}"
            )

        data = response.json()

        try:
            message = data["orchestration_result"]["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Unexpected SAP AI Core orchestration response: "
                + json.dumps(data, ensure_ascii=False)[:2000]
            ) from exc

        content = _content_to_text(message.get("content", ""))
        tool_calls = self._parse_tool_calls(message.get("tool_calls", []))

        ai_message = AIMessage(
            content=content,
            tool_calls=tool_calls,
        )

        return ChatResult(
            generations=[ChatGeneration(message=ai_message)],
            llm_output={
                "provider": self.provider,
                "model_name": self.model_name,
            },
        )


def get_model(model_name: str = "claude") -> SAPAICoreChatModel:
    """Return the configured SAP AI Core chat model."""

    normalized = model_name.strip().lower()
    if normalized not in MODEL_CONFIGS:
        raise ValueError(
            f"Unknown model '{model_name}'. Choose from: "
            + ", ".join(sorted(MODEL_CONFIGS))
        )

    config = MODEL_CONFIGS[normalized]

    return SAPAICoreChatModel(
        deployment_id=config["deployment_id"],
        provider=config["provider"],
        model_name=config["model_name"],
    )
