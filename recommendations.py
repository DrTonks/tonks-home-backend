"""SQLite storage and validation for public pet recommendations."""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable


ALLOWED_RECOMMENDATION_CATEGORIES = {"music", "book", "game", "anime"}
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RecommendationValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RecommendationRateLimitExceeded(RuntimeError):
    pass


def default_recommendations_database_path() -> str:
    configured = os.environ.get("SLEEPY_RECOMMENDATIONS_DB")
    if configured:
        return configured
    return str(Path(__file__).resolve().with_name("recommendations.sqlite3"))


def recommendation_limit_from_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RecommendationValidationError("invalid_field", f"{field} must be a string")
    value = CONTROL_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    if required and not value:
        raise RecommendationValidationError("missing_field", f"{field} is required")
    if len(value) > maximum:
        raise RecommendationValidationError("field_too_long", f"{field} is too long")
    return value


def validate_recommendation_payload(payload: Any) -> tuple[str, str, str, str]:
    if not isinstance(payload, dict):
        raise RecommendationValidationError("invalid_body", "expected a JSON object")
    category = _text(payload.get("category"), "category", 16, required=True).lower()
    if category not in ALLOWED_RECOMMENDATION_CATEGORIES:
        raise RecommendationValidationError("unknown_category", "category is not enabled")
    content = _text(payload.get("content"), "content", 100, required=True)
    user_name = _text(payload.get("user_name"), "user_name", 30) or "unknown"
    city = _text(payload.get("city"), "city", 50) or "unknown"
    return category, content, user_name, city


def validate_recommendation_filters(
    category: Any = None,
    created_date: Any = None,
) -> tuple[str | None, str | None]:
    normalized_category = _text(category, "category", 16).lower() or None
    if normalized_category and normalized_category not in ALLOWED_RECOMMENDATION_CATEGORIES:
        raise RecommendationValidationError("unknown_category", "category is not enabled")
    normalized_date = _text(created_date, "date", 10) or None
    if normalized_date:
        try:
            if date.fromisoformat(normalized_date).isoformat() != normalized_date:
                raise ValueError
        except ValueError as exc:
            raise RecommendationValidationError(
                "invalid_date", "date must use YYYY-MM-DD"
            ) from exc
    return normalized_category, normalized_date


class RecommendationStore:
    def __init__(self, database_path: str | None = None):
        self.database_path = database_path or default_recommendations_database_path()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pet_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL
                        CHECK (category IN ('music', 'book', 'game', 'anime')),
                    content TEXT NOT NULL,
                    user_name TEXT NOT NULL DEFAULT 'unknown',
                    city TEXT NOT NULL DEFAULT 'unknown',
                    created_at TEXT NOT NULL,
                    created_date TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pet_recommendations_created_date
                    ON pet_recommendations(created_date);

                CREATE INDEX IF NOT EXISTS idx_pet_recommendations_category
                    ON pet_recommendations(category);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(pet_recommendations)")
            }
            if "city" not in columns:
                connection.execute(
                    "ALTER TABLE pet_recommendations "
                    "ADD COLUMN city TEXT NOT NULL DEFAULT 'unknown'"
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "category": str(row["category"]),
            "content": str(row["content"]),
            "user_name": str(row["user_name"]),
            "city": str(row["city"]),
            "created_at": str(row["created_at"]),
        }

    def create(
        self,
        category: str,
        content: str,
        user_name: str,
        city: str = "unknown",
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        created_at = current.astimezone(timezone.utc).isoformat()
        created_date = current.date().isoformat()
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pet_recommendations
                    (category, content, user_name, city, created_at, created_date)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (category, content, user_name, city, created_at, created_date),
            )
            row = connection.execute(
                """
                SELECT id, category, content, user_name, city, created_at
                FROM pet_recommendations WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
        return self._row(row)

    def list(
        self,
        *,
        category: str | None = None,
        created_date: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        values: list[str] = []
        if category:
            clauses.append("category = ?")
            values.append(category)
        if created_date:
            clauses.append("created_date = ?")
            values.append(created_date)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, category, content, user_name, city, created_at
                FROM pet_recommendations
                """
                + where
                + " ORDER BY id DESC",
                values,
            ).fetchall()
        return [self._row(row) for row in rows]

    def delete(self, recommendation_id: int) -> bool:
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pet_recommendations WHERE id = ?",
                (recommendation_id,),
            )
        return cursor.rowcount == 1


class RecommendationRateLimiter:
    def __init__(
        self,
        minute_limit: int = 6,
        daily_limit: int = 30,
        now: Callable[[], float] = time.time,
    ):
        self.minute_limit = max(1, minute_limit)
        self.daily_limit = max(1, daily_limit)
        self._now = now
        self._minute: dict[str, deque[float]] = defaultdict(deque)
        self._daily: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def check(self, ip_key: str, client_key: str) -> None:
        now = self._now()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        keys = {f"ip:{ip_key}", f"client:{client_key}"}
        with self._lock:
            for key in keys:
                minute = self._minute[key]
                while minute and now - minute[0] >= 60:
                    minute.popleft()
                if len(minute) >= self.minute_limit:
                    raise RecommendationRateLimitExceeded("minute_limit")
                if self._daily[(day, key)] >= self.daily_limit:
                    raise RecommendationRateLimitExceeded("daily_limit")
            for key in keys:
                self._minute[key].append(now)
                self._daily[(day, key)] += 1
            self._daily = defaultdict(
                int,
                {key: value for key, value in self._daily.items() if key[0] == day},
            )
