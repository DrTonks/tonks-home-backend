"""Exercise production logging in fresh processes; never change capture levels."""
from __future__ import annotations

import ast
import importlib
import json
import logging
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone


REPO_DIR = Path(__file__).resolve().parents[1]
COUNTS = {
    "community_comments": 1,
    "community_feedback_topics": 1,
    "community_feedback_messages": 1,
    "community_feedback_sources": 1,
    "community_feedback_events": 3,
}


def startup_probe(entry, scenario):
    # This runs only in the child, with all data paths pointing to its temp cwd.
    sys.path.insert(0, str(REPO_DIR))
    root = logging.getLogger()
    initial_root_level = root.level
    if scenario != "default":
        handler = logging.StreamHandler()
        handler.setLevel(logging.WARNING if scenario == "warning" else logging.INFO)
        root.addHandler(handler)
    root_handlers = [(h, h.level, h.formatter, list(h.filters)) for h in root.handlers]
    unrelated = logging.getLogger("sleepy.f6.unrelated")
    unrelated_state = (unrelated.level, unrelated.propagate, list(unrelated.handlers))
    if scenario == "existing":
        logger = logging.getLogger("community")
        logger.disabled = True
        logger.setLevel(logging.ERROR)
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(logging.StreamHandler())

    def start():
        if entry == "main":
            # Real Waitress serve() still executes basicConfig(); only socket/server
            # construction is replaced, so no listener or worker is started.
            import waitress
            with mock.patch.object(waitress, "create_server") as factory:
                namespace = runpy.run_path(str(REPO_DIR / "server.py"), run_name="__main__")
                factory.assert_called_once()
                factory.return_value.run.assert_called_once_with()
                return namespace["community_store"]
        if "server" in sys.modules:
            return importlib.reload(sys.modules["server"]).community_store
        return importlib.import_module("server").community_store

    store = start()
    from community import validate_comment_payload, validate_feedback_topic_payload
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    payload = {
        "nickname": "Private name", "email": "private@example.com",
        "content": "Private message", "title": "Private title", "kind": "bug",
    }
    submission = validate_comment_payload("about", payload)
    # Fixture setup only: prevent the new topic from consuming the daily gate.
    with mock.patch.object(store, "_purge_after_submission"):
        topic = store.create_feedback_topic(
            validate_feedback_topic_payload(payload), "private-actor", status="published", now=now
        )
        comment = store.create_comment(submission, "private-actor", status="published", now=now)
    store.attach_comment_tree_to_feedback(comment["id"], topic_id=topic["id"])
    store.delete_feedback_topic(topic["id"])
    with store._connect() as connection:
        connection.execute(
            "UPDATE community_comments SET status = 'deleted', moved_to_feedback_id = NULL, "
            "deleted_at = '2025-01-01T00:00:00+00:00'"
        )
        connection.execute("UPDATE community_feedback_topics SET deleted_at = '2025-01-01T00:00:00+00:00'")
    accepted = []

    def submit(timestamp):
        accepted.append(store.create_comment(submission, "private-actor", status="published", now=timestamp)["id"])

    submit(now)  # Real submission -> committed purge, all five nonzero counts.
    with store._connect() as connection:
        fixture_removed = connection.execute(
            "SELECT id FROM community_comments WHERE id = ?", (comment["id"],)
        ).fetchone() is None
        feedback_removed = all(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                               for table in COUNTS if table != "community_comments")
    first_handlers = list(logging.getLogger("community").handlers)
    for _ in range(3):
        if "runtime_logging" in sys.modules:
            importlib.reload(sys.modules["runtime_logging"])
        store = start()
    logger = logging.getLogger("community")
    submit(now)  # Persisted daily gate stays silent after server/config reloads.
    submit(now + timedelta(days=1))  # Real successful zero-count purge.
    doomed = store.create_comment(submission, "private-actor", status="deleted", now=now)
    with store._connect() as connection:
        connection.execute("UPDATE community_comments SET deleted_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
                           (doomed["id"],))
    real_purge = store._purge_deleted_records

    def fail_after_delete(connection, timestamp):
        real_purge(connection, timestamp)
        raise sqlite3.OperationalError("injected-rollback")

    with mock.patch.object(store, "_purge_deleted_records", side_effect=fail_after_delete):
        submit(now + timedelta(days=2))
    submit(now + timedelta(days=2))  # Failed attempt also keeps the daily gate.
    with mock.patch.object(store, "_lazy_purge", side_effect=sqlite3.OperationalError("injected-outer")):
        submit(now + timedelta(days=3))
    logger.debug("community-debug-marker")
    logger.warning("community-warning-marker")
    unrelated.info("unrelated-info-marker")
    unrelated.warning("unrelated-warning-marker")
    with store._connect() as connection:
        ids = {row[0] for row in connection.execute("SELECT id FROM community_comments")}
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    print(json.dumps({
        "root_level": root.level, "initial_root_level": initial_root_level,
        "root_handlers_preserved": all(h in root.handlers and (h.level, h.formatter, list(h.filters)) == (level, fmt, filters)
                                       for h, level, fmt, filters in root_handlers),
        "root_handler_count": len(root.handlers),
        "unrelated_unchanged": unrelated_state == (unrelated.level, unrelated.propagate, list(unrelated.handlers)),
        "handler_reused": first_handlers == logger.handlers,
        "handler_count": len(logger.handlers), "propagate": logger.propagate,
        "fixture_removed": fixture_removed and feedback_removed,
        "accepted_persisted": set(accepted) <= ids, "rollback_preserved": doomed["id"] in ids,
        "foreign_keys_ok": not foreign_keys,
    }))


class CommunityStartupLoggingTests(unittest.TestCase):
    def check_startup(self, scenario):
        for entry in ("import", "main"):
            with self.subTest(entry=entry, scenario=scenario), tempfile.TemporaryDirectory(prefix="sleepy-f6-") as directory:
                root = Path(directory)
                (root / "data.json").write_text('{"host":"127.0.0.1","port":9010}', encoding="utf-8")
                env = {key: value for key, value in os.environ.items() if not key.startswith("SLEEPY_")}
                env.update({
                    "SLEEPY_ENV_FILE": str(root / "missing.env"),
                    "SLEEPY_MUSIC_DIR": str(root / "music"),
                    "SLEEPY_GITHUB_CACHE_FILE": str(root / "github.json"),
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                })
                for key in ("ANALYTICS", "AGENT_ACTIVITY", "RECOMMENDATIONS", "COMMUNITY"):
                    env[f"SLEEPY_{key}_DB"] = str(root / f"{key.lower()}.sqlite3")
                result = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--probe", entry, scenario],
                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state = json.loads(result.stdout.splitlines()[-1])
                logs = result.stderr
                success_lines = [line for line in logs.splitlines() if "Community lazy purge committed: counts=" in line]
                self.assertEqual(len(success_lines), 2, logs)
                counts = [ast.literal_eval(line.split("counts=", 1)[1]) for line in success_lines]
                self.assertEqual(counts, [COUNTS, dict.fromkeys(COUNTS, 0)])
                self.assertTrue(all("INFO community" in line for line in success_lines))
                self.assertEqual(logs.count("Community lazy purge rolled back"), 1, logs)
                self.assertEqual(logs.count("Community lazy purge failed after submission commit"), 1, logs)
                self.assertEqual(logs.count("Traceback (most recent call last):"), 2, logs)
                self.assertEqual(logs.count("community-warning-marker"), 1, logs)
                self.assertEqual(logs.count("unrelated-warning-marker"), 1, logs)
                for private in ("Private", "private@example.com", "private-actor", "unrelated-info-marker", "community-debug-marker"):
                    self.assertNotIn(private, logs)
                self.assertEqual(state["root_level"], logging.WARNING)
                self.assertEqual(state["initial_root_level"], logging.WARNING)
                self.assertEqual(state["root_handler_count"], int(entry == "main" or scenario != "default"))
                self.assertEqual(state["handler_count"], 1)
                self.assertFalse(state["propagate"])
                for key in ("root_handlers_preserved", "unrelated_unchanged", "handler_reused", "fixture_removed",
                            "accepted_persisted", "rollback_preserved", "foreign_keys_ok"):
                    self.assertTrue(state[key], key)

    def test_default_startup_outputs_committed_counts(self):
        self.check_startup("default")

    def test_root_warning_handler_cannot_suppress_counts(self):
        self.check_startup("warning")

    def test_root_info_handler_does_not_duplicate_records(self):
        self.check_startup("info")

    def test_existing_community_handlers_are_replaced_once(self):
        self.check_startup("existing")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        startup_probe(*sys.argv[2:])
    else:
        unittest.main()
