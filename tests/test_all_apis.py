# coding: utf-8
"""Contract regression tests for every public Sleepy API route.

Run from the sleepy directory:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
TEST_CONFIG = {
    "version": 2,
    "debug": False,
    "host": "127.0.0.1",
    "port": 9010,
    "secret": "status-test-secret",
    "admin_secret": "admin-test-secret",
    "status": 0,
    "app_name": "Desktop",
    "timestamp": 0,
    "status_list": [
        {"id": 0, "name": "Online", "desc": "Active", "color": "awake"},
        {"id": 1, "name": "Offline", "desc": "Away", "color": "sleeping"},
    ],
    "github_token": "",
    "agent_activity": [],
    "calendar_events": [],
    "blog_base_url": "https://blog.test",
    "blog_data_url": "https://blog.test/data",
    "music_files": [],
    "todos": [],
}

workspace = None
backend = None
original_cwd = None


def setUpModule():
    global workspace, backend, original_cwd
    original_cwd = os.getcwd()
    test_temp_root = REPO_DIR / ".test-tmp"
    test_temp_root.mkdir(exist_ok=True)
    workspace = test_temp_root / f"sleepy-api-tests-{uuid.uuid4().hex}"
    workspace.mkdir()
    root = workspace
    (root / "music").mkdir()
    (root / "data.json").write_text(
        json.dumps(TEST_CONFIG, ensure_ascii=False),
        encoding="utf-8",
    )
    os.environ["SLEEPY_MUSIC_DIR"] = str(root / "music")
    os.environ["SLEEPY_ANALYTICS_DB"] = str(root / "analytics.sqlite3")
    os.environ["SLEEPY_ANALYTICS_SALT"] = "analytics-test-salt"
    os.environ["SLEEPY_CORS_ORIGINS"] = "http://127.0.0.1:4321"
    os.chdir(root)
    sys.path.insert(0, str(REPO_DIR))
    import server as imported_backend

    backend = imported_backend
    backend.app.config.update(TESTING=True)


def tearDownModule():
    os.chdir(original_cwd)
    for name in (
        "SLEEPY_MUSIC_DIR",
        "SLEEPY_ANALYTICS_DB",
        "SLEEPY_ANALYTICS_SALT",
        "SLEEPY_CORS_ORIGINS",
    ):
        os.environ.pop(name, None)
    if str(REPO_DIR) in sys.path:
        sys.path.remove(str(REPO_DIR))
    workspace_resolved = workspace.resolve()
    temp_root_resolved = (REPO_DIR / ".test-tmp").resolve()
    if workspace_resolved.is_relative_to(temp_root_resolved):
        shutil.rmtree(workspace_resolved)


class AllApiRoutesTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        root = workspace
        (root / "data.json").write_text(
            json.dumps(TEST_CONFIG, ensure_ascii=False),
            encoding="utf-8",
        )
        for suffix in ("", "-shm", "-wal"):
            path = root / f"analytics.sqlite3{suffix}"
            if path.exists():
                path.unlink()
        for path in (root / "music").iterdir():
            path.unlink()
        backend.d.load()
        backend.blog_analytics = backend.BlogAnalytics(
            str(root / "analytics.sqlite3")
        )
        backend.online_users.clear()
        self.client = backend.app.test_client()

    def json(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        return response.get_json()

    def test_route_inventory_matches_tested_contract(self):
        expected = {
            ("GET", "/"),
            ("GET", "/query"),
            ("GET", "/get/status_list"),
            ("GET", "/online_count"),
            ("GET", "/set"),
            ("GET", "/agent-activity"),
            ("POST", "/agent-activity"),
            ("GET", "/blog-posts"),
            ("GET", "/blog/views"),
            ("POST", "/blog/views/<path:slug>"),
            ("GET", "/images/<path:filename>"),
            ("GET", "/music/list"),
            ("GET", "/music/<path:filename>"),
            ("GET", "/music/lyrics/<path:filename>"),
            ("POST", "/music/upload"),
            ("POST", "/music/delete"),
            ("POST", "/music/reorder"),
            ("GET", "/calendar/events"),
            ("POST", "/calendar/events"),
            ("GET", "/calendar/holidays"),
            ("GET", "/github/stats"),
            ("GET", "/todos"),
            ("POST", "/todos"),
        }
        actual = {
            (method, rule.rule)
            for rule in backend.app.url_map.iter_rules()
            if rule.endpoint != "static"
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        self.assertEqual(actual, expected)

    def test_health_device_status_and_online_routes(self):
        health = self.client.get(
            "/",
            headers={"Origin": "http://127.0.0.1:4321"},
        )
        self.assertTrue(self.json(health)["success"])
        self.assertEqual(
            health.headers.get("Access-Control-Allow-Origin"),
            "http://127.0.0.1:4321",
        )

        statuses = self.json(self.client.get("/get/status_list"))
        self.assertEqual(statuses[0]["name"], "Online")

        result = self.json(
            self.client.get(
                "/set",
                query_string={
                    "secret": TEST_CONFIG["secret"],
                    "status": 1,
                    "app_name": "Sleeping",
                    "timestamp": int(time.time()),
                },
            )
        )
        self.assertTrue(result["success"])

        query = self.json(self.client.get("/query"))
        self.assertEqual(query["status"], 1)
        self.assertGreater(query["timestamp"], 0)

        online = self.json(
            self.client.get(
                "/online_count",
                headers={"X-Client-ID": "contract-test-client", "isMobile": "true"},
            )
        )
        self.assertGreaterEqual(online["online_count"], 1)
        self.assertGreaterEqual(online["mobile_count"], 1)

        denied = self.json(
            self.client.get(
                "/set",
                query_string={"secret": "wrong", "status": 0, "app_name": "Nope"},
            )
        )
        self.assertFalse(denied["success"])

    def test_agent_activity_get_and_post(self):
        payload = {
            "dailyActivity": [
                {
                    "date": "2026-07-29",
                    "messageCount": 4,
                    "sessionCount": 2,
                    "toolCallCount": 8,
                }
            ]
        }
        posted = self.json(
            self.client.post(
                "/agent-activity",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json=payload,
            )
        )
        self.assertTrue(posted["success"])
        result = self.json(self.client.get("/agent-activity"))
        self.assertEqual(result["activities"][0]["messageCount"], 4)
        self.assertIn("intensity", result["activities"][0])

    def test_blog_posts_and_article_views(self):
        posts = [
            {
                "title": "Test post",
                "link": "https://blog.test/posts/test-post/",
                "date": "2026-07-29",
                "summary": "Summary",
            }
        ]
        extra = {"featuredProject": None, "featuredTimeline": None}
        with (
            mock.patch.object(backend, "fetch_blog_rss", return_value=posts),
            mock.patch.object(backend, "fetch_blog_extra", return_value=extra),
        ):
            result = self.json(self.client.get("/blog-posts?count=3"))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["posts"][0]["title"], "Test post")

        first = self.json(
            self.client.post(
                "/blog/views/guides/test-post",
                headers={"X-Client-ID": "reader-a"},
            )
        )
        duplicate = self.json(
            self.client.post(
                "/blog/views/guides/test-post",
                headers={"X-Client-ID": "reader-a"},
            )
        )
        second_reader = self.json(
            self.client.post(
                "/blog/views/guides/test-post",
                headers={"X-Client-ID": "reader-b"},
            )
        )
        self.assertEqual((first["views"], first["counted"]), (1, True))
        self.assertEqual((duplicate["views"], duplicate["counted"]), (1, False))
        self.assertEqual((second_reader["views"], second_reader["counted"]), (2, True))

        totals = self.json(
            self.client.get(
                "/blog/views",
                query_string=[
                    ("slugs", "guides/test-post"),
                    ("slugs", "never-viewed"),
                ],
            )
        )
        self.assertEqual(totals["views"], {
            "guides/test-post": 2,
            "never-viewed": 0,
        })
        invalid = self.json(self.client.get("/blog/views?slugs=../bad"))
        self.assertFalse(invalid["success"])

    def test_image_route(self):
        response = self.client.get("/images/projects/calculator.png")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("image/"))
        response.close()
        missing = self.json(self.client.get("/images/projects/missing.png"))
        self.assertFalse(missing["success"])

    def test_music_routes_full_lifecycle(self):
        upload = self.json(
            self.client.post(
                "/music/upload",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                data={
                    "file": (io.BytesIO(b"fake mp3 bytes"), "contract.mp3"),
                    "lyrics": (io.BytesIO(b"[00:00.00]hello"), "contract.lrc"),
                    "title": "Contract song",
                    "artist": "Test artist",
                },
                content_type="multipart/form-data",
            )
        )
        filename = upload["file"]["filename"]
        self.assertTrue(upload["file"]["hasLyrics"])

        listing = self.json(self.client.get("/music/list"))
        self.assertEqual(listing["music"][0]["filename"], filename)
        self.assertTrue(listing["music"][0]["hasLyrics"])

        stream = self.client.get(f"/music/{filename}")
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.mimetype, "audio/mpeg")
        stream.get_data()
        stream.close()
        lyrics = self.client.get(f"/music/lyrics/{filename}")
        self.assertEqual(lyrics.status_code, 200)
        self.assertIn("hello", lyrics.get_data(as_text=True))
        lyrics.close()

        reordered = self.json(
            self.client.post(
                "/music/reorder",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"order": [filename]},
            )
        )
        self.assertTrue(reordered["success"])

        deleted = self.json(
            self.client.post(
                "/music/delete",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"filename": filename},
            )
        )
        self.assertEqual(deleted["deleted"], filename)
        missing = self.json(self.client.get(f"/music/{filename}"))
        self.assertFalse(missing["success"])

    def test_calendar_routes_full_lifecycle(self):
        added = self.json(
            self.client.post(
                "/calendar/events",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={
                    "action": "add",
                    "event": {
                        "date": "2026-07-29",
                        "name": "Contract event",
                        "type": "holiday",
                    },
                },
            )
        )
        event_id = added["event"]["id"]

        updated = self.json(
            self.client.post(
                "/calendar/events",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={
                    "action": "update",
                    "event": {"id": event_id, "name": "Updated event"},
                },
            )
        )
        self.assertEqual(updated["event"]["name"], "Updated event")

        events = self.json(self.client.get("/calendar/events?date=2026-07"))
        self.assertEqual(len(events["events"]), 1)
        holidays = self.json(self.client.get("/calendar/holidays?year=2026"))
        self.assertEqual(holidays["year"], 2026)
        self.assertEqual(holidays["customHolidays"][0]["id"], event_id)

        deleted = self.json(
            self.client.post(
                "/calendar/events",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "delete", "id": event_id},
            )
        )
        self.assertEqual(deleted["deleted"], event_id)

    def test_github_stats_route(self):
        github_payload = {
            "username": "contract-user",
            "totalContributions": 7,
            "days": [],
            "topLanguages": [],
        }
        with mock.patch.object(
            backend,
            "fetch_github_contributions",
            return_value=github_payload,
        ):
            result = self.json(self.client.get("/github/stats"))
        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "contract-user")

    def test_todos_routes_full_lifecycle(self):
        added = self.json(
            self.client.post(
                "/todos",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "add", "text": "Contract todo"},
            )
        )
        todo_id = added["todo"]["id"]

        completed = self.json(
            self.client.post(
                "/todos",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "complete", "id": todo_id},
            )
        )
        self.assertTrue(completed["todo"]["done"])
        listing = self.json(self.client.get("/todos"))
        self.assertEqual(listing["todos"][0]["id"], todo_id)

        deleted = self.json(
            self.client.post(
                "/todos",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "delete", "id": todo_id},
            )
        )
        self.assertEqual(deleted["deleted"], todo_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
