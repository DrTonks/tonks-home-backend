"""Small in-memory abuse guard for the public, anonymous endpoint."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import threading
import time
from typing import Callable


class RateLimitExceeded(RuntimeError):
    pass


class PetAIRateLimiter:
    def __init__(
        self,
        minute_limit: int,
        daily_ip_limit: int,
        daily_global_limit: int,
        daily_search_limit: int,
        now: Callable[[], float] = time.time,
    ):
        self.minute_limit = minute_limit
        self.daily_ip_limit = daily_ip_limit
        self.daily_global_limit = daily_global_limit
        self.daily_search_limit = daily_search_limit
        self._now = now
        self._minute: dict[str, deque[float]] = defaultdict(deque)
        self._daily_ip: dict[tuple[str, str], int] = defaultdict(int)
        self._daily_client: dict[tuple[str, str], int] = defaultdict(int)
        self._daily_global: dict[str, int] = defaultdict(int)
        self._daily_search: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, timezone.utc).date().isoformat()

    def check_request(self, ip_key: str, client_key: str | None = None) -> None:
        now = self._now()
        day = self._day(now)
        client_key = client_key or ip_key
        with self._lock:
            keys = {f"ip:{ip_key}", f"client:{client_key}"}
            for key in keys:
                minute = self._minute[key]
                while minute and now - minute[0] >= 60:
                    minute.popleft()
                if len(minute) >= self.minute_limit:
                    raise RateLimitExceeded("minute_limit")
            if self._daily_ip[(day, ip_key)] >= self.daily_ip_limit:
                raise RateLimitExceeded("daily_ip_limit")
            if self._daily_client[(day, client_key)] >= self.daily_ip_limit:
                raise RateLimitExceeded("daily_client_limit")
            if self._daily_global[day] >= self.daily_global_limit:
                raise RateLimitExceeded("daily_global_limit")
            for key in keys:
                self._minute[key].append(now)
            self._daily_ip[(day, ip_key)] += 1
            self._daily_client[(day, client_key)] += 1
            self._daily_global[day] += 1
            self._prune_days(day)

    def check_search(self) -> None:
        now = self._now()
        day = self._day(now)
        with self._lock:
            if self._daily_search[day] >= self.daily_search_limit:
                raise RateLimitExceeded("daily_search_limit")
            self._daily_search[day] += 1
            self._prune_days(day)

    def _prune_days(self, current_day: str) -> None:
        self._daily_ip = defaultdict(
            int,
            {key: value for key, value in self._daily_ip.items() if key[0] == current_day},
        )
        self._daily_client = defaultdict(
            int,
            {key: value for key, value in self._daily_client.items() if key[0] == current_day},
        )
        self._daily_global = defaultdict(
            int,
            {key: value for key, value in self._daily_global.items() if key == current_day},
        )
        self._daily_search = defaultdict(
            int,
            {key: value for key, value in self._daily_search.items() if key == current_day},
        )
