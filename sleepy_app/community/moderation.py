"""Independent LLM moderation for public blog comments."""

from __future__ import annotations
from sleepy_app.config import PROJECT_ROOT

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any

from pet_ai.config import PetAIConfig
from pet_ai.provider import OpenAICompatibleProvider, ProviderError


PROMPT_PATH = (PROJECT_ROOT / "comment_moderation_prompt.md")


@dataclass(frozen=True)
class ModerationResult:
    decision: str
    reason: str
    category: str


class CommentModerationService:
    def __init__(
        self,
        config: PetAIConfig | None = None,
        *,
        provider: Any | None = None,
        prompt_path: Path | None = None,
    ):
        self.config = config or PetAIConfig.from_env()
        self.provider = provider or OpenAICompatibleProvider(self.config)
        self.prompt_path = prompt_path or PROMPT_PATH

    def moderate(
        self,
        *,
        page: str,
        nickname: str,
        content: str,
        reply_to_name: str,
        history: list[dict[str, Any]],
    ) -> ModerationResult:
        payload = self._history_payload(
            page=page,
            nickname=nickname,
            content=content,
            reply_to_name=reply_to_name,
            history=history,
        )
        messages = [
            {"role": "system", "content": self.prompt_path.read_text(encoding="utf-8")},
            {
                "role": "user",
                "content": "<comment_data>\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n</comment_data>",
            },
        ]
        try:
            message = self.provider.chat(
                messages,
                temperature=0.0,
                max_tokens=180,
            )
            return self._parse(message.get("content", ""))
        except ProviderError as exc:
            return ModerationResult("review", exc.code, "moderation_unavailable")
        except (OSError, UnicodeError, ValueError, TypeError):
            return ModerationResult("review", "moderation_failed", "moderation_unavailable")

    @staticmethod
    def _parse(raw: Any) -> ModerationResult:
        if not isinstance(raw, str):
            raise ValueError("moderation response is not text")
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("moderation response is not an object")
        decision = str(data.get("decision", "")).strip().lower()
        if decision not in {"allow", "reject", "review"}:
            raise ValueError("invalid moderation decision")
        reason = str(data.get("reason", "")).strip()[:300]
        category = str(data.get("category", "other")).strip().lower()[:40]
        return ModerationResult(decision, reason, category)

    @staticmethod
    def _history_payload(
        *,
        page: str,
        nickname: str,
        content: str,
        reply_to_name: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            maximum_chars = int(os.environ.get("SLEEPY_COMMENT_HISTORY_CHARS", "12000"))
        except ValueError:
            maximum_chars = 12000
        maximum_chars = max(2000, min(50000, maximum_chars))

        serialized = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= maximum_chars:
            history_payload: dict[str, Any] = {
                "total": len(history),
                "truncated": False,
                "items": history,
            }
        else:
            recent: list[dict[str, Any]] = []
            used = 0
            for item in reversed(history):
                item_size = len(json.dumps(item, ensure_ascii=False))
                if recent and used + item_size > maximum_chars:
                    break
                recent.append(item)
                used += item_size
            recent.reverse()
            history_payload = {
                "total": len(history),
                "truncated": True,
                "older_count": len(history) - len(recent),
                "older_status_counts": {
                    status: sum(1 for item in history[:-len(recent)] if item.get("status") == status)
                    for status in {"published", "pending", "rejected"}
                },
                "items": recent,
            }

        return {
            "commenter": nickname,
            "page": page,
            "reply_to": reply_to_name,
            "new_content": content,
            "same_email_history": history_payload,
        }
