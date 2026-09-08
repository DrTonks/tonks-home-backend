from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest import mock

from comment_moderation import CommentModerationService, ModerationResult
from community import (
    _retention_expired,
    CommunityBurstLimiter,
    CommunityRateLimitExceeded,
    CommunityStore,
    CommunityValidationError,
    validate_friend_application_payload,
    validate_comment_payload,
    validate_feedback_message_payload,
    validate_feedback_topic_payload,
)
from pet_ai.provider import ProviderError


class CommunityValidationTests(unittest.TestCase):
    def test_comment_requires_a_well_formed_email(self):
        valid = validate_comment_payload(
            "about",
            {
                "nickname": "  Tonks  ",
                "email": " Person@Example.COM ",
                "website": "https://tonks.top/",
                "content": "你好\n\n这是一条评论。",
            },
        )
        self.assertEqual(valid.nickname, "Tonks")
        self.assertEqual(valid.email, "person@example.com")
        with self.assertRaises(CommunityValidationError):
            validate_comment_payload(
                "about",
                {"nickname": "T", "email": "not-an-email", "content": "hello"},
            )

    def test_only_about_and_friends_accept_comments(self):
        with self.assertRaises(CommunityValidationError):
            validate_comment_payload(
                "post",
                {"nickname": "T", "email": "t@example.com", "content": "hello"},
            )

    def test_friend_application_requires_site_and_description(self):
        valid = validate_friend_application_payload(
            {
                "name": "Example",
                "website": "https://example.com/",
                "description": "一个认真更新的小站",
            }
        )
        self.assertEqual(valid.website, "https://example.com/")
        self.assertEqual(valid.email, "")
        with self.assertRaises(CommunityValidationError):
            validate_friend_application_payload(
                {"name": "Example", "website": "https://example.com/", "description": ""}
            )


class CommunityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = Path(
            tempfile.mkdtemp(prefix=f"sleepy-community-{uuid.uuid4().hex}-")
        )
        self.store = CommunityStore(str(self.temporary_directory / "community.sqlite3"))

    def tearDown(self):
        shutil.rmtree(self.temporary_directory)

    @staticmethod
    def submission(page="about", **changes):
        payload = {
            "nickname": "Tonks",
            "email": "tonks@example.com",
            "website": "https://tonks.top/",
            "content": "欢迎来到这里。",
            **changes,
        }
        return validate_comment_payload(page, payload)

    def purge_comment(self, **kwargs):
        with mock.patch.object(self.store, "_lazy_purge"):
            return self.store.create_comment(self.submission(), "purge-test", status="published", **kwargs)

    def age_comments(self, ids, timestamp="2025-11-30T12:00:00+00:00"):
        with self.store._connect() as connection:
            connection.executemany(
                "UPDATE community_comments SET status = 'deleted', deleted_at = ? WHERE id = ?",
                [(timestamp, i) for i in ids],
            )

    def comment_ids(self):
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            return {row[0] for row in connection.execute("SELECT id FROM community_comments")}

    def test_retention_calendar_boundaries(self):
        for deleted, expires in [
            ("2025-11-30T12:00:00+00:00", "2026-02-28T12:00:00+00:00"),
            ("2023-11-30T12:00:00+00:00", "2024-02-29T12:00:00+00:00"),
            ("2026-01-31T12:00:00+00:00", "2026-04-30T12:00:00+00:00"),
            ("2026-02-28T12:00:00+00:00", "2026-05-28T12:00:00+00:00"),
            ("2025-11-30T20:00:00+08:00", "2026-02-28T12:00:00+00:00"),
        ]:
            with self.subTest(deleted=deleted):
                boundary = datetime.fromisoformat(expires)
                self.assertFalse(_retention_expired(deleted, boundary - timedelta(microseconds=1)))
                self.assertTrue(_retention_expired(deleted, boundary))
        for value in (None, "", "bad-date", "9999-12-31T00:00:00+00:00"):
            self.assertFalse(_retention_expired(value, datetime(2026, 9, 8, tzinfo=timezone.utc)))

    def test_deleted_at_migration_starts_now_and_excludes_feedback(self):
        old = self.purge_comment(now=datetime(2020, 1, 1, tzinfo=timezone.utc))
        moved = self.purge_comment()
        topic = self.store.attach_comment_tree_to_feedback(moved["id"])
        with self.store._connect() as connection:
            connection.execute("UPDATE community_comments SET status = 'deleted' WHERE id = ?", (old["id"],))
            connection.execute("ALTER TABLE community_comments DROP COLUMN deleted_at")
        migration = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
        with mock.patch("community.datetime", wraps=datetime) as clock:
            clock.now.return_value = migration
            self.store.initialize()
            clock.now.return_value = migration + timedelta(days=10)
            self.store.initialize()
        with self.store._connect() as connection:
            rows = {row["id"]: row for row in connection.execute("SELECT * FROM community_comments")}
        self.assertEqual(rows[old["id"]]["deleted_at"], migration.isoformat())
        self.assertIsNone(rows[moved["id"]]["deleted_at"])
        self.assertEqual(rows[moved["id"]]["moved_to_feedback_id"], topic)

    def test_delete_tree_timestamps_preserve_first_deletion(self):
        root = self.purge_comment()
        child = self.purge_comment(parent_context=self.store.get_parent_context("about", root["id"]))
        first = datetime(2026, 1, 31, tzinfo=timezone.utc)
        with mock.patch("community.datetime", wraps=datetime) as clock:
            clock.now.return_value = first
            self.store.delete_comment(child["id"])
            clock.now.return_value = first + timedelta(days=2)
            self.store.delete_comment(root["id"])
        with self.store._connect() as connection:
            rows = {row["id"]: row["deleted_at"] for row in connection.execute("SELECT * FROM community_comments")}
        self.assertEqual(rows[child["id"]], first.isoformat())
        self.assertEqual(rows[root["id"]], (first + timedelta(days=2)).isoformat())

    def test_purge_only_entire_eligible_trees(self):
        root = self.purge_comment()
        child = self.purge_comment(parent_context=self.store.get_parent_context("about", root["id"]))
        grandchild = self.purge_comment(parent_context=self.store.get_parent_context("about", child["id"]))
        live = self.purge_comment()
        partial = self.purge_comment(parent_context=self.store.get_parent_context("about", live["id"]))
        self.age_comments([root["id"], child["id"], partial["id"]])
        self.age_comments([grandchild["id"]], "2025-12-01T12:00:00+00:00")
        self.store._lazy_purge(datetime(2026, 2, 28, 12, tzinfo=timezone.utc))
        self.assertEqual(len(self.comment_ids()), 5)
        self.store.create_comment(self.submission(), "trigger", status="published",
                                  now=datetime(2026, 3, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(self.comment_ids() & {root["id"], child["id"], grandchild["id"]}, set())
        self.assertTrue({live["id"], partial["id"]} <= self.comment_ids())

    def test_feedback_sources_and_moved_comments_protected(self):
        root = self.purge_comment()
        topic = self.store.attach_comment_tree_to_feedback(root["id"])
        self.store.delete_feedback_topic(topic)
        self.age_comments([root["id"]])
        with self.store._connect() as connection:
            connection.execute("UPDATE community_feedback_topics SET deleted_at = '2025-01-01T00:00:00+00:00'")
        self.store._lazy_purge(datetime(2026, 2, 28, tzinfo=timezone.utc))
        self.assertIn(root["id"], self.comment_ids())
        # A source reference protects a comment even without the logical moved flag.
        with self.store._connect() as connection:
            connection.execute("UPDATE community_comments SET moved_to_feedback_id = NULL")
            connection.execute("UPDATE community_feedback_topics SET deleted_at = NULL")
        self.store._lazy_purge(datetime(2026, 3, 1, tzinfo=timezone.utc))
        self.assertIn(root["id"], self.comment_ids())

    def test_feedback_purge_dependencies_and_incoming_merge_refs(self):
        def topic():
            return self.store.create_feedback_topic(validate_feedback_topic_payload({
                "title": "Test", "kind": "bug", "nickname": "T",
                "email": "t@example.com", "content": "Test",
            }), "actor", status="published", now=datetime(2025, 1, 1, tzinfo=timezone.utc))["id"]
        target, source, removable = topic(), topic(), topic()
        root = self.purge_comment()
        self.store.attach_comment_tree_to_feedback(root["id"], topic_id=removable)
        self.store.merge_feedback_topics(target, [source])
        self.store.delete_feedback_topic(target)
        self.store.delete_feedback_topic(removable)
        with self.store._connect() as connection:
            connection.execute("UPDATE community_comments SET moved_to_feedback_id = NULL")
            connection.execute("UPDATE community_feedback_topics SET deleted_at = '2025-11-30T12:00:00+00:00'")
            connection.execute("UPDATE community_feedback_topics SET deleted_at = '2026-02-01T00:00:00+00:00' WHERE id = ?", (source,))
        self.store._lazy_purge(datetime(2026, 2, 28, 12, tzinfo=timezone.utc))
        with self.store._connect() as connection:
            self.assertEqual({r[0] for r in connection.execute("SELECT id FROM community_feedback_topics")}, {target, source})
            for table in ("community_feedback_messages", "community_feedback_sources", "community_feedback_events"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE topic_id = ?", (removable,)).fetchone()[0], 0)
        self.store._lazy_purge(datetime(2026, 5, 1, tzinfo=timezone.utc))
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM community_feedback_topics").fetchone()[0], 0)
        self.comment_ids()

    def test_daily_gate_persists_and_uses_utc(self):
        self.store.initialize()
        with mock.patch.object(CommunityStore, "_purge_deleted_records") as purge:
            for timestamp in ("2026-03-01T23:00:00+00:00", "2026-03-02T07:30:00+08:00",
                              "2026-03-02T08:00:00+08:00"):
                CommunityStore(self.store.database_path).create_comment(
                    self.submission(), "trigger", status="pending", now=datetime.fromisoformat(timestamp))
            self.assertEqual(purge.call_count, 2)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT value FROM community_metadata WHERE key = 'last_purge_date'").fetchone()[0], "2026-03-02")

    def test_purge_failure_rolls_back_but_comment_and_daily_claim_commit(self):
        old = self.purge_comment()
        self.age_comments([old["id"]])
        def fail(connection, now):
            connection.execute("DELETE FROM community_comments WHERE id = ?", (old["id"],))
            raise sqlite3.OperationalError("injected failure")
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        with mock.patch.object(self.store, "_purge_deleted_records", side_effect=fail) as purge:
            with self.assertLogs("community", level="ERROR"):
                accepted = self.store.create_comment(self.submission(), "new", status="published", now=now)
            self.store.create_comment(self.submission(), "second", status="published", now=now)
            self.assertEqual(purge.call_count, 1)
        self.assertTrue({old["id"], accepted["id"]} <= self.comment_ids())
        with mock.patch.object(self.store, "_lazy_purge", side_effect=sqlite3.OperationalError("connection failed")):
            with self.assertLogs("community", level="ERROR"):
                accepted = self.store.create_comment(self.submission(), "third", status="published", now=now)
        self.assertIn(accepted["id"], self.comment_ids())
        self.store._lazy_purge(now + timedelta(days=1))
        self.assertNotIn(old["id"], self.comment_ids())

    def test_concurrent_comment_insertions_claim_one_purge(self):
        self.store.initialize()
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        def submit(_):
            return CommunityStore(self.store.database_path).create_comment(
                self.submission(), "parallel", status="published", now=now)["id"]
        with mock.patch.object(CommunityStore, "_purge_deleted_records") as purge:
            with ThreadPoolExecutor(max_workers=4) as executor:
                ids = list(executor.map(submit, range(8)))
            self.assertEqual(purge.call_count, 1)
        self.assertEqual(len(set(ids)), 8)
        self.assertEqual(self.comment_ids(), set(ids))

    def test_failed_insert_and_reads_do_not_trigger_purge(self):
        self.store.initialize()
        with mock.patch.object(self.store, "_lazy_purge") as purge:
            self.store.list_public_comments("about")
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.create_comment(self.submission(), "bad", status="published",
                                          parent_context={"id": 999, "root_id": 999, "nickname": "missing"})
            purge.assert_not_called()

    def submit_purge_kind(self, kind, now, status="published"):
        payload = {"title": "Private test title", "kind": "bug", "nickname": "Private name",
                   "email": "private@example.com", "content": "Private message"}
        if kind == "comment":
            return self.store.create_comment(self.submission(), "actor", status=status, now=now)
        if kind == "topic":
            return self.store.create_feedback_topic(validate_feedback_topic_payload(payload),
                                                    "actor", status=status, now=now)
        if kind == "room":
            return self.store.add_feedback_room_message(validate_feedback_message_payload(payload),
                                                        "actor", status=status, now=now)
        with mock.patch.object(self.store, "_purge_after_submission"):
            topic = self.submit_purge_kind("topic", now)
        return self.store.add_feedback_message(topic["id"], validate_feedback_message_payload(payload),
                                                "actor", status=status, now=now)

    def test_all_submission_kinds_trigger_real_purge(self):
        for offset, kind in enumerate(("comment", "room", "reply", "topic")):
            with self.subTest(kind=kind):
                old = self.purge_comment()
                self.age_comments([old["id"]])
                self.submit_purge_kind(kind, datetime(2030, 1, 1 + offset, tzinfo=timezone.utc))
                self.assertNotIn(old["id"], self.comment_ids())

    def test_submission_kinds_share_daily_gate_and_skip_deleted(self):
        self.store.initialize()
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(self.store, "_purge_deleted_records", return_value={}) as purge:
            for kind in ("comment", "room", "reply", "topic"):
                self.submit_purge_kind(kind, now, status="deleted")
            purge.assert_not_called()
            for kind in ("comment", "room", "reply", "topic"):
                for status in ("published", "pending", "rejected"):
                    self.submit_purge_kind(kind, now, status=status)
            self.assertEqual(purge.call_count, 1)
            self.submit_purge_kind("room", now + timedelta(days=1))
            self.assertEqual(purge.call_count, 2)

    def test_feedback_submission_failure_isolation_and_commit(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for kind, table in (("room", "community_feedback_room_messages"),
                            ("reply", "community_feedback_messages"),
                            ("topic", "community_feedback_topics")):
            with self.subTest(kind=kind):
                with mock.patch.object(self.store, "_lazy_purge", side_effect=sqlite3.OperationalError("injected")):
                    with self.assertLogs("community", level="ERROR"):
                        accepted = self.submit_purge_kind(kind, now)
                with self.store._connect() as connection:
                    self.assertIsNotNone(connection.execute(
                        f"SELECT id FROM {table} WHERE id = ?", (accepted["id"],)
                    ).fetchone())
        with mock.patch.object(self.store, "_lazy_purge") as purge:
            with self.assertRaises(CommunityValidationError):
                self.store.add_feedback_message(999999, validate_feedback_message_payload({
                    "nickname": "T", "email": "t@example.com", "content": "test"
                }), "actor", status="published", now=now)
            purge.assert_not_called()

    def test_purge_logs_exact_committed_counts_without_content(self):
        with mock.patch.object(self.store, "_purge_after_submission"):
            topic = self.submit_purge_kind("topic", datetime(2025, 1, 1, tzinfo=timezone.utc))
        root = self.purge_comment()
        self.store.attach_comment_tree_to_feedback(root["id"], topic_id=topic["id"])
        self.store.delete_feedback_topic(topic["id"])
        self.age_comments([root["id"]])
        with self.store._connect() as connection:
            connection.execute("UPDATE community_comments SET moved_to_feedback_id = NULL")
            connection.execute("UPDATE community_feedback_topics SET deleted_at = '2025-01-01T00:00:00+00:00'")
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with self.assertLogs("community", level="INFO") as logs:
            self.submit_purge_kind("room", now)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].args, {
            "community_comments": 1, "community_feedback_topics": 1,
            "community_feedback_messages": 1, "community_feedback_sources": 1,
            "community_feedback_events": 3,
        })
        self.assertNotIn("Private", logs.output[0])
        self.assertNotIn("@", logs.output[0])
        with self.assertNoLogs("community", level="INFO"):
            self.submit_purge_kind("room", now)
        with self.assertLogs("community", level="INFO") as logs:
            self.submit_purge_kind("room", now + timedelta(days=1))
        self.assertEqual(set(logs.records[0].args.values()), {0})
        real_purge = self.store._purge_deleted_records
        def fail(connection, timestamp):
            real_purge(connection, timestamp)
            raise sqlite3.OperationalError("rollback")
        with mock.patch.object(self.store, "_purge_deleted_records", side_effect=fail):
            with self.assertLogs("community", level="INFO") as logs:
                self.submit_purge_kind("room", now + timedelta(days=2))
        self.assertEqual([record.levelname for record in logs.records], ["ERROR"])

    def test_pin_visibility_moderation_and_deletion(self):
        comment = self.store.create_comment(self.submission(), "a", status="published")
        pending = self.store.create_comment(self.submission(), "b", status="pending")
        self.assertFalse(self.store.set_comment_pin(pending["id"], True))
        with self.assertRaises(CommunityValidationError):
            self.store.set_comment_pin(comment["id"], "false")
        self.assertTrue(self.store.set_comment_pin(comment["id"], True))
        self.assertTrue(self.store.list_public_comments("about")[0]["is_pinned"])
        self.store.update_comment_status(comment["id"], "rejected")
        self.store.update_comment_status(comment["id"], "published")
        self.assertFalse(self.store.list_public_comments("about")[0]["is_pinned"])
        self.store.set_comment_pin(comment["id"], True)
        self.store.delete_comment(comment["id"])
        self.assertEqual(self.store.list_public_comments("about"), [])
        self.assertFalse(self.store.set_comment_pin(comment["id"], True))

    def test_latest_window_keeps_pins_and_reply_ancestors(self):
        root = self.store.create_comment(self.submission(), "a", status="published")
        pinned = self.store.create_comment(self.submission(), "b", status="published")
        self.store.set_comment_pin(pinned["id"], True)
        self.store.create_comment(self.submission(), "c", status="published")
        reply = self.store.create_comment(
            self.submission(parent_id=root["id"]), "d", status="published",
            parent_context=self.store.get_parent_context("about", root["id"]),
        )
        rows = self.store.list_public_comments("about", limit=1)
        self.assertEqual([row["id"] for row in rows], [root["id"], pinned["id"], reply["id"]])
        self.assertEqual(self.store.list_public_comments("friends", limit=1), [])
        self.store.set_comment_pin(pinned["id"], False)
        self.assertEqual([row["id"] for row in self.store.list_public_comments("about", limit=1)],
                         [root["id"], reply["id"]])

    def test_like_toggle_is_unique_per_target_and_identity(self):
        self.assertEqual(self.store.toggle_like("page:about", "visitor-a"), (1, True))
        self.assertEqual(self.store.toggle_like("page:about", "visitor-a"), (0, False))
        self.store.toggle_like("page:about", "visitor-a")
        self.store.toggle_like("page:about", "visitor-b")
        state = self.store.get_likes(["page:about", "page:friends"], "visitor-a")
        self.assertEqual(state["page:about"], {"count": 2, "liked": True})
        self.assertEqual(state["page:friends"], {"count": 0, "liked": False})

    def test_public_comment_tree_never_exposes_email_or_actor_hash(self):
        root = self.store.create_comment(
            self.submission(),
            "actor-a",
            status="published",
            owner_hash="owner-a",
        )
        parent = self.store.get_parent_context("about", root["id"])
        reply = self.store.create_comment(
            self.submission(parent_id=root["id"], content="谢谢你的留言。"),
            "actor-b",
            status="published",
            parent_context=parent,
        )
        comments = self.store.list_public_comments(
            "about", viewer_owner_hash="owner-a"
        )
        self.assertEqual([item["id"] for item in comments], [root["id"], reply["id"]])
        self.assertEqual(comments[1]["root_id"], root["id"])
        self.assertEqual(comments[1]["reply_to_name"], "Tonks")
        self.assertNotIn("email", json.dumps(comments))
        self.assertNotIn("actor_hash", json.dumps(comments))
        self.assertNotIn("owner_hash", json.dumps(comments))
        self.assertTrue(comments[0]["owned"])
        self.assertFalse(comments[1]["owned"])
        self.assertFalse(self.store.list_public_comments("about")[0]["owned"])
        self.assertEqual(comments[0]["author_key"], root["author_key"])
        self.assertNotEqual(comments[0]["author_key"], comments[1]["author_key"])
        self.assertEqual(len(comments[0]["author_key"]), 20)

    def test_history_includes_rejected_attempts_for_future_moderation(self):
        self.store.create_comment(
            self.submission(content="正常评论"), "same-actor", status="published"
        )
        self.store.create_comment(
            self.submission(content="重复广告"),
            "same-actor",
            status="rejected",
            moderation_reason="advertising",
        )
        history = self.store.actor_history("same-actor")
        self.assertEqual([item["status"] for item in history], ["published", "rejected"])

    def test_admin_comment_flag_and_recursive_soft_delete(self):
        root = self.store.create_comment(
            self.submission(), "actor-a", status="published", is_admin=True
        )
        parent = self.store.get_parent_context("about", root["id"])
        reply = self.store.create_comment(
            self.submission(parent_id=root["id"], content="回复"),
            "actor-b",
            status="published",
            parent_context=parent,
        )
        self.assertTrue(self.store.list_public_comments("about")[0]["is_admin"])
        self.assertEqual(self.store.delete_comment(root["id"]), [root["id"], reply["id"]])
        self.assertEqual(self.store.list_public_comments("about"), [])

    def test_admin_comment_list_includes_private_fields_and_updates_status(self):
        comment = self.store.create_comment(
            self.submission(email="contact@example.com", content="请人工确认"),
            "actor-a",
            status="pending",
            moderation_reason="moderation_unavailable: provider failed",
        )
        self.assertEqual(self.store.list_public_comments("about"), [])
        admin_comments = self.store.list_public_comments(
            "about", include_nonpublished=True
        )
        self.assertEqual(admin_comments[0]["email"], "contact@example.com")
        self.assertIn("provider failed", admin_comments[0]["moderation_reason"])
        self.assertTrue(
            self.store.update_comment_status(
                comment["id"], "published", "人工审核通过"
            )
        )
        public_comments = self.store.list_public_comments("about")
        self.assertEqual(public_comments[0]["status"], "published")
        self.assertNotIn("email", public_comments[0])
        with self.assertRaises(CommunityValidationError):
            self.store.update_comment_status(comment["id"], "pending")

    def test_friend_application_lifecycle(self):
        from community import FriendApplicationSubmission

        application = self.store.create_friend_application(
            FriendApplicationSubmission(
                name="Example",
                website="https://example.com/",
                avatar="",
                description="一个小站",
                email="owner@example.com",
            ),
            "submitter-hash",
            tracking_hash="tracking-hash",
        )
        self.assertEqual(application["status"], "pending")
        tracked = self.store.list_friend_applications_by_tracking_hashes(
            ["tracking-hash"]
        )
        self.assertEqual([item["id"] for item in tracked], [application["id"]])
        self.assertNotIn("email", tracked[0])
        self.assertEqual(
            self.store.list_friend_applications_by_tracking_hashes(["missing"]), []
        )
        self.assertTrue(self.store.update_friend_application(application["id"], "approved"))
        self.assertEqual(self.store.list_friend_applications()[0]["status"], "approved")

    def test_feedback_lifecycle_preserves_sources_and_merge_history(self):
        first = self.store.create_feedback_topic(
            validate_feedback_topic_payload(
                {
                    "title": "移动端关闭按钮失效",
                    "kind": "bug",
                    "nickname": "Visitor",
                    "email": "visitor@example.com",
                    "website": "",
                    "content": "右上角按钮无法点击。",
                }
            ),
            "feedback-actor",
            status="published",
            owner_hash="feedback-owner",
        )
        self.store.add_feedback_message(
            first["id"],
            validate_feedback_message_payload(
                {
                    "nickname": "Tonks",
                    "email": "tonks@example.com",
                    "website": "https://tonks.top/",
                    "content": "已经定位到遮罩层级。",
                }
            ),
            "admin-actor",
            status="published",
            is_admin=True,
        )
        root = self.store.create_comment(
            self.submission(content="友链页面也有相同问题"),
            "comment-actor",
            status="published",
        )
        self.store.attach_comment_tree_to_feedback(root["id"], topic_id=first["id"])
        self.assertEqual(self.store.list_public_comments("about"), [])
        room_message = self.store.add_feedback_room_message(
            validate_feedback_message_payload(
                {
                    "nickname": "Visitor",
                    "email": "visitor@example.com",
                    "website": "",
                    "content": "谢谢，修好后我再确认一下。",
                }
            ),
            "feedback-actor",
            status="published",
            owner_hash="feedback-owner",
        )
        room_messages = self.store.list_feedback_room_messages(
            viewer_owner_hash="feedback-owner"
        )
        self.assertEqual(room_messages[0]["id"], room_message["id"])
        self.assertTrue(room_messages[0]["owned"])
        second = self.store.create_feedback_topic(
            validate_feedback_topic_payload(
                {
                    "title": "重复反馈",
                    "kind": "bug",
                    "nickname": "Other",
                    "email": "other@example.com",
                    "content": "同一个关闭问题。",
                }
            ),
            "other-actor",
            status="published",
        )
        self.assertEqual(
            self.store.merge_feedback_topics(first["id"], [second["id"]]),
            [second["id"]],
        )
        self.assertTrue(
            self.store.update_feedback_topic(
                first["id"], status="resolved", resolution_note="已修复层级"
            )
        )
        topics = self.store.list_feedback_topics(viewer_owner_hash="feedback-owner")
        retained = next(item for item in topics if item["id"] == first["id"])
        merged = next(item for item in topics if item["id"] == second["id"])
        self.assertEqual(retained["status"], "resolved")
        self.assertEqual(len(retained["messages"]), 3)
        self.assertEqual(retained["sources"][0]["root_comment_id"], root["id"])
        self.assertTrue(retained["owned"])
        self.assertNotIn("email", json.dumps(topics))
        self.assertEqual(merged["merged_into_id"], first["id"])
        admin_topics = self.store.list_feedback_topics(include_nonpublished=True)
        self.assertEqual(
            next(item for item in admin_topics if item["id"] == first["id"])["email"],
            "visitor@example.com",
        )
        self.assertEqual(
            self.store.delete_feedback_topic(first["id"]),
            [first["id"], second["id"]],
        )
        self.assertEqual(self.store.list_feedback_topics(include_nonpublished=True), [])
        self.assertEqual(self.store.delete_feedback_topic(first["id"]), [])
        self.assertEqual(
            [item["content"] for item in self.store.feedback_actor_history("feedback-actor")],
            ["谢谢，修好后我再确认一下。"],
        )

    def test_daily_quota_is_persistent_for_each_identity_dimension(self):
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        self.store.reserve_comment_quota(
            {"email:a", "ip:a", "client:a"}, daily_limit=2, now=now
        )
        self.store.reserve_comment_quota(
            {"email:a", "ip:a", "client:b"}, daily_limit=2, now=now
        )
        reloaded = CommunityStore(self.store.database_path)
        with self.assertRaises(CommunityRateLimitExceeded):
            reloaded.reserve_comment_quota(
                {"email:a", "ip:a", "client:c"}, daily_limit=2, now=now
            )


class ModerationTests(unittest.TestCase):
    class FakeProvider:
        def __init__(self, content=None, error=None):
            self.content = content
            self.error = error
            self.calls = []

        def chat(self, messages, **options):
            self.calls.append((messages, options))
            if self.error:
                raise ProviderError(self.error)
            return {"content": self.content}

    def test_strict_json_result_and_history_are_sent_without_email(self):
        provider = self.FakeProvider(
            '{"decision":"reject","category":"flooding","reason":"重复灌水"}'
        )
        service = CommentModerationService(provider=provider)
        result = service.moderate(
            page="friends",
            nickname="Visitor",
            content="再次发送",
            reply_to_name="Tonks",
            history=[{"content": "重复", "status": "published"}],
        )
        self.assertEqual(result.decision, "reject")
        user_payload = provider.calls[0][0][1]["content"]
        self.assertIn("same_email_history", user_payload)
        self.assertNotIn("@", user_payload)
        self.assertEqual(provider.calls[0][1]["temperature"], 0.0)

    def test_provider_failure_becomes_review_instead_of_fail_open(self):
        service = CommentModerationService(
            provider=self.FakeProvider(error="provider_unavailable")
        )
        result = service.moderate(
            page="about",
            nickname="Visitor",
            content="hello",
            reply_to_name="",
            history=[],
        )
        self.assertEqual(result.decision, "review")


class CommunityRouteTests(unittest.TestCase):
    def setUp(self):
        import server

        self.server = server
        self.temporary_directory = Path(
            tempfile.mkdtemp(prefix=f"sleepy-community-route-{uuid.uuid4().hex}-")
        )
        self.store = CommunityStore(str(self.temporary_directory / "community.sqlite3"))
        self.patches = [
            mock.patch.object(server, "community_store", self.store),
            mock.patch.object(server, "community_comment_limiter", CommunityBurstLimiter(100)),
            mock.patch.object(server, "community_like_limiter", CommunityBurstLimiter(100)),
            mock.patch.object(
                server,
                "comment_moderator",
                mock.Mock(
                    moderate=mock.Mock(
                        return_value=ModerationResult("allow", "normal", "normal")
                    )
                ),
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()
        self.headers = {
            "X-Client-ID": "route-test-client",
            "X-Community-Identity": "visitor-owner-token-1234567890abcdef",
            "Content-Type": "application/json",
        }

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        shutil.rmtree(self.temporary_directory)

    def test_pin_route_requires_admin_and_boolean(self):
        comment = self.store.create_comment(CommunityStoreTests.submission(), "a", status="published")
        url = f"/blog/community/comments/{comment['id']}/pin"
        with mock.patch.object(self.server, "verify_admin_secret", return_value=False):
            self.assertEqual(self.client.patch(url, json={"is_pinned": True}).status_code, 401)
        with mock.patch.object(self.server, "verify_admin_secret", return_value=True):
            headers = {"X-Admin-Secret": "test-admin"}
            self.assertEqual(self.client.patch(url, headers=headers, json={"is_pinned": "false"}).status_code, 400)
            response = self.client.patch(url, headers=headers, json={"is_pinned": True})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["is_pinned"])
            public = self.client.get("/blog/community/comments/about").get_json()["comments"]
            self.assertTrue(public[0]["is_pinned"])
            self.assertEqual(self.client.patch(url, headers=headers, json={"is_pinned": False}).status_code, 200)
            self.assertEqual(self.client.patch("/blog/community/comments/999999/pin", headers=headers,
                                               json={"is_pinned": True}).status_code, 404)

    def test_like_and_comment_public_contract(self):
        liked = self.client.post(
            "/blog/community/likes/page:about", headers={"X-Client-ID": "visitor-a"}
        ).get_json()
        self.assertTrue(liked["success"])
        self.assertEqual(liked["count"], 1)

        created = self.client.post(
            "/blog/community/comments/about",
            headers=self.headers,
            json={
                "nickname": "Visitor",
                "email": "visitor@example.com",
                "website": "",
                "content": "很喜欢这个页面。",
            },
        ).get_json()
        self.assertEqual(created["status"], "published")
        self.assertTrue(created["comment"]["owned"])
        self.assertEqual(len(created["comment"]["author_key"]), 20)
        listed = self.client.get(
            "/blog/community/comments/about",
            headers={"X-Community-Identity": "visitor-owner-token-1234567890abcdef"},
        ).get_json()
        self.assertEqual(listed["count"], 1)
        self.assertNotIn("email", json.dumps(listed))
        self.assertEqual(
            listed["comments"][0]["author_key"], created["comment"]["author_key"]
        )
        self.assertTrue(listed["comments"][0]["owned"])
        other = self.client.get(
            "/blog/community/comments/about",
            headers={"X-Community-Identity": "another-owner-token-1234567890abcdef"},
        ).get_json()
        self.assertFalse(other["comments"][0]["owned"])

    def test_invalid_email_is_rejected_before_moderation(self):
        response = self.client.post(
            "/blog/community/comments/friends",
            headers=self.headers,
            json={"nickname": "Visitor", "email": "bad", "content": "hello"},
        ).get_json()
        self.assertFalse(response["success"])
        self.assertEqual(response["code"], "invalid_email")
        self.server.comment_moderator.moderate.assert_not_called()

    def test_admin_can_list_and_moderate_pending_comment(self):
        pending = self.store.create_comment(
            CommunityStoreTests.submission(content="等待确认"),
            "actor-review",
            status="pending",
            moderation_reason="uncertain: needs review",
        )
        public = self.client.get("/blog/community/comments/about").get_json()
        self.assertEqual(public["count"], 0)

        with mock.patch.object(self.server, "verify_admin_secret", return_value=True):
            admin_headers = {"X-Admin-Secret": "route-secret"}
            listed = self.client.get(
                "/blog/community/comments/about", headers=admin_headers
            ).get_json()
            self.assertEqual(listed["comments"][0]["email"], "tonks@example.com")
            approved = self.client.patch(
                f"/blog/community/comments/{pending['id']}",
                headers={**admin_headers, "Content-Type": "application/json"},
                json={"status": "published", "moderation_reason": "人工审核通过"},
            ).get_json()
            self.assertTrue(approved["success"])
            self.assertEqual(approved["status"], "published")

        public = self.client.get("/blog/community/comments/about").get_json()
        self.assertEqual(public["count"], 1)
        self.assertNotIn("email", json.dumps(public))

    def test_feedback_public_and_admin_contract(self):
        created = self.client.post(
            "/blog/community/feedback",
            headers=self.headers,
            json={
                "title": "移动端按钮问题",
                "kind": "bug",
                "nickname": "Visitor",
                "email": "visitor@example.com",
                "website": "",
                "content": "右上角关闭按钮无法点击。",
            },
        ).get_json()
        self.assertTrue(created["success"])
        topic_id = created["topic"]["id"]
        replied = self.client.post(
            f"/blog/community/feedback/{topic_id}/messages",
            headers=self.headers,
            json={
                "nickname": "Visitor",
                "email": "visitor@example.com",
                "website": "",
                "content": "补充：只在窄屏发生。",
            },
        ).get_json()
        self.assertEqual(replied["status"], "published")
        chatted = self.client.post(
            "/blog/community/feedback/messages",
            headers=self.headers,
            json={
                "nickname": "Visitor",
                "email": "visitor@example.com",
                "website": "",
                "content": "我先在群里补充一下复现环境。",
            },
        ).get_json()
        self.assertEqual(chatted["status"], "published")
        listed = self.client.get(
            "/blog/community/feedback",
            headers={"X-Community-Identity": self.headers["X-Community-Identity"]},
        ).get_json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(len(listed["topics"][0]["messages"]), 2)
        self.assertEqual(len(listed["room_messages"]), 1)
        self.assertTrue(listed["room_messages"][0]["owned"])
        self.assertTrue(listed["topics"][0]["owned"])
        self.assertNotIn("email", json.dumps(listed))

        comment = self.store.create_comment(
            CommunityStoreTests.submission(content="这个问题也出现在友链页"),
            "comment-actor",
            status="published",
        )
        with mock.patch.object(self.server, "verify_admin_secret", return_value=True):
            admin_headers = {"X-Admin-Secret": "route-secret", "Content-Type": "application/json"}
            converted = self.client.post(
                "/blog/community/feedback/from-comment",
                headers=admin_headers,
                json={"root_comment_id": comment["id"], "topic_id": topic_id},
            ).get_json()
            self.assertEqual(converted["topic_id"], topic_id)
            self.assertEqual(
                self.client.get("/blog/community/comments/about").get_json()["comments"],
                [],
            )
            resolved = self.client.patch(
                f"/blog/community/feedback/{topic_id}",
                headers=admin_headers,
                json={"status": "resolved", "resolution_note": "已修复"},
            ).get_json()
            self.assertTrue(resolved["success"])
            deleted = self.client.delete(
                f"/blog/community/feedback/{topic_id}",
                headers=admin_headers,
            ).get_json()
            self.assertEqual(deleted["deleted"], [topic_id])
            self.assertEqual(
                self.client.get("/blog/community/feedback").get_json()["topics"],
                [],
            )
            self.assertEqual(
                self.client.delete(
                    f"/blog/community/feedback/{topic_id}",
                    headers=admin_headers,
                ).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
