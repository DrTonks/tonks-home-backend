"""Strict request validation for untrusted browser payloads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ALLOWED_PETS = {"static", "live2d"}


class PetAIRequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PetAIRequest:
    pet_id: str
    question_id: str
    answer: str
    context: dict[str, Any]
    question: dict[str, Any]


def load_questions(path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = path or BASE_DIR / "questions.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("pet AI questions config must be an object")
    return data


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PetAIRequestError("invalid_field", f"{field} must be a string")
    value = CONTROL_RE.sub("", value).strip()
    if required and not value:
        raise PetAIRequestError("missing_field", f"{field} is required")
    if len(value) > maximum:
        raise PetAIRequestError("field_too_long", f"{field} is too long")
    return value


def _weather(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise PetAIRequestError("invalid_field", "context.weather must be an object")
    desc = _text(value.get("desc"), "context.weather.desc", 24)
    raw_temp = value.get("temp")
    temp: int | None = None
    if raw_temp is not None:
        if isinstance(raw_temp, bool):
            raise PetAIRequestError("invalid_field", "context.weather.temp must be a number")
        try:
            temp = int(raw_temp)
        except (TypeError, ValueError) as exc:
            raise PetAIRequestError("invalid_field", "context.weather.temp must be a number") from exc
        if temp < -80 or temp > 80:
            raise PetAIRequestError("invalid_field", "context.weather.temp is out of range")
    result: dict[str, Any] = {}
    if desc:
        result["desc"] = desc
    if temp is not None:
        result["temp"] = temp
    return result or None


def validate_payload(
    payload: Any,
    questions: dict[str, dict[str, Any]] | None = None,
) -> PetAIRequest:
    if not isinstance(payload, dict):
        raise PetAIRequestError("invalid_body", "expected a JSON object")

    pet_id = _text(payload.get("pet_id"), "pet_id", 16, required=True)
    if pet_id not in ALLOWED_PETS:
        raise PetAIRequestError("unknown_pet", "unknown pet_id")

    question_id = _text(payload.get("question_id"), "question_id", 40, required=True)
    question_map = questions or load_questions()
    question = question_map.get(question_id)
    if not isinstance(question, dict):
        raise PetAIRequestError("unknown_question", "question_id is not enabled")

    answer = _text(payload.get("answer"), "answer", 100, required=True)
    raw_context = payload.get("context", {})
    if raw_context is None:
        raw_context = {}
    if not isinstance(raw_context, dict):
        raise PetAIRequestError("invalid_field", "context must be an object")

    allowed = set(question.get("context_fields", []))
    context: dict[str, Any] = {}
    if "previous_answer" in allowed:
        previous = _text(raw_context.get("previous_answer"), "context.previous_answer", 100)
        if previous:
            context["previous_answer"] = previous
    if "user_name" in allowed:
        name = _text(raw_context.get("user_name"), "context.user_name", 30)
        if name:
            context["user_name"] = name
    if "city" in allowed:
        city = _text(raw_context.get("city"), "context.city", 30)
        if city:
            context["city"] = city
    if "weather" in allowed:
        weather = _weather(raw_context.get("weather"))
        if weather:
            context["weather"] = weather

    return PetAIRequest(
        pet_id=pet_id,
        question_id=question_id,
        answer=answer,
        context=context,
        question=question,
    )
