"""Runtime configuration for the stateless pet AI endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit


def _search_db_path() -> str:
    root = Path(os.environ.get("SLEEPY_DATA_DIR") or Path(__file__).resolve().parents[1]).expanduser().resolve()
    path = Path(os.environ.get("SLEEPY_AI_SEARCH_DB") or "pet_search.sqlite3").expanduser()
    return str((path if path.is_absolute() else root / path).resolve())


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
    model_auto: bool = True
    search_api_key: str = ""
    search_timeout_seconds: int = 6
    search_max_results: int = 3
    search_cache_ttl_seconds: int = 21600
    search_cache_max_entries: int = 1024
    search_db_path: str = field(default_factory=_search_db_path)

    @property
    def official_endpoint(self) -> bool:
        try:
            endpoint = urlsplit(self.endpoint)
            official = (endpoint.scheme == "https" and endpoint.hostname == "api.deepseek.com"
                        and endpoint.port in {None, 443} and not endpoint.username and not endpoint.password)
        except ValueError:
            official = False
        return official

    @property
    def effective_search_api_key(self) -> str:
        # A third-party model proxy's secret must never be sent to DeepSeek.
        return self.search_api_key or (self.api_key if self.official_endpoint else "")

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
            model=os.environ.get("SLEEPY_AI_MODEL", "deepseek-flash").strip(),
            timeout_seconds=_int_env("SLEEPY_AI_TIMEOUT", 12, 3, 30),
            max_output_tokens=_int_env("SLEEPY_AI_MAX_TOKENS", 180, 64, 512),
            search_enabled=_bool_env("SLEEPY_AI_SEARCH_ENABLED", True),
            minute_limit=_int_env("SLEEPY_AI_MINUTE_LIMIT", 4, 1, 60),
            daily_ip_limit=_int_env("SLEEPY_AI_DAILY_IP_LIMIT", 40, 1, 1000),
            daily_global_limit=_int_env("SLEEPY_AI_DAILY_GLOBAL_LIMIT", 500, 1, 100000),
            daily_search_limit=_int_env("SLEEPY_AI_DAILY_SEARCH_LIMIT", 100, 0, 10000),
            thinking_enabled=_bool_env("SLEEPY_AI_THINKING_ENABLED", False),
            model_auto=_bool_env("SLEEPY_AI_MODEL_AUTO", True),
            search_api_key=os.environ.get("SLEEPY_AI_SEARCH_API_KEY", "").strip(),
            search_timeout_seconds=_int_env("SLEEPY_AI_SEARCH_TIMEOUT", 6, 2, 15),
            search_max_results=_int_env("SLEEPY_AI_SEARCH_MAX_RESULTS", 3, 1, 5),
            search_cache_ttl_seconds=_int_env("SLEEPY_AI_SEARCH_CACHE_TTL", 21600, 60, 86400),
            search_cache_max_entries=_int_env("SLEEPY_AI_SEARCH_CACHE_MAX_ENTRIES", 1024, 1, 10000),
            search_db_path=_search_db_path(),
        )
