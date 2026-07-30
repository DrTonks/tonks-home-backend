# coding: utf-8
"""SQLite-backed analytics storage for public blog metrics."""

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
