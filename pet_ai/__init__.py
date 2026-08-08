"""Flask Blueprint for the public stateless pet reply endpoint."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from .config import PetAIConfig
from .provider import ProviderError
from .rate_limit import RateLimitExceeded
from .service import PetAIService
from .validation import PetAIRequestError, load_questions, validate_payload


MAX_BODY_BYTES = 4096


def _json_error(code: str, status: int) -> tuple[Response, int]:
    return jsonify({"success": False, "code": code}), status


def _rate_limit_keys() -> tuple[str, str]:
    client_id = request.headers.get("X-Client-ID", "").strip()[:80]
    remote = request.remote_addr or "unknown"
    salt = os.environ.get("SLEEPY_ANALYTICS_SALT", "pet-ai-rate-limit")
    ip_key = hashlib.sha256(f"{salt}|ip|{remote}".encode("utf-8")).hexdigest()
    client_key = hashlib.sha256(
        f"{salt}|client|{client_id or 'missing'}".encode("utf-8")
    ).hexdigest()
    return ip_key, client_key


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def create_pet_ai_blueprint() -> Blueprint:
    blueprint = Blueprint("pet_ai", __name__)
    questions = load_questions()

    def service() -> PetAIService:
        existing = current_app.extensions.get("pet_ai_service")
        if existing is None:
            existing = PetAIService(PetAIConfig.from_env())
            current_app.extensions["pet_ai_service"] = existing
        return existing

    @blueprint.post("/pet/reply")
    def pet_reply():
        content_length = request.content_length
        if content_length is not None and content_length > MAX_BODY_BYTES:
            return _json_error("body_too_large", 413)
        try:
            payload = request.get_json(force=False, silent=False)
            validated = validate_payload(payload, questions)
        except PetAIRequestError as exc:
            return _json_error(exc.code, exc.status)
        except Exception:
            return _json_error("invalid_json", 400)

        pet_service = service()
        try:
            pet_service.limiter.check_request(*_rate_limit_keys())
        except RateLimitExceeded:
            return _json_error("rate_limited", 429)

        wants_stream = "text/event-stream" in request.headers.get("Accept", "")
        if not wants_stream:
            try:
                result = None
                for event in pet_service.events(validated):
                    if event.get("type") == "result":
                        result = event
                if result is None:
                    raise ProviderError("empty_reply")
                return jsonify({"success": True, "reply": result["reply"]})
            except ProviderError as exc:
                return _json_error(exc.code, 503)
            except Exception:
                return _json_error("reply_failed", 503)

        @stream_with_context
        def generate():
            try:
                for event in pet_service.events(validated):
                    yield _sse(event)
            except ProviderError as exc:
                yield _sse({"type": "error", "code": exc.code})
            except Exception:
                yield _sse({"type": "error", "code": "reply_failed"})

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return blueprint


__all__ = ["create_pet_ai_blueprint"]
