"""Load private runtime settings from a local .env without extra dependencies."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_DATA_KEYS = {
    "secret": "SLEEPY_STATUS_SECRET",
    "admin_secret": "SLEEPY_ADMIN_SECRET",
    "github_token": "SLEEPY_GITHUB_TOKEN",
}


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
        return parsed if isinstance(parsed, str) else str(parsed)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value.split(" #", 1)[0].rstrip()


def load_env_file(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Load .env next to this module; existing process variables take precedence."""
    env_path = Path(path) if path else Path(__file__).resolve().with_name(".env")
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if ENV_KEY_RE.fullmatch(key):
            os.environ.setdefault(key, _parse_value(raw_value))
    return env_path


def configured_value(data_store: Any, env_key: str, legacy_key: str, default: Any = None):
    """Prefer environment configuration while retaining legacy data.json fallback."""
    value = os.environ.get(env_key)
    if value is not None and value != "":
        return value
    legacy = data_store.dget(legacy_key)
    return default if legacy is None else legacy


def migrate_sensitive_data_keys(data_store: Any) -> list[str]:
    """Remove legacy secret copies only when a non-empty environment replacement exists."""
    migrated = [
        data_key
        for data_key, env_key in SENSITIVE_DATA_KEYS.items()
        if os.environ.get(env_key) and data_key in data_store.data
    ]
    if not migrated:
        return []
    for data_key in migrated:
        data_store.data.pop(data_key, None)
    data_store.save()
    return migrated
