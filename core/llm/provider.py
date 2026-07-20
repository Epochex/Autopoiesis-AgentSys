from __future__ import annotations

import copy
import json
from typing import Protocol
from urllib import error, request

from core.env import autopoiesis_env


class JsonLLMClient(Protocol):
    """Anything that can turn chat messages into one JSON object."""

    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
        ...


class LLMConfigurationError(RuntimeError):
    """LLM mode was requested but required configuration is absent."""


class LLMResponseError(RuntimeError):
    """The provider replied, but not with a usable JSON completion."""


class OpenAICompatibleClient:
    """Minimal stdlib client for any OpenAI-compatible /chat/completions endpoint.

    Configuration uses AUTOPOIESIS_LLM_BASE_URL / AUTOPOIESIS_LLM_API_KEY /
    AUTOPOIESIS_LLM_MODEL; raises LLMConfigurationError when incomplete.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_sec: int = 30,
    ):
        self.base_url = (base_url or autopoiesis_env("LLM_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or autopoiesis_env("LLM_API_KEY")
        self.model = model or autopoiesis_env("LLM_MODEL")
        self.timeout_sec = timeout_sec
        if not self.base_url or not self.api_key or not self.model:
            raise LLMConfigurationError(
                "LLM mode requires AUTOPOIESIS_LLM_BASE_URL, "
                "AUTOPOIESIS_LLM_API_KEY, and AUTOPOIESIS_LLM_MODEL"
            )

    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
        """POST `messages` and return the completion parsed as one JSON object.

        Raises ValueError on empty `messages`, LLMResponseError on transport
        failure, a malformed completion envelope, or non-JSON content.
        """
        if not messages:
            raise ValueError("messages must not be empty")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"Return only JSON for schema {schema_name}. Do not include markdown.",
                },
                *messages,
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMResponseError(f"LLM request failed: HTTP {exc.code} for schema {schema_name}: {detail}") from exc
        except error.URLError as exc:
            raise LLMResponseError(f"LLM endpoint unreachable: {exc.reason}") from exc

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"malformed completion envelope for schema {schema_name}: {raw[:500]}") from exc
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"completion content is not valid JSON for schema {schema_name}: {content[:500]}") from exc
        if not isinstance(parsed, dict):
            raise LLMResponseError(f"completion for schema {schema_name} is JSON but not an object: {content[:500]}")
        return parsed


class StaticJsonLLMClient:
    """Test double only; never use for real held-out metrics."""

    def __init__(self, response: dict):
        self.response = response

    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
        return copy.deepcopy(self.response)
