"""Small, trusted reaction vocabulary shared with the homepage build."""
from __future__ import annotations
import json
from pathlib import Path
import re
from typing import Any

CATALOG = json.loads((Path(__file__).parent / 'reactions.json').read_text(encoding='utf-8'))['items']

def choices(pet_id: str) -> list[dict[str, str]]:
    return [{"id": item["id"], "meaning": item["description"]} for item in CATALOG if pet_id in item["pets"]]

def parse_reply(content: str, pet_id: str) -> tuple[str, str | None]:
    """Accept legacy prose; never send malformed JSON or untrusted asset URLs to the UI."""
    value = content.strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*', '', value, flags=re.I)
        value = re.sub(r'\s*```$', '', value).strip()
    try:
        data: Any = json.loads(value)
    except (ValueError, TypeError):
        # A broken structured reply must use the existing local fallback, not display raw JSON.
        return ('', None) if value.startswith(('{', '[')) else (value, None)
    if isinstance(data, str):
        return data, None
    if not isinstance(data, dict) or not isinstance(data.get('reply'), str):
        return '', None
    emoji = data.get('emoji_id')
    allowed = {item['id'] for item in choices(pet_id)}
    return data['reply'], emoji if isinstance(emoji, str) and emoji in allowed else None
