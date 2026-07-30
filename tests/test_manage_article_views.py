# coding: utf-8
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from manage_article_views import (
    connect_existing,
    list_views,
    parse_views,
    resolve_slug,
    set_views,
)


class ManageArticleViewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = (
            Path(__file__).resolve().parents[1] / ".test-tmp" / f"views-{uuid4().hex}"
        )
        self.temporary_directory.mkdir(parents=True)
        self.database = self.temporary_directory / "analytics.sqlite3"
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE article_views (
                slug TEXT PRIMARY KEY,
                views INTEGER NOT NULL DEFAULT 0 CHECK (views >= 0),
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO article_views VALUES ('old/article', 17, 1)"
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        for suffix in ("-shm", "-wal", ""):
            path = Path(f"{self.database}{suffix}")
            if path.exists():
                path.unlink()
        self.temporary_directory.rmdir()

    def test_lists_and_resolves_number_or_slug(self) -> None:
        with closing(connect_existing(self.database)) as connection:
            rows = list_views(connection)
        self.assertEqual(rows, [("old/article", 17)])
        self.assertEqual(resolve_slug("1", rows), "old/article")
        self.assertEqual(resolve_slug("/new/article/", rows), "new/article")

    def test_set_is_idempotent_and_can_insert(self) -> None:
        with closing(connect_existing(self.database)) as connection:
            set_views(connection, "old/article", 88)
            set_views(connection, "old/article", 88)
            set_views(connection, "new/article", 5)
            self.assertEqual(
                list_views(connection),
                [("new/article", 5), ("old/article", 88)],
            )

    def test_rejects_invalid_views(self) -> None:
        for value in ("", "-1", "1.5", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_views(value)
        self.assertEqual(parse_views("0"), 0)


if __name__ == "__main__":
    unittest.main()
