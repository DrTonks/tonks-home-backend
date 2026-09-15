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
VISITOR_IP = "110.80.172.21"
VISITOR_ENV = {"REMOTE_ADDR": VISITOR_IP}


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
    os.environ["SLEEPY_AGENT_ACTIVITY_DB"] = str(root / "agent_activity.sqlite3")
    os.environ["SLEEPY_RECOMMENDATIONS_DB"] = str(root / "recommendations.sqlite3")
    os.environ["SLEEPY_COMMUNITY_DB"] = str(root / "community.sqlite3")
    os.environ["SLEEPY_ANALYTICS_SALT"] = "analytics-test-salt"
    os.environ["SLEEPY_CORS_ORIGINS"] = "http://127.0.0.1:4321"
    os.environ["SLEEPY_ENV_FILE"] = str(root / "missing-test.env")
    os.environ["SLEEPY_STATUS_SECRET"] = TEST_CONFIG["secret"]
    os.environ["SLEEPY_ADMIN_SECRET"] = TEST_CONFIG["admin_secret"]
    os.environ["SLEEPY_GITHUB_TOKEN"] = TEST_CONFIG["github_token"]
    os.environ["SLEEPY_SENIVERSE_API_KEY"] = "weather-test-key"
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
        "SLEEPY_AGENT_ACTIVITY_DB",
        "SLEEPY_RECOMMENDATIONS_DB",
        "SLEEPY_COMMUNITY_DB",
        "SLEEPY_ANALYTICS_SALT",
        "SLEEPY_CORS_ORIGINS",
        "SLEEPY_ENV_FILE",
        "SLEEPY_STATUS_SECRET",
        "SLEEPY_ADMIN_SECRET",
        "SLEEPY_GITHUB_TOKEN",
        "SLEEPY_SENIVERSE_API_KEY",
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
            path = root / f"agent_activity.sqlite3{suffix}"
            if path.exists():
                path.unlink()
            path = root / f"recommendations.sqlite3{suffix}"
            if path.exists():
                path.unlink()
            path = root / f"community.sqlite3{suffix}"
            if path.exists():
                path.unlink()
        for path in (root / "music").iterdir():
            path.unlink()
        backend.d.load()
        backend.blog_analytics = backend.BlogAnalytics(
            str(root / "analytics.sqlite3")
        )
        backend.agent_store = backend.AgentActivityStore(
            str(root / "agent_activity.sqlite3")
        )
        backend.recommendation_store = backend.RecommendationStore(
            str(root / "recommendations.sqlite3")
        )
        backend.recommendation_limiter = backend.RecommendationRateLimiter(100, 100)
        backend.community_store = backend.CommunityStore(
            str(root / "community.sqlite3")
        )
        backend.friend_application_limiter = backend.CommunityBurstLimiter(100)
        backend.online_users.clear()
        backend.geoip_cache.clear()
        backend.geoip_last_attempt.clear()
        backend.geoip_upstream_attempts.clear()
        backend.weather_cache.clear()
        backend.weather_last_attempt.clear()
        backend.weather_upstream_attempts.clear()
        self.client = backend.app.test_client()

    def json(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        return response.get_json()

    def test_route_inventory_matches_tested_contract(self):
        expected = {
            ("GET", "/blog/community/articles/<article_id>/comments"),
            ("POST", "/blog/community/articles/<article_id>/comments"),
            ("PATCH", "/blog/community/articles/<article_id>/comments/<int:ident>"),
            ("DELETE", "/blog/community/articles/<article_id>/comments/<int:ident>"),
            ("GET", "/blog/community/articles/<article_id>/avatar/<int:ident>"),
            ("GET", "/"),
            ("GET", "/geoip"),
            ("GET", "/weather"),
            ("GET", "/query"),
            ("GET", "/get/status_list"),
            ("GET", "/online_count"),
            ("GET", "/set"),
            ("GET", "/agent-activity"),
            ("POST", "/agent-activity"),
            ("GET", "/blog-posts"),
            ("GET", "/blog/views"),
            ("POST", "/blog/views/<path:slug>"),
            ("GET", "/blog/site-visits"),
            ("POST", "/blog/site-visits"),
            ("GET", "/blog/community/likes"),
            ("POST", "/blog/community/likes/<path:target>"),
            ("GET", "/blog/community/comments/<page>"),
            ("POST", "/blog/community/comments/<page>"),
            ("PATCH", "/blog/community/comments/<int:comment_id>"),
            ("DELETE", "/blog/community/comments/<int:comment_id>"),
            ("POST", "/blog/community/avatar-preview"),
            ("GET", "/blog/community/avatar/<int:comment_id>"),
            ("GET", "/blog/community/feedback"),
            ("POST", "/blog/community/feedback"),
            ("GET", "/blog/community/feedback/avatar/<int:message_id>"),
            ("GET", "/blog/community/feedback/room-avatar/<int:message_id>"),
            ("POST", "/blog/community/feedback/messages"),
            ("POST", "/blog/community/feedback/<int:topic_id>/messages"),
            ("PATCH", "/blog/community/feedback/<int:topic_id>"),
            ("DELETE", "/blog/community/feedback/<int:topic_id>"),
            ("PATCH", "/blog/community/comments/<int:comment_id>/pin"),
            ("POST", "/blog/community/feedback/from-comment"),
            ("POST", "/blog/community/feedback/merge"),
            ("GET", "/blog/community/friend-applications"),
            ("POST", "/blog/community/friend-applications"),
            ("POST", "/blog/community/friend-applications/status"),
            ("POST", "/blog/community/friend-applications/<int:application_id>"),
            ("GET", "/images/<path:filename>"),
            ("GET", "/music/list"),
            ("GET", "/music/<path:filename>"),
            ("GET", "/music/lyrics/<path:filename>"),
            ("GET", "/music/cover/<path:filename>"),
            ("POST", "/music/upload"),
            ("POST", "/music/cover/upload"),
            ("POST", "/music/delete"),
            ("POST", "/music/reorder"),
            ("GET", "/calendar/events"),
            ("POST", "/calendar/events"),
            ("GET", "/calendar/holidays"),
            ("GET", "/github/stats"),
            ("GET", "/todos"),
            ("POST", "/todos"),
            ("POST", "/pet/reply"),
            ("GET", "/pet/recommendations"),
            ("POST", "/pet/recommendations"),
            ("DELETE", "/pet/recommendations/<int:recommendation_id>"),
            ("GET", "/status-history"),
        }
        actual = {
            (method, rule.rule)
            for rule in backend.app.url_map.iter_rules()
            if rule.endpoint != "static"
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        self.assertEqual(actual, expected)

    def test_waitress_is_the_only_proxy_header_trust_boundary(self):
        request = mock.MagicMock()
        request.remote_addr = VISITOR_IP
        request.headers = {"X-Forwarded-For": "8.8.8.8"}
        request.environ = {
            "werkzeug.proxy_fix.orig": {"REMOTE_ADDR": "8.8.8.8"},
        }

        self.assertEqual(backend.get_geoip_client_address(request), VISITOR_IP)
        self.assertEqual(backend.get_request_key(request), f"ip:{VISITOR_IP}")

    def test_waitress_proxy_settings_trust_exactly_one_local_proxy(self):
        with mock.patch.dict(
            os.environ,
            {
                "SLEEPY_TRUSTED_PROXY": "127.0.0.1",
                "SLEEPY_TRUSTED_PROXY_COUNT": "1",
            },
        ):
            settings = backend.get_waitress_proxy_settings()

        self.assertEqual(settings["trusted_proxy"], "127.0.0.1")
        self.assertEqual(settings["trusted_proxy_count"], 1)
        self.assertEqual(
            settings["trusted_proxy_headers"],
            {"x-forwarded-for", "x-forwarded-proto"},
        )
        self.assertTrue(settings["clear_untrusted_proxy_headers"])

    def test_waitress_proxy_settings_reject_invalid_proxy_count(self):
        with mock.patch.dict(
            os.environ,
            {
                "SLEEPY_TRUSTED_PROXY": "127.0.0.1",
                "SLEEPY_TRUSTED_PROXY_COUNT": "0",
            },
        ):
            with self.assertRaises(RuntimeError):
                backend.get_waitress_proxy_settings()

    def test_weather_uses_explicit_visitor_ip_and_caches_normalized_result(self):
        upstream_result = {
            "location": {
                "id": "WT7W3R63DQMH",
                "city": "福州",
                "region": "福建",
                "country": "CN",
                "path": "福州,福建,中国",
                "timezone": "Asia/Shanghai",
            },
            "now": {"text": "多云", "code": 4, "temperature": 28},
            "tomorrow": {
                "date": "2026-09-01",
                "text": "阵雨",
                "code": 10,
                "low": 25,
                "high": 32,
            },
            "cached_at": "2026-08-31T00:00:00+00:00",
            "stale": False,
        }
        with mock.patch.object(
            backend, "fetch_seniverse_weather", return_value=upstream_result
        ) as lookup:
            first = self.client.get(
                "/weather", environ_overrides=VISITOR_ENV
            )
            second = self.client.get(
                "/weather", environ_overrides=VISITOR_ENV
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers.get("Cache-Control"), "private, no-store")
        self.assertEqual(first.get_json()["location"]["city"], "福州")
        self.assertEqual(first.get_json()["tomorrow"]["text"], "阵雨")
        self.assertNotIn("110.80.172.21", json.dumps(first.get_json()))
        self.assertEqual(second.get_json(), first.get_json())
        lookup.assert_called_once_with("110.80.172.21")

    def test_weather_ignores_application_layer_forwarded_for_spoofing(self):
        upstream_result = {
            "location": {
                "id": "WT7W3R63DQMH",
                "city": "福州",
                "region": "福建",
                "country": "CN",
                "path": "福州,福建,中国",
                "timezone": "Asia/Shanghai",
            },
            "now": {"text": "晴", "code": 0, "temperature": 28},
            "tomorrow": {
                "date": "2026-09-01",
                "text": "多云",
                "code": 4,
                "low": 25,
                "high": 32,
            },
            "cached_at": "2026-08-31T00:00:00+00:00",
            "stale": False,
        }
        with mock.patch.object(
            backend, "fetch_seniverse_weather", return_value=upstream_result
        ) as lookup:
            response = self.client.get(
                "/weather",
                headers={"X-Forwarded-For": "8.8.8.8"},
                environ_overrides=VISITOR_ENV,
            )

        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(VISITOR_IP)

    def test_weather_returns_stale_cache_during_upstream_failure(self):
        cached_result = {
            "location": {
                "id": "WT7W3R63DQMH",
                "city": "福州",
                "region": "福建",
                "country": "CN",
                "path": "福州,福建,中国",
                "timezone": "Asia/Shanghai",
            },
            "now": {"text": "多云", "code": 4, "temperature": 28},
            "tomorrow": {
                "date": "2026-09-01",
                "text": "阵雨",
                "code": 10,
                "low": 25,
                "high": 32,
            },
            "cached_at": "2026-08-31T00:00:00+00:00",
            "stale": False,
        }
        with mock.patch.object(
            backend, "fetch_seniverse_weather", return_value=cached_result
        ):
            self.assertEqual(
                self.client.get("/weather", environ_overrides=VISITOR_ENV).status_code,
                200,
            )

        cache_entry = next(iter(backend.weather_cache.values()))
        cache_entry["fresh_until"] = time.monotonic() - 1
        for key in list(backend.weather_last_attempt):
            backend.weather_last_attempt[key] = time.monotonic() - 60
        with mock.patch.object(
            backend,
            "fetch_seniverse_weather",
            side_effect=backend.WeatherUpstreamError("provider failed"),
        ) as lookup:
            stale = self.client.get("/weather", environ_overrides=VISITOR_ENV)

        self.assertEqual(stale.status_code, 200)
        self.assertTrue(stale.get_json()["stale"])
        self.assertEqual(stale.get_json()["location"]["city"], "福州")
        lookup.assert_called_once_with("110.80.172.21")

    def test_weather_reports_missing_server_configuration_without_leaking_details(self):
        with mock.patch.dict(os.environ, {"SLEEPY_SENIVERSE_API_KEY": ""}):
            response = self.client.get(
                "/weather", environ_overrides=VISITOR_ENV
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "weather not configured")
        self.assertNotIn("key", response.get_json()["message"].lower())

    def test_seniverse_request_uses_explicit_ip_instead_of_server_auto_detection(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "results": [{
                "location": {"name": "福州"},
                "now": {"text": "晴", "code": "0", "temperature": "26"},
            }]
        }).encode("utf-8")
        with mock.patch.object(
            backend.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            backend.fetch_seniverse_json("now", "110.80.172.21")

        requested_url = urlopen.call_args.args[0].full_url
        self.assertIn("location=110.80.172.21", requested_url)
        self.assertNotIn("location=ip", requested_url)

    def test_seniverse_weather_normalizes_current_and_tomorrow_payloads(self):
        now_payload = {
            "results": [{
                "location": {
                    "id": "WT7W3R63DQMH",
                    "name": "福州",
                    "country": "CN",
                    "path": "福州,福建,中国",
                    "timezone": "Asia/Shanghai",
                },
                "now": {"text": "晴", "code": "0", "temperature": "26"},
            }]
        }
        daily_payload = {
            "results": [{
                "daily": [
                    {
                        "date": "2026-08-31",
                        "text_day": "晴",
                        "code_day": "0",
                        "low": "24",
                        "high": "31",
                    },
                    {
                        "date": "2026-09-01",
                        "text_day": "多云",
                        "code_day": "4",
                        "low": "23",
                        "high": "30",
                    },
                ]
            }]
        }
        with mock.patch.object(
            backend,
            "fetch_seniverse_json",
            side_effect=[now_payload, daily_payload],
        ) as fetch:
            result = backend.fetch_seniverse_weather("110.80.172.21")

        self.assertEqual(result["location"]["city"], "福州")
        self.assertEqual(result["location"]["region"], "福建")
        self.assertEqual(result["now"], {
            "text": "晴", "code": 0, "temperature": 26,
        })
        self.assertEqual(result["tomorrow"]["date"], "2026-09-01")
        self.assertEqual(result["tomorrow"]["low"], 23)
        self.assertFalse(result["stale"])
        self.assertEqual(fetch.call_args_list, [
            mock.call("now", "110.80.172.21"),
            mock.call("daily", "110.80.172.21", {"start": 0, "days": 2}),
        ])

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

        with mock.patch.object(
            backend,
            "fetch_ip_api_location",
            return_value={
                "success": True,
                "city": "福州",
                "region": "福建",
                "country": "CN",
                "lat": 26.08,
                "lon": 119.30,
            },
        ) as lookup:
            geoip = self.client.get(
                "/geoip",
                environ_overrides=VISITOR_ENV,
            )
        self.assertEqual(geoip.status_code, 200)
        self.assertEqual(geoip.headers.get("Cache-Control"), "private, no-store")
        self.assertEqual(geoip.get_json()["city"], "福州")
        self.assertNotIn("ip", geoip.get_json())
        lookup.assert_called_once_with("110.80.172.21")

        # 同一访客重复请求命中加盐哈希内存缓存，不再次消耗 ip-api 额度。
        cached_geoip = self.client.get(
            "/geoip",
            environ_overrides=VISITOR_ENV,
        )
        self.assertEqual(cached_geoip.status_code, 200)
        self.assertEqual(cached_geoip.get_json()["city"], "福州")
        lookup.assert_called_once()
        self.assertNotIn(
            "110.80.172.21",
            json.dumps(list(backend.geoip_cache.keys())),
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

        history = self.json(self.client.get("/status-history"))
        self.assertTrue(history["success"])
        self.assertEqual(history["history"][0]["app_name"], "Sleeping")
        self.assertEqual(history["history"][0]["info"]["name"], "Offline")

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

    def test_status_history_keeps_five_and_deduplicates_consecutive_states(self):
        backend.d.data["status_history"] = []
        backend.d.save()
        base = int(time.time())
        for index in range(6):
            result = self.json(self.client.get(
                "/set",
                query_string={
                    "secret": TEST_CONFIG["secret"],
                    "status": 0,
                    "app_name": f"App-{index}",
                    "timestamp": base + index,
                },
            ))
            self.assertTrue(result["success"])
        history = self.json(self.client.get("/status-history"))["history"]
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0]["app_name"], "App-5")
        self.assertEqual(history[-1]["app_name"], "App-1")

        self.client.get(
            "/set",
            query_string={
                "secret": TEST_CONFIG["secret"],
                "status": 0,
                "app_name": "App-5",
                "timestamp": base + 100,
            },
        )
        deduplicated = self.json(self.client.get("/status-history"))["history"]
        self.assertEqual(len(deduplicated), 5)
        self.assertEqual(deduplicated[0]["timestamp"], base + 100)

    def test_geoip_distinguishes_client_rate_limit_and_upstream_failures(self):
        invalid = self.client.get("/geoip")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "geoip unavailable")

        backend.geoip_last_attempt.clear()
        backend.geoip_upstream_attempts.clear()
        with mock.patch.object(
            backend,
            "fetch_ip_api_location",
            side_effect=backend.GeoIpUpstreamError("provider failed"),
        ):
            upstream = self.client.get(
                "/geoip",
                environ_overrides=VISITOR_ENV,
            )
        self.assertEqual(upstream.status_code, 502)
        self.assertEqual(upstream.get_json()["code"], "geoip upstream error")

        backend.geoip_last_attempt.clear()
        backend.geoip_upstream_attempts.clear()
        backend.geoip_upstream_attempts.extend(
            [time.monotonic()] * backend.GEOIP_UPSTREAM_LIMIT_PER_MINUTE
        )
        with mock.patch.object(backend, "fetch_ip_api_location") as lookup:
            limited = self.client.get(
                "/geoip",
                environ_overrides=VISITOR_ENV,
            )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.get_json()["code"], "geoip rate limited")
        self.assertGreaterEqual(int(limited.headers.get("Retry-After")), 1)
        self.assertLessEqual(int(limited.headers.get("Retry-After")), 60)
        lookup.assert_not_called()

    def test_geoip_treats_provider_payload_failures_as_upstream_errors(self):
        failed_response = mock.MagicMock()
        failed_response.__enter__.return_value.read.return_value = b'{"status":"fail"}'
        with mock.patch.object(
            backend.urllib.request,
            "urlopen",
            return_value=failed_response,
        ):
            with self.assertRaises(backend.GeoIpUpstreamError):
                backend.fetch_ip_api_location("110.80.172.21")

        malformed_request = mock.MagicMock()
        malformed_request.environ = {}
        malformed_request.remote_addr = "not-an-ip"
        with self.assertRaises(backend.GeoIpClientError):
            backend.get_geoip_client_address(malformed_request)

        malformed_response = mock.MagicMock()
        malformed_response.__enter__.return_value.read.return_value = (
            b'{"status":"success","lat":null,"lon":119.3}'
        )
        with mock.patch.object(
            backend.urllib.request,
            "urlopen",
            return_value=malformed_response,
        ):
            with self.assertRaises(backend.GeoIpUpstreamError):
                backend.fetch_ip_api_location("110.80.172.21")

    def test_pet_recommendations_full_lifecycle(self):
        anonymous = self.json(
            self.client.post(
                "/pet/recommendations",
                json={"category": "book", "content": "献给阿尔吉侬的花束"},
                headers={"X-Client-ID": "reader-1"},
            )
        )["recommendation"]
        self.assertEqual(anonymous["user_name"], "unknown")
        self.assertEqual(anonymous["city"], "unknown")

        public_named = self.json(
            self.client.post(
                "/pet/recommendations",
                json={
                    "category": "book",
                    "content": "三体",
                    "user_name": "读者小明",
                },
                headers={"X-Client-ID": "reader-public-named"},
                environ_base={"REMOTE_ADDR": "203.0.113.42"},
            )
        )["recommendation"]
        self.assertEqual(public_named["user_name"], "读者小明")

        named = self.json(
            self.client.post(
                "/pet/recommendations",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={
                    "category": "music",
                    "content": "晴天",
                    "user_name": "Tonks",
                    "city": "福州",
                },
                headers={"X-Client-ID": "reader-2"},
            )
        )["recommendation"]
        self.assertEqual(named["user_name"], "Tonks")
        self.assertEqual(named["city"], "福州")

        public_list = self.json(self.client.get("/pet/recommendations"))
        self.assertTrue(public_list["success"])
        self.assertEqual(public_list["scope"], "public")
        self.assertEqual(public_list["count"], 3)

        daily_denied = self.json(
            self.client.post(
                "/pet/recommendations",
                json={"category": "game", "content": "第二次推荐"},
                headers={"X-Client-ID": "reader-1"},
            )
        )
        self.assertEqual(daily_denied["code"], "daily limit reached")

        all_items = self.json(
            self.client.get(
                "/pet/recommendations",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
            )
        )
        self.assertEqual(all_items["count"], 3)
        self.assertEqual(all_items["recommendations"][0]["id"], named["id"])

        music = self.json(
            self.client.get(
                "/pet/recommendations",
                query_string={
                    "category": "music",
                    "secret": TEST_CONFIG["admin_secret"],
                },
            )
        )
        self.assertEqual(music["recommendations"], [named])

        today = time.strftime("%Y-%m-%d")
        dated = self.json(
            self.client.get(
                "/pet/recommendations",
                query_string={
                    "date": today,
                    "category": "book",
                    "secret": TEST_CONFIG["admin_secret"],
                },
            )
        )
        self.assertEqual(dated["recommendations"], [public_named, anonymous])

        invalid = self.json(
            self.client.post(
                "/pet/recommendations",
                json={"category": "food", "content": "nope"},
            )
        )
        self.assertFalse(invalid["success"])
        self.assertEqual(invalid["code"], "unknown_category")

        denied = self.json(
            self.client.delete(
                f"/pet/recommendations/{anonymous['id']}",
                query_string={"secret": "wrong"},
            )
        )
        self.assertFalse(denied["success"])

        deleted = self.json(
            self.client.delete(
                f"/pet/recommendations/{anonymous['id']}",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
            )
        )
        self.assertEqual(deleted["deleted"], anonymous["id"])
        self.assertEqual(
            self.json(
                self.client.get(
                    "/pet/recommendations",
                    query_string={"secret": TEST_CONFIG["admin_secret"]},
                )
            )["count"],
            2,
        )

    def test_public_recommendation_reuses_client_cached_city(self):
        with mock.patch.object(backend, "get_automatic_recommendation_city") as lookup:
            recommendation = self.json(
                self.client.post(
                    "/pet/recommendations",
                    json={
                        "category": "book",
                        "content": "三体",
                        "city": "福州",
                    },
                    headers={"X-Client-ID": "reader-with-cached-city"},
                )
            )["recommendation"]

        self.assertEqual(recommendation["city"], "福州")
        lookup.assert_not_called()

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

    def test_agent_activity_multi_machine_summing(self):
        """两台机器同日上报 → 加总；同机器同日重复上报 → 覆盖"""
        admin_qs = {"secret": TEST_CONFIG["admin_secret"]}
        date = "2026-08-06"

        # --- 1. Windows 上报 ---
        r1 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": date, "messageCount": 10, "sessionCount": 2, "toolCallCount": 5}
                    ],
                },
            )
        )
        self.assertTrue(r1["success"])
        self.assertEqual(r1["machineId"], "windows-pc")

        # --- 2. Mac 同日上报 → 应加总 ---
        r2 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "macbook",
                    "dailyActivity": [
                        {"date": date, "messageCount": 20, "sessionCount": 3, "toolCallCount": 8}
                    ],
                },
            )
        )
        self.assertTrue(r2["success"])

        agg = self.json(self.client.get("/agent-activity"))
        day = agg["activities"][0]
        self.assertEqual(day["date"], date)
        self.assertEqual(day["messageCount"], 30)    # 10 + 20
        self.assertEqual(day["sessionCount"], 5)     # 2 + 3
        self.assertEqual(day["toolCallCount"], 13)   # 5 + 8

        # --- 3. Windows 再次上报同日（messageCount 升，sessionCount/toolCallCount 降）→
        #     只升不降：messageCount 应更新为 15，sessionCount 保持 2，toolCallCount 保持 5 ---
        r3 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": date, "messageCount": 15, "sessionCount": 1, "toolCallCount": 3}
                    ],
                },
            )
        )
        self.assertTrue(r3["success"])

        agg2 = self.json(self.client.get("/agent-activity"))
        day2 = agg2["activities"][0]
        self.assertEqual(day2["messageCount"], 35)    # 15 + 20 (messageCount 升，接受)
        self.assertEqual(day2["sessionCount"], 5)     # 2 + 3  (sessionCount 降，保持旧值)
        self.assertEqual(day2["toolCallCount"], 13)   # 5 + 8  (toolCallCount 降，保持旧值)

        # --- 4. 多日跨机器 ---
        day2_date = "2026-08-07"
        self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": day2_date, "messageCount": 5, "sessionCount": 1, "toolCallCount": 2}
                    ],
                },
            )
        )
        agg3 = self.json(self.client.get("/agent-activity"))
        self.assertEqual(len(agg3["activities"]), 2)  # 两天
        self.assertEqual(agg3["activities"][1]["date"], day2_date)

        # --- 5. 向后兼容：老格式（裸数组，无 machineId） ---
        r5 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json=[
                    {"date": "2026-08-08", "messageCount": 7, "sessionCount": 1, "toolCallCount": 4}
                ],
            )
        )
        self.assertTrue(r5["success"])
        self.assertEqual(r5["machineId"], "unknown")

        # --- 6. 只升不降：值全部下降 → 全部被拒绝，保留旧值 ---
        r6 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": "2026-08-06", "messageCount": 5, "sessionCount": 0, "toolCallCount": 0}
                    ],
                },
            )
        )
        self.assertTrue(r6["success"])
        agg6 = self.json(self.client.get("/agent-activity"))
        day6 = [a for a in agg6["activities"] if a["date"] == "2026-08-06"][0]
        # 旧值应是: messageCount=15(windows)+20(mac)=35, sessionCount=2+3=5, toolCallCount=5+8=13
        self.assertEqual(day6["messageCount"], 35)
        self.assertEqual(day6["sessionCount"], 5)
        self.assertEqual(day6["toolCallCount"], 13)

    def test_blog_post_stats_batch_and_partial_failure(self):
        posts = [{'link': 'https://blog.test/posts/nested/test/', 'title': 'A'},
                 {'link': 'https://blog.test/posts/unseen/', 'title': 'B'},
                 {'link': 'https://blog.test/about/', 'title': 'Other'}]
        with (mock.patch.object(backend.blog_analytics, 'get_views', return_value={'nested/test': 9, 'unseen': 0}) as views,
              mock.patch.object(backend.community_store, 'get_likes', return_value={'post:nested/test': {'count': 3}, 'post:unseen': {'count': 0}}) as likes,
              mock.patch.object(backend.app.extensions['article_comments'], 'get_public_totals_by_slug', return_value={'nested/test': 2, 'unseen': None}) as comments):
            result = backend.enrich_blog_post_stats(posts)
        self.assertEqual(result[0]['stats'], {'views': 9, 'likes': 3, 'comments': 2})
        self.assertEqual(result[1]['stats'], {'views': 0, 'likes': 0, 'comments': None})
        self.assertEqual(result[2]['stats'], {'views': None, 'likes': None, 'comments': None})
        self.assertNotIn('stats', posts[0])
        self.assertEqual((views.call_count, likes.call_count, comments.call_count), (1, 1, 1))
        with (mock.patch.object(backend.blog_analytics, 'get_views', side_effect=RuntimeError('offline')),
              mock.patch.object(backend.community_store, 'get_likes', return_value={'post:nested/test': {'count': 3}}),
              mock.patch.object(backend.app.extensions['article_comments'], 'get_public_totals_by_slug', return_value={'nested/test': 2})):
            result = backend.enrich_blog_post_stats(posts[:1])
        self.assertEqual(result[0]['stats'], {'views': None, 'likes': 3, 'comments': 2})

    def test_blog_feed_category_atom_and_rss(self):
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://blog.test/posts/a/"/><category term="Code"/></entry></feed>'
        with mock.patch.object(backend.urllib.request, 'urlopen', return_value=io.BytesIO(atom)):
            self.assertEqual(backend.fetch_blog_rss()[0]['category'], 'Code')
        rss = b'<rss><channel><item><title>A</title><link>https://blog.test/posts/a/</link><category>Notes</category></item></channel></rss>'
        with mock.patch.object(backend.urllib.request, 'urlopen', side_effect=[OSError('no atom'), io.BytesIO(rss)]):
            self.assertEqual(backend.fetch_blog_rss()[0]['category'], 'Notes')

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

        empty_visits = self.json(self.client.get("/blog/site-visits"))
        self.assertEqual(empty_visits["visits"], 0)
        first_visit = self.json(
            self.client.post(
                "/blog/site-visits",
                headers={
                    "X-Visit-ID": "blog-session-00000001",
                    "X-Site-Source": "blog",
                },
            )
        )
        duplicate_visit = self.json(
            self.client.post(
                "/blog/site-visits",
                headers={
                    "X-Visit-ID": "blog-session-00000001",
                    "X-Site-Source": "blog",
                },
            )
        )
        home_visit = self.json(
            self.client.post(
                "/blog/site-visits",
                headers={
                    "X-Visit-ID": "home-session-00000001",
                    "X-Site-Source": "home",
                },
            )
        )
        self.assertEqual((first_visit["visits"], first_visit["counted"]), (1, True))
        self.assertEqual(
            (duplicate_visit["visits"], duplicate_visit["counted"]),
            (1, False),
        )
        self.assertEqual((home_visit["visits"], home_visit["counted"]), (2, True))
        self.assertEqual(self.json(self.client.get("/blog/site-visits"))["visits"], 2)

        invalid_visit = self.json(
            self.client.post(
                "/blog/site-visits",
                headers={"X-Visit-ID": "short", "X-Site-Source": "blog"},
            )
        )
        self.assertFalse(invalid_visit["success"])

    def test_blog_community_avatar_prefers_q1_and_keeps_fallbacks(self):
        with mock.patch.object(
            backend.community_store,
            "get_comment_avatar",
            return_value={"email": "3064517736@qq.com"},
        ):
            primary = self.client.get(
                "/blog/community/avatar/8",
                follow_redirects=False,
            )
            gravatar = self.client.get(
                "/blog/community/avatar/8?fallback=1",
                follow_redirects=False,
            )
            local = self.client.get(
                "/blog/community/avatar/8?fallback=2",
                follow_redirects=False,
            )

        self.assertEqual(primary.status_code, 302)
        self.assertEqual(
            primary.headers["Location"],
            "https://q1.qlogo.cn/g?b=qq&nk=3064517736&s=640",
        )
        self.assertEqual(gravatar.status_code, 200)
        self.assertEqual(gravatar.mimetype, "image/svg+xml")
        self.assertEqual(local.status_code, 200)
        self.assertEqual(local.mimetype, "image/svg+xml")

    def test_non_qq_avatars_are_local_and_match_preview(self):
        import urllib.parse
        email = 'visitor@example.com'
        preview = self.json(self.client.post('/blog/community/avatar-preview', json={'email': email}))
        svg = urllib.parse.unquote(preview['avatar_url'].split(',', 1)[1])
        self.assertNotIn(email, svg)
        self.assertEqual(svg, backend.community_avatar_svg(' VISITOR@EXAMPLE.COM '))
        for route, getter in [('/blog/community/avatar/8', 'get_comment_avatar'),
                              ('/blog/community/feedback/avatar/8', 'get_feedback_avatar'),
                              ('/blog/community/feedback/room-avatar/8', 'get_feedback_room_avatar')]:
            with self.subTest(route=route), mock.patch.object(backend.community_store, getter, return_value={'email': email}):
                for suffix in ['', '?fallback=1', '?fallback=2']:
                    response = self.client.get(route + suffix)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, 'image/svg+xml')
                    self.assertEqual(response.get_data(as_text=True), svg)
                    self.assertNotIn('Location', response.headers)

    def test_blog_community_avatar_preview_uses_private_email_derivation(self):
        qq = self.json(
            self.client.post(
                "/blog/community/avatar-preview",
                json={"email": "3064517736@qq.com"},
            )
        )
        gravatar = self.json(
            self.client.post(
                "/blog/community/avatar-preview",
                json={"email": "visitor@example.com"},
            )
        )
        invalid = self.client.post(
            "/blog/community/avatar-preview",
            json={"email": "not-an-email"},
        )

        self.assertTrue(qq["success"])
        self.assertEqual(
            qq["avatar_url"],
            "https://q1.qlogo.cn/g?b=qq&nk=3064517736&s=640",
        )
        self.assertTrue(gravatar["success"])
        self.assertTrue(gravatar["avatar_url"].startswith("data:image/svg+xml,"))
        self.assertNotIn("visitor@example.com", gravatar["avatar_url"])
        self.assertEqual(invalid.status_code, 400)

    def test_friend_application_tracking_is_private_and_follows_moderation(self):
        submitted_response = self.client.post(
            "/blog/community/friend-applications",
            headers={"X-Client-ID": "friend-applicant-a"},
            json={
                "name": "Example site",
                "website": "https://example.com/",
                "avatar": "https://example.com/avatar.png",
                "description": "A small personal site",
                "email": "owner@example.com",
            },
        )
        self.assertEqual(submitted_response.status_code, 201)
        submitted = submitted_response.get_json()
        token = submitted["tracking_token"]
        application_id = submitted["application"]["id"]
        self.assertGreaterEqual(len(token), 32)
        self.assertNotIn("email", submitted["application"])

        own = self.json(
            self.client.post(
                "/blog/community/friend-applications/status",
                json={"tokens": [token]},
            )
        )
        self.assertEqual(own["applications"][0]["status"], "pending")
        self.assertNotIn("email", own["applications"][0])
        unknown = self.json(
            self.client.post(
                "/blog/community/friend-applications/status",
                json={"tokens": ["x" * 43]},
            )
        )
        self.assertEqual(unknown["applications"], [])

        updated = self.json(
            self.client.post(
                f"/blog/community/friend-applications/{application_id}",
                headers={"X-Admin-Secret": TEST_CONFIG["admin_secret"]},
                json={"status": "approved", "moderation_note": "欢迎加入"},
            )
        )
        self.assertEqual(updated["status"], "approved")
        approved = self.json(
            self.client.post(
                "/blog/community/friend-applications/status",
                json={"tokens": [token]},
            )
        )
        self.assertEqual(approved["applications"][0]["status"], "approved")
        self.assertEqual(approved["applications"][0]["moderation_note"], "欢迎加入")

    def test_image_route(self):
        response = self.client.get("/images/projects/calculator.png")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], 'https://blog.test/images/projects/calculator.png')
        response.close()
        missing = self.client.get("/images/projects/missing.png")
        self.assertEqual(missing.status_code, 302)  # Blog now owns existence checks.
        self.assertEqual(self.client.get('/images/projects/%252e%252e/secret').status_code, 404)

    def test_image_route_preserves_known_legacy_alias_without_case_guessing(self):
        cases = {
            'jxj.JPG': 'jxj.jpg',
            'jxj.jpg': 'jxj.jpg',
            'JXJ.JPG': 'JXJ.JPG',
            'other.JPG': 'other.JPG',
        }
        with mock.patch.object(backend.urllib.request, 'urlopen') as request:
            for source, target in cases.items():
                with self.subTest(source=source):
                    response = self.client.get('/images/projects/' + source)
                    try:
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(
                            response.headers['Location'],
                            'https://blog.test/images/projects/' + target,
                        )
                        self.assertEqual(response.headers['Cache-Control'], 'public, max-age=300')
                    finally:
                        response.close()
            request.assert_not_called()

    def test_blog_extra_uses_blog_images_without_local_copies(self):
        project = {'startDate': '2026-01-01', 'image': '/images/projects/new-only-in-blog.png'}
        timeline = {'startDate': '2026-01-02', 'image': ['/images/projects/other.JPG']}
        responses = [io.BytesIO(json.dumps([project]).encode()), io.BytesIO(json.dumps([timeline]).encode())]
        with mock.patch.object(backend.urllib.request, 'urlopen', side_effect=responses) as request:
            result = backend.fetch_blog_extra()
        self.assertEqual(request.call_count, 2)  # Only JSON requests; no image requests.
        self.assertEqual(result['featuredProject']['image'], 'https://blog.test/images/projects/new-only-in-blog.png')
        self.assertEqual(result['featuredProject']['images'], [result['featuredProject']['image']])
        self.assertEqual(result['featuredTimeline']['image'], result['featuredTimeline']['images'])

    def test_music_routes_full_lifecycle(self):
        upload = self.json(
            self.client.post(
                "/music/upload",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                data={
                    "file": (io.BytesIO(b"fake mp3 bytes"), "contract.mp3"),
                    "lyrics": (io.BytesIO(b"[00:00.00]hello"), "contract.lrc"),
                    "cover": (io.BytesIO(b"fake png bytes"), "cover.png"),
                    "title": "Contract song",
                    "artist": "Test artist",
                },
                content_type="multipart/form-data",
            )
        )
        filename = upload["file"]["filename"]
        self.assertTrue(upload["file"]["hasLyrics"])
        self.assertTrue(upload["file"]["hasCover"])

        listing = self.json(self.client.get("/music/list"))
        self.assertEqual(listing["music"][0]["filename"], filename)
        self.assertTrue(listing["music"][0]["hasLyrics"])
        self.assertTrue(listing["music"][0]["hasCover"])

        cover = self.client.get(f"/music/cover/{filename}")
        self.assertEqual(cover.status_code, 200)
        self.assertEqual(cover.get_data(), b"fake png bytes")
        cover.close()

        replaced = self.json(
            self.client.post(
                "/music/cover/upload",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                data={
                    "filename": filename,
                    "cover": (io.BytesIO(b"replacement webp"), "replacement.webp"),
                },
                content_type="multipart/form-data",
            )
        )
        self.assertTrue(replaced["hasCover"])
        replaced_cover = self.client.get(f"/music/cover/{filename}")
        self.assertEqual(replaced_cover.get_data(), b"replacement webp")
        replaced_cover.close()

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
        deleted_cover = self.json(self.client.get(f"/music/cover/{filename}"))
        self.assertFalse(deleted_cover["success"])
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

    def test_github_language_stats_use_repository_count(self):
        source = Path(backend.__file__).read_text(encoding="utf-8")
        self.assertIn("affiliations: [OWNER]", source)
        self.assertIn("first: 100", source)
        self.assertNotIn("orderBy: {field: STARGAZERS", source)
        self.assertIn("'repoCount': repo_count", source)

    def test_github_languages_add_distinct_external_contribution_repositories(self):
        viewer = {
            "login": "contract-user",
            "repositories": {"nodes": [{
                "name": "owned-python",
                "nameWithOwner": "contract-user/owned-python",
                "primaryLanguage": {"name": "Python", "color": "#3572A5"},
            }]},
            "contributionsCollection": {
                "commitContributionsByRepository": [
                    {
                        "repository": {
                            "nameWithOwner": "open-source/web-app",
                            "owner": {"login": "open-source"},
                            "primaryLanguage": {"name": "TypeScript", "color": "#3178c6"},
                        },
                        "contributions": {"totalCount": 12},
                    },
                    {
                        "repository": {
                            "nameWithOwner": "OPEN-SOURCE/WEB-APP",
                            "owner": {"login": "open-source"},
                            "primaryLanguage": {"name": "TypeScript", "color": "#3178c6"},
                        },
                        "contributions": {"totalCount": 2},
                    },
                    {
                        "repository": {
                            "nameWithOwner": "contract-user/owned-python",
                            "owner": {"login": "contract-user"},
                            "primaryLanguage": {"name": "Python", "color": "#3572A5"},
                        },
                        "contributions": {"totalCount": 30},
                    },
                ]
            },
        }

        languages = backend._aggregate_github_languages(viewer)
        self.assertEqual(
            [(item["name"], item["repoCount"]) for item in languages],
            [("Python", 1), ("TypeScript", 1)],
        )

    def test_github_stats_cache_is_reused_for_24_hours(self):
        cache_file = workspace / "github-stats-cache.json"
        payload = {
            "username": "cache-user",
            "totalContributions": 9,
            "days": [],
            "topLanguages": [],
        }
        with (
            mock.patch.object(backend, "GITHUB_CACHE_FILE", str(cache_file)),
            mock.patch.object(
                backend, "_fetch_github_contributions_from_api", return_value=payload
            ) as fetch,
        ):
            self.assertEqual(backend.fetch_github_contributions(), payload)
            self.assertEqual(backend.fetch_github_contributions(), payload)

        self.assertEqual(fetch.call_count, 1)
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertIn("cachedAt", cached)
        self.assertEqual(cached["data"], payload)

    def test_github_stats_expired_cache_refreshes_and_falls_back(self):
        cache_file = workspace / "github-stats-expired.json"
        stale = {
            "username": "stale-user",
            "totalContributions": 3,
            "days": [],
            "topLanguages": [],
        }
        cache_file.write_text(json.dumps({
            "version": backend.GITHUB_CACHE_VERSION,
            "cachedAt": "2026-08-01T00:00:00+00:00",
            "cachedAtEpoch": 1,
            "data": stale,
        }), encoding="utf-8")
        with (
            mock.patch.object(backend, "GITHUB_CACHE_FILE", str(cache_file)),
            mock.patch.object(
                backend,
                "_fetch_github_contributions_from_api",
                return_value={"error": "GitHub API HTTP 403"},
            ) as fetch,
        ):
            self.assertEqual(backend.fetch_github_contributions(), stale)

        fetch.assert_called_once_with()

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
