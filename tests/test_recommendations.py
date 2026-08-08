from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import unittest
import uuid

from recommendations import (
    RecommendationRateLimiter,
    RecommendationRateLimitExceeded,
    RecommendationStore,
    RecommendationValidationError,
    validate_recommendation_filters,
    validate_recommendation_payload,
)


class RecommendationValidationTests(unittest.TestCase):
    def test_submission_defaults_to_unknown_and_normalizes_whitespace(self):
        self.assertEqual(
            validate_recommendation_payload(
                {"category": "MUSIC", "content": "  晴天\n 周杰伦  "}
            ),
            ("music", "晴天 周杰伦", "unknown"),
        )

    def test_rejects_unknown_categories_long_values_and_bad_dates(self):
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation_payload({"category": "food", "content": "面"})
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation_payload({"category": "book", "content": "x" * 101})
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation_filters(created_date="2026-02-30")


class RecommendationStoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temporary_directory = temp_root / f"recommendation-tests-{uuid.uuid4().hex}"
        self.temporary_directory.mkdir()
        self.database = self.temporary_directory / "recommendations.sqlite3"
        self.store = RecommendationStore(str(self.database))

    def tearDown(self):
        shutil.rmtree(self.temporary_directory)

    def test_create_list_filter_and_delete(self):
        china = timezone(timedelta(hours=8))
        first = self.store.create(
            "book",
            "献给阿尔吉侬的花束",
            "Tonks",
            now=datetime(2026, 8, 8, 23, 30, tzinfo=china),
        )
        second = self.store.create(
            "anime",
            "葬送的芙莉莲",
            "unknown",
            now=datetime(2026, 8, 9, 0, 30, tzinfo=china),
        )
        self.assertEqual([item["id"] for item in self.store.list()], [second["id"], first["id"]])
        self.assertEqual(self.store.list(category="book"), [first])
        self.assertEqual(self.store.list(created_date="2026-08-09"), [second])
        self.assertTrue(self.store.delete(first["id"]))
        self.assertFalse(self.store.delete(first["id"]))


class RecommendationRateLimiterTests(unittest.TestCase):
    def test_client_or_ip_rotation_does_not_bypass_limits(self):
        clock = [1_700_000_000.0]
        limiter = RecommendationRateLimiter(10, 2, now=lambda: clock[0])
        limiter.check("same-ip", "client-a")
        limiter.check("same-ip", "client-b")
        with self.assertRaises(RecommendationRateLimitExceeded):
            limiter.check("same-ip", "client-c")

        other = RecommendationRateLimiter(10, 2, now=lambda: clock[0])
        other.check("ip-a", "same-client")
        other.check("ip-b", "same-client")
        with self.assertRaises(RecommendationRateLimitExceeded):
            other.check("ip-c", "same-client")


if __name__ == "__main__":
    unittest.main()
