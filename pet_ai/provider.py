"""Minimal OpenAI-compatible Chat Completions client using the standard library."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import PetAIConfig


class ProviderError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OpenAICompatibleProvider:
    def __init__(self, config: PetAIConfig):
        self.config = config

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.config.available:
            raise ProviderError("not_configured")

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": self.config.max_output_tokens,
        }
        if self.config.endpoint.startswith("https://api.deepseek.com/"):
            payload["thinking"] = {
                "type": "enabled" if self.config.thinking_enabled else "disabled"
            }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        request = urllib.request.Request(
            self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "SleepyPetAI/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ProviderError("response_too_large")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ProviderError("provider_rate_limited") from exc
            if exc.code in {401, 403}:
                raise ProviderError("provider_auth_failed") from exc
            raise ProviderError("provider_http_error") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("provider_unavailable") from exc

        try:
            data = json.loads(raw.decode("utf-8"))
            message = data["choices"][0]["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("invalid_provider_response") from exc
        if not isinstance(message, dict):
            raise ProviderError("invalid_provider_response")
        return message
