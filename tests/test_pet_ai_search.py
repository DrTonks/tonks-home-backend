# coding: utf-8
"""Tests for fixed-host search parsing and fallback."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from pet_ai.search import FixedHostWebSearch


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _maximum):
        return self.body


class FixedHostWebSearchTests(unittest.TestCase):
    def test_falls_back_to_bing_rss_when_duckduckgo_has_no_results(self):
        empty_duck = b"<html><body>no results</body></html>"
        bing = """<?xml version="1.0" encoding="utf-8"?>
        <rss><channel><item><title>缎带英雄 | Netflix</title>
        <description>改编自手冢治虫作品的动画电影。</description></item></channel></rss>
        """.encode("utf-8")
        with mock.patch(
            "pet_ai.search.urllib.request.urlopen",
            side_effect=[FakeResponse(empty_duck), FakeResponse(bing)],
        ) as urlopen:
            results = FixedHostWebSearch().search("缎带英雄 动画 番剧")

        self.assertEqual(len(urlopen.call_args_list), 2)
        self.assertEqual(results[0]["title"], "缎带英雄 | Netflix")
        self.assertIn("动画电影", results[0]["snippet"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
