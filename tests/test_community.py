from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest import mock

from comment_moderation import CommentModerationService, ModerationResult
from community import (
    CommunityBurstLimiter,
    CommunityRateLimitExceeded,
    CommunityStore,
    CommunityValidationError,
    validate_friend_application_payload,
    validate_comment_payload,
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
        )
        parent = self.store.get_parent_context("about", root["id"])
        reply = self.store.create_comment(
            self.submission(parent_id=root["id"], content="谢谢你的留言。"),
            "actor-b",
            status="published",
            parent_context=parent,
        )
        comments = self.store.list_public_comments("about")
        self.assertEqual([item["id"] for item in comments], [root["id"], reply["id"]])
        self.assertEqual(comments[1]["root_id"], root["id"])
        self.assertEqual(comments[1]["reply_to_name"], "Tonks")
        self.assertNotIn("email", json.dumps(comments))
        self.assertNotIn("actor_hash", json.dumps(comments))
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
        )
        self.assertEqual(application["status"], "pending")
        self.assertTrue(self.store.update_friend_application(application["id"], "approved"))
        self.assertEqual(self.store.list_friend_applications()[0]["status"], "approved")

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
        self.headers = {"X-Client-ID": "route-test-client", "Content-Type": "application/json"}

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        shutil.rmtree(self.temporary_directory)

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
        self.assertEqual(len(created["comment"]["author_key"]), 20)
        listed = self.client.get("/blog/community/comments/about").get_json()
        self.assertEqual(listed["count"], 1)
        self.assertNotIn("email", json.dumps(listed))
        self.assertEqual(
            listed["comments"][0]["author_key"], created["comment"]["author_key"]
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
