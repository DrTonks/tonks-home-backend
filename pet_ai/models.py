"""Lazy, bounded official model discovery; never send proxy keys to DeepSeek."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from .config import PetAIConfig

logger = logging.getLogger(__name__)
MODELS_URL = "https://api.deepseek.com/models"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def select_model(data: object) -> str:
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("invalid_models")
    valid = [entry for entry in entries if isinstance(entry, dict)
             and isinstance(entry.get("id"), str) and entry["id"].strip()
             and len(entry["id"]) <= 200
             and not any(ord(char) < 32 for char in entry["id"])]
    if not valid:
        raise ValueError("empty_models")
    for entry in valid:
        if "flash" in entry["id"].lower() or "flash" in str(entry.get("name", "")).lower():
            return entry["id"]
    return valid[0]["id"]


class ModelResolver:
    """One cache per long-lived provider; failed refresh retains last selection."""

    def __init__(self, config: PetAIConfig, *, opener=None, clock=time.monotonic):
        self.config = config
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._clock = clock
        self._lock = threading.Lock()
        self._selected = config.model
        self._refresh_at = 0.0

    def resolve(self) -> str:
        if not (self.config.model_auto and self.config.official_endpoint and self.config.api_key):
            return self.config.model
        with self._lock:
            if self._clock() < self._refresh_at:
                return self._selected
            request = urllib.request.Request(MODELS_URL, headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
                "User-Agent": "SleepyPetAI/1.0",
            })
            try:
                timeout = min(3, self.config.timeout_seconds)
                deadline = self._clock() + timeout
                with self._opener.open(request, timeout=timeout) as response:
                    if response.status != 200:
                        raise ValueError("models_http_error")
                    raw = bytearray()
                    # read() can wait for the entire body while a slow peer keeps
                    # resetting socket timeouts. read1() lets us check elapsed time
                    # between network reads; one read may still take the socket timeout.
                    while True:
                        if self._clock() >= deadline:
                            raise ValueError("models_read_timeout")
                        chunk = response.read1(min(16384, 131073 - len(raw)))
                        if self._clock() >= deadline:
                            raise ValueError("models_read_timeout")
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > 131072:
                            raise ValueError("models_too_large")
                self._selected = select_model(json.loads(raw.decode("utf-8")))
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                self._refresh_at = self._clock() + 60
                # Never include upstream bodies, the key or the Request object.
                logger.warning("pet_model discovery_failed; using cached/configured model")
            else:
                self._refresh_at = self._clock() + 21600
            return self._selected
