from __future__ import annotations

import json
import os
from typing import Protocol
from urllib import request


class JsonLLMClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
        ...


class LLMConfigurationError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_sec: int = 30,
    ):
        self.base_url = (base_url or os.getenv("SELFEVO_LLM_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("SELFEVO_LLM_API_KEY")
        self.model = model or os.getenv("SELFEVO_LLM_MODEL")
        self.timeout_sec = timeout_sec
        if not self.base_url or not self.api_key or not self.model:
            raise LLMConfigurationError("LLM mode requires SELFEVO_LLM_BASE_URL, SELFEVO_LLM_API_KEY, and SELFEVO_LLM_MODEL")

    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
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
        with request.urlopen(req, timeout=self.timeout_sec) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


class StaticJsonLLMClient:
    """Test double only; never use for real held-out metrics."""

    def __init__(self, response: dict):
        self.response = response

    def complete_json(self, messages: list[dict[str, str]], *, schema_name: str) -> dict:
        return dict(self.response)
