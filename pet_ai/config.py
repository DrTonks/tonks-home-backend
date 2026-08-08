"""Runtime configuration for the stateless pet AI endpoint."""

from __future__ import annotations

from dataclasses import dataclass
import os


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PetAIConfig:
    api_key: str
    endpoint: str
    model: str
    timeout_seconds: int
    max_output_tokens: int
    search_enabled: bool
    minute_limit: int
    daily_ip_limit: int
    daily_global_limit: int
    daily_search_limit: int
    thinking_enabled: bool = False

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.endpoint and self.model)

    @classmethod
    def from_env(cls) -> "PetAIConfig":
        return cls(
            api_key=os.environ.get("SLEEPY_AI_API_KEY", "").strip(),
            endpoint=os.environ.get(
                "SLEEPY_AI_ENDPOINT",
                "https://api.deepseek.com/chat/completions",
            ).strip(),
            model=os.environ.get("SLEEPY_AI_MODEL", "deepseek-v4-flash").strip(),
            timeout_seconds=_int_env("SLEEPY_AI_TIMEOUT", 12, 3, 30),
            max_output_tokens=_int_env("SLEEPY_AI_MAX_TOKENS", 180, 64, 512),
            search_enabled=_bool_env("SLEEPY_AI_SEARCH_ENABLED", True),
            minute_limit=_int_env("SLEEPY_AI_MINUTE_LIMIT", 4, 1, 60),
            daily_ip_limit=_int_env("SLEEPY_AI_DAILY_IP_LIMIT", 40, 1, 1000),
            daily_global_limit=_int_env("SLEEPY_AI_DAILY_GLOBAL_LIMIT", 500, 1, 100000),
            daily_search_limit=_int_env("SLEEPY_AI_DAILY_SEARCH_LIMIT", 100, 0, 10000),
            thinking_enabled=_bool_env("SLEEPY_AI_THINKING_ENABLED", False),
        )
