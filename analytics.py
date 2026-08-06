# coding: utf-8
"""SQLite-backed analytics storage for public blog metrics and agent activity."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


DEFAULT_DEDUPE_SECONDS = 30 * 60


def default_database_path() -> str:
    configured = os.environ.get("SLEEPY_ANALYTICS_DB")
    if configured:
        return configured
    return str(Path(__file__).resolve().with_name("analytics.sqlite3"))


class BlogAnalytics:
    def __init__(self, database_path: str | None = None):
        self.database_path = database_path or default_database_path()

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
                CREATE TABLE IF NOT EXISTS article_views (
                    slug TEXT PRIMARY KEY,
                    views INTEGER NOT NULL DEFAULT 0 CHECK (views >= 0),
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS article_view_dedup (
                    slug TEXT NOT NULL,
                    visitor_hash TEXT NOT NULL,
                    time_bucket INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (slug, visitor_hash, time_bucket)
                );

                CREATE INDEX IF NOT EXISTS idx_article_view_dedup_created_at
                    ON article_view_dedup(created_at);
                """
            )

    def record_view(
        self,
        slug: str,
        visitor_hash: str,
        *,
        now: int | None = None,
        dedupe_seconds: int = DEFAULT_DEDUPE_SECONDS,
    ) -> tuple[int, bool]:
        timestamp = int(time.time()) if now is None else int(now)
        bucket = timestamp // dedupe_seconds
        self.initialize()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO article_view_dedup
                    (slug, visitor_hash, time_bucket, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (slug, visitor_hash, bucket, timestamp),
            )
            counted = cursor.rowcount == 1
            if counted:
                connection.execute(
                    """
                    INSERT INTO article_views (slug, views, updated_at)
                    VALUES (?, 1, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        views = article_views.views + 1,
                        updated_at = excluded.updated_at
                    """,
                    (slug, timestamp),
                )
            row = connection.execute(
                "SELECT views FROM article_views WHERE slug = ?",
                (slug,),
            ).fetchone()

            # Keep the dedupe table bounded without adding a separate scheduler.
            if counted and timestamp % 97 == 0:
                connection.execute(
                    "DELETE FROM article_view_dedup WHERE created_at < ?",
                    (timestamp - 7 * 24 * 60 * 60,),
                )

        return (int(row["views"]) if row else 0, counted)

    def get_views(self, slugs: Iterable[str]) -> dict[str, int]:
        unique_slugs = list(dict.fromkeys(slugs))
        if not unique_slugs:
            return {}
        self.initialize()
        placeholders = ",".join("?" for _ in unique_slugs)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT slug, views FROM article_views WHERE slug IN ({placeholders})",
                unique_slugs,
            ).fetchall()
        found = {str(row["slug"]): int(row["views"]) for row in rows}
        return {slug: found.get(slug, 0) for slug in unique_slugs}


# ---------------------------------------------------------------------------
# Agent activity storage  (Claude Code / Codex session stats)
# ---------------------------------------------------------------------------

def _default_agent_activity_db_path() -> str:
    configured = os.environ.get("SLEEPY_AGENT_ACTIVITY_DB")
    if configured:
        return configured
    return str(Path(__file__).resolve().with_name("agent_activity.sqlite3"))


class AgentActivityStore:
    """SQLite-backed store for agent usage stats from one or more machines.

    Each (date, machine_id) pair is stored independently so that uploading
    from a second machine adds to the same date rather than overwriting.
    Lazy cleanup (at most once per day) removes records older than 365 days.
    """

    DEFAULT_RETENTION_DAYS = 365

    def __init__(self, database_path: str | None = None) -> None:
        self.database_path = database_path or _default_agent_activity_db_path()

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

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
                CREATE TABLE IF NOT EXISTS agent_activity (
                    date         TEXT    NOT NULL,
                    machine_id   TEXT    NOT NULL,
                    message_count  INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
                    session_count  INTEGER NOT NULL DEFAULT 0 CHECK (session_count >= 0),
                    tool_call_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
                    updated_at   INTEGER NOT NULL,
                    PRIMARY KEY (date, machine_id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_activity_date
                    ON agent_activity(date);

                CREATE TABLE IF NOT EXISTS agent_activity_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def upsert_activities(
        self,
        machine_id: str,
        activities: list[dict],
        *,
        now: int | None = None,
    ) -> tuple[int, int]:
        """Upsert *activities* for *machine_id*.

        Same (date, machine_id) → replaced (the client always sends a
        full re-computation for that day).  Different machine_id →
        separate row kept independently.

        Returns ``(new_or_updated, total_rows_after)``.
        """
        timestamp = int(time.time()) if now is None else int(now)
        self.initialize()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for a in activities:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO agent_activity
                        (date, machine_id, message_count,
                         session_count, tool_call_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(a["date"]),
                        machine_id,
                        int(a["messageCount"]),
                        int(a["sessionCount"]),
                        int(a["toolCallCount"]),
                        timestamp,
                    ),
                )

            # ---- lazy cleanup: at most once per calendar day ----------------
            today = time.strftime("%Y-%m-%d", time.localtime(timestamp))
            row = connection.execute(
                "SELECT value FROM agent_activity_meta WHERE key = 'last_cleanup_date'"
            ).fetchone()
            last = row["value"] if row else None
            if last != today:
                cutoff_ts = timestamp - self.DEFAULT_RETENTION_DAYS * 24 * 60 * 60
                cutoff_date = time.strftime("%Y-%m-%d", time.localtime(cutoff_ts))
                connection.execute(
                    "DELETE FROM agent_activity WHERE date < ?",
                    (cutoff_date,),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO agent_activity_meta (key, value) "
                    "VALUES ('last_cleanup_date', ?)",
                    (today,),
                )

            total_row = connection.execute(
                "SELECT COUNT(*) AS cnt FROM agent_activity"
            ).fetchone()
            total = int(total_row["cnt"]) if total_row else 0

        return len(activities), total

    def get_aggregated_activities(self) -> list[dict]:
        """Return per-date activity **summed across all machines**, sorted by date.

        Each element::

            {"date": str, "messageCount": int, "sessionCount": int,
             "toolCallCount": int}
        """
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    date,
                    SUM(message_count)  AS message_count,
                    SUM(session_count)  AS session_count,
                    SUM(tool_call_count) AS tool_call_count
                FROM agent_activity
                GROUP BY date
                ORDER BY date
                """
            ).fetchall()

        return [
            {
                "date": str(row["date"]),
                "messageCount": int(row["message_count"]),
                "sessionCount": int(row["session_count"]),
                "toolCallCount": int(row["tool_call_count"]),
            }
            for row in rows
        ]

    def migrate_from_json(self, existing_activities: list[dict]) -> int:
        """One-shot: copy legacy ``agent_activity`` array from data.json into
        SQLite (keyed as ``machine_id='legacy'``) – only when the store is
        still empty.  Returns the number of migrated rows."""
        if not existing_activities:
            return 0
        # Avoid nested connections: use get_aggregated_activities which
        # opens its own (read-only) handle.
        if self.get_aggregated_activities():
            return 0  # already populated – skip migration
        return self.upsert_activities("legacy", existing_activities)[0]
