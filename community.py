"""SQLite-backed likes and moderated comments for the public blog."""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


COMMENT_PAGES = {"about", "friends"}
COMMENT_STATUSES = {"published", "pending", "rejected", "deleted"}
COMMENT_MODERATION_STATUSES = {"published", "rejected"}
FRIEND_APPLICATION_STATUSES = {"pending", "approved", "rejected"}
FEEDBACK_KINDS = {"bug", "suggestion", "content", "other"}
FEEDBACK_STATUSES = {"open", "in_progress", "resolved", "merged"}
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
BLOG_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def public_author_key(actor_hash: str) -> str:
    """Return a stable public member key without exposing the internal actor hash."""
    return hashlib.sha256(
        f"community-public-author|{actor_hash}".encode("utf-8")
    ).hexdigest()[:20]


class CommunityValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CommunityRateLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class CommentSubmission:
    page: str
    parent_id: int | None
    nickname: str
    email: str
    website: str
    content: str


@dataclass(frozen=True)
class FriendApplicationSubmission:
    name: str
    website: str
    avatar: str
    description: str
    email: str


@dataclass(frozen=True)
class FeedbackSubmission:
    nickname: str
    email: str
    website: str
    content: str


@dataclass(frozen=True)
class FeedbackTopicSubmission:
    title: str
    kind: str
    author: FeedbackSubmission


def default_community_database_path() -> str:
    configured = os.environ.get("SLEEPY_COMMUNITY_DB")
    if configured:
        return configured
    return str(Path(__file__).resolve().with_name("community.sqlite3"))


def community_limit_from_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


def normalize_like_target(value: Any) -> str:
    if not isinstance(value, str):
        raise CommunityValidationError("invalid_target", "like target must be a string")
    target = value.strip()
    if target in {"page:about", "page:friends"}:
        return target
    if target.startswith("post:"):
        slug = target[5:]
        if BLOG_SLUG_RE.fullmatch(slug) and ".." not in slug and "//" not in slug:
            return f"post:{slug}"
    raise CommunityValidationError("invalid_target", "like target is not enabled")


def normalize_comment_page(value: Any) -> str:
    if not isinstance(value, str):
        raise CommunityValidationError("invalid_page", "comment page must be a string")
    page = value.strip().lower()
    if page not in COMMENT_PAGES:
        raise CommunityValidationError("invalid_page", "comments are not enabled here")
    return page


def normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        raise CommunityValidationError("invalid_email", "email must be a string")
    email = CONTROL_RE.sub("", value).strip().casefold()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise CommunityValidationError("invalid_email", "please enter a valid email")
    return email


def _single_line(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CommunityValidationError("invalid_field", f"{field} must be a string")
    normalized = re.sub(r"\s+", " ", CONTROL_RE.sub("", value)).strip()
    if required and not normalized:
        raise CommunityValidationError("missing_field", f"{field} is required")
    if len(normalized) > maximum:
        raise CommunityValidationError("field_too_long", f"{field} is too long")
    return normalized


def _comment_text(value: Any) -> str:
    if not isinstance(value, str):
        raise CommunityValidationError("invalid_field", "content must be a string")
    content = CONTROL_RE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    content = "\n".join(line.rstrip() for line in content.split("\n")).strip()
    content = re.sub(r"\n{4,}", "\n\n\n", content)
    if not content:
        raise CommunityValidationError("missing_field", "content is required")
    if len(content) > 800:
        raise CommunityValidationError("field_too_long", "content is too long")
    return content


def _website(value: Any) -> str:
    website = _single_line(value, "website", 300)
    if not website:
        return ""
    try:
        parsed = urlsplit(website)
    except ValueError as exc:
        raise CommunityValidationError("invalid_website", "website is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise CommunityValidationError(
            "invalid_website", "website must be an http or https URL"
        )
    return website


def _friend_description(value: Any) -> str:
    if not isinstance(value, str):
        raise CommunityValidationError("invalid_field", "description must be a string")
    description = CONTROL_RE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    description = "\n".join(line.rstrip() for line in description.split("\n")).strip()
    if not description:
        raise CommunityValidationError("missing_field", "description is required")
    if len(description) > 240:
        raise CommunityValidationError("field_too_long", "description is too long")
    return description


def validate_comment_payload(page: Any, payload: Any) -> CommentSubmission:
    normalized_page = normalize_comment_page(page)
    if not isinstance(payload, dict):
        raise CommunityValidationError("invalid_body", "expected a JSON object")
    parent_value = payload.get("parent_id")
    if parent_value in {None, ""}:
        parent_id = None
    elif isinstance(parent_value, bool):
        raise CommunityValidationError("invalid_parent", "parent_id must be an integer")
    else:
        try:
            parent_id = int(parent_value)
        except (TypeError, ValueError) as exc:
            raise CommunityValidationError("invalid_parent", "parent_id must be an integer") from exc
        if parent_id <= 0:
            raise CommunityValidationError("invalid_parent", "parent_id must be positive")
    return CommentSubmission(
        page=normalized_page,
        parent_id=parent_id,
        nickname=_single_line(payload.get("nickname"), "nickname", 30, required=True),
        email=normalize_email(payload.get("email")),
        website=_website(payload.get("website")),
        content=_comment_text(payload.get("content")),
    )


def validate_friend_application_payload(payload: Any) -> FriendApplicationSubmission:
    if not isinstance(payload, dict):
        raise CommunityValidationError("invalid_body", "expected a JSON object")
    website = _website(payload.get("website"))
    if not website:
        raise CommunityValidationError("missing_field", "website is required")
    avatar = _website(payload.get("avatar"))
    raw_email = payload.get("email")
    email = ""
    if raw_email not in {None, ""}:
        email = normalize_email(raw_email)
    return FriendApplicationSubmission(
        name=_single_line(payload.get("name"), "name", 40, required=True),
        website=website,
        avatar=avatar,
        description=_friend_description(payload.get("description")),
        email=email,
    )


def validate_feedback_message_payload(payload: Any) -> FeedbackSubmission:
    if not isinstance(payload, dict):
        raise CommunityValidationError("invalid_body", "expected a JSON object")
    return FeedbackSubmission(
        nickname=_single_line(payload.get("nickname"), "nickname", 30, required=True),
        email=normalize_email(payload.get("email")),
        website=_website(payload.get("website")),
        content=_comment_text(payload.get("content")),
    )


def validate_feedback_topic_payload(payload: Any) -> FeedbackTopicSubmission:
    author = validate_feedback_message_payload(payload)
    kind = _single_line(payload.get("kind"), "kind", 20, required=True).lower()
    if kind not in FEEDBACK_KINDS:
        raise CommunityValidationError("invalid_kind", "feedback kind is invalid")
    return FeedbackTopicSubmission(
        title=_single_line(payload.get("title"), "title", 70, required=True),
        kind=kind,
        author=author,
    )


class CommunityStore:
    def __init__(self, database_path: str | None = None):
        self.database_path = database_path or default_community_database_path()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS community_likes (
                    target TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (target, identity_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_community_likes_target
                    ON community_likes(target);

                CREATE TABLE IF NOT EXISTS community_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page TEXT NOT NULL CHECK (page IN ('about', 'friends')),
                    parent_id INTEGER REFERENCES community_comments(id),
                    root_id INTEGER REFERENCES community_comments(id),
                    actor_hash TEXT NOT NULL,
                    owner_hash TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL,
                    email TEXT NOT NULL,
                    website TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('published', 'pending', 'rejected', 'deleted')),
                    moderation_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_date TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_community_comments_public
                    ON community_comments(page, status, id);

                CREATE INDEX IF NOT EXISTS idx_community_comments_actor
                    ON community_comments(actor_hash, id);

                CREATE TABLE IF NOT EXISTS community_comment_rate_limits (
                    created_date TEXT NOT NULL,
                    identity_key TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (created_date, identity_key)
                );

                CREATE TABLE IF NOT EXISTS friend_link_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    website TEXT NOT NULL,
                    avatar TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
                    moderation_note TEXT NOT NULL DEFAULT '',
                    submitter_hash TEXT NOT NULL DEFAULT '',
                    tracking_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_friend_applications_status
                    ON friend_link_applications(status, id);

                CREATE TABLE IF NOT EXISTS community_feedback_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL
                        CHECK (kind IN ('bug', 'suggestion', 'content', 'other')),
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'in_progress', 'resolved', 'merged')),
                    actor_hash TEXT NOT NULL,
                    owner_hash TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL,
                    email TEXT NOT NULL,
                    website TEXT NOT NULL DEFAULT '',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    resolution_note TEXT NOT NULL DEFAULT '',
                    merged_into_id INTEGER REFERENCES community_feedback_topics(id),
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_topics_status
                    ON community_feedback_topics(status, updated_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS community_feedback_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_id INTEGER NOT NULL REFERENCES community_feedback_topics(id),
                    actor_hash TEXT NOT NULL,
                    owner_hash TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL,
                    email TEXT NOT NULL,
                    website TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('published', 'pending', 'rejected', 'deleted')),
                    moderation_reason TEXT NOT NULL DEFAULT '',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_messages_topic
                    ON community_feedback_messages(topic_id, status, id);

                CREATE TABLE IF NOT EXISTS community_feedback_room_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_hash TEXT NOT NULL,
                    owner_hash TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL,
                    email TEXT NOT NULL,
                    website TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('published', 'pending', 'rejected', 'deleted')),
                    moderation_reason TEXT NOT NULL DEFAULT '',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_room_messages_public
                    ON community_feedback_room_messages(status, created_at, id);

                CREATE TABLE IF NOT EXISTS community_feedback_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_id INTEGER NOT NULL REFERENCES community_feedback_topics(id),
                    page TEXT NOT NULL CHECK (page IN ('about', 'friends')),
                    root_comment_id INTEGER NOT NULL REFERENCES community_comments(id),
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(topic_id, page, root_comment_id)
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_sources_topic
                    ON community_feedback_sources(topic_id, id);

                CREATE TABLE IF NOT EXISTS community_feedback_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_id INTEGER NOT NULL REFERENCES community_feedback_topics(id),
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_events_topic
                    ON community_feedback_events(topic_id, id);
                """
            )
            comment_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(community_comments)").fetchall()
            }
            if "is_admin" not in comment_columns:
                try:
                    connection.execute(
                        "ALTER TABLE community_comments ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            if "owner_hash" not in comment_columns:
                try:
                    connection.execute(
                        "ALTER TABLE community_comments "
                        "ADD COLUMN owner_hash TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            if "moved_to_feedback_id" not in comment_columns:
                try:
                    connection.execute(
                        "ALTER TABLE community_comments "
                        "ADD COLUMN moved_to_feedback_id INTEGER"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_community_comments_owner "
                "ON community_comments(owner_hash, id) WHERE owner_hash != ''"
            )
            friend_application_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(friend_link_applications)"
                ).fetchall()
            }
            if "tracking_hash" not in friend_application_columns:
                try:
                    connection.execute(
                        "ALTER TABLE friend_link_applications "
                        "ADD COLUMN tracking_hash TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_friend_applications_tracking "
                "ON friend_link_applications(tracking_hash) WHERE tracking_hash != ''"
            )
            feedback_topic_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(community_feedback_topics)"
                ).fetchall()
            }
            if "deleted_at" not in feedback_topic_columns:
                try:
                    connection.execute(
                        "ALTER TABLE community_feedback_topics ADD COLUMN deleted_at TEXT"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_topics_visible "
                "ON community_feedback_topics(status, updated_at DESC, id DESC) "
                "WHERE deleted_at IS NULL"
            )

    def toggle_like(self, target: str, identity_hash: str) -> tuple[int, bool]:
        target = normalize_like_target(target)
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM community_likes WHERE target = ? AND identity_hash = ?",
                (target, identity_hash),
            ).fetchone()
            if existing:
                connection.execute(
                    "DELETE FROM community_likes WHERE target = ? AND identity_hash = ?",
                    (target, identity_hash),
                )
                liked = False
            else:
                connection.execute(
                    "INSERT INTO community_likes (target, identity_hash, created_at) VALUES (?, ?, ?)",
                    (target, identity_hash, now),
                )
                liked = True
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM community_likes WHERE target = ?",
                (target,),
            ).fetchone()
        return int(row["total"]), liked

    def get_likes(
        self, targets: Iterable[str], identity_hash: str
    ) -> dict[str, dict[str, int | bool]]:
        normalized = list(dict.fromkeys(normalize_like_target(item) for item in targets))
        if not normalized:
            return {}
        self.initialize()
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            totals = connection.execute(
                f"SELECT target, COUNT(*) AS total FROM community_likes "
                f"WHERE target IN ({placeholders}) GROUP BY target",
                normalized,
            ).fetchall()
            liked_rows = connection.execute(
                f"SELECT target FROM community_likes WHERE identity_hash = ? "
                f"AND target IN ({placeholders})",
                [identity_hash, *normalized],
            ).fetchall()
        count_by_target = {str(row["target"]): int(row["total"]) for row in totals}
        liked_targets = {str(row["target"]) for row in liked_rows}
        return {
            target: {
                "count": count_by_target.get(target, 0),
                "liked": target in liked_targets,
            }
            for target in normalized
        }

    def get_parent_context(self, page: str, parent_id: int | None) -> dict[str, Any] | None:
        if parent_id is None:
            return None
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, page, root_id, nickname
                FROM community_comments
                WHERE id = ? AND page = ? AND status = 'published'
                """,
                (parent_id, page),
            ).fetchone()
        if row is None:
            raise CommunityValidationError("invalid_parent", "reply target was not found")
        return {
            "id": int(row["id"]),
            "root_id": int(row["root_id"] or row["id"]),
            "nickname": str(row["nickname"]),
        }

    def actor_history(self, actor_hash: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT page, content, status, created_at
                FROM community_comments
                WHERE actor_hash = ? AND status != 'deleted'
                ORDER BY id ASC
                """,
                (actor_hash,),
            ).fetchall()
        return [
            {
                "page": str(row["page"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def reserve_comment_quota(
        self,
        identity_keys: Iterable[str],
        *,
        daily_limit: int,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        created_date = current.date().isoformat()
        updated_at = current.astimezone(timezone.utc).isoformat()
        keys = sorted(set(identity_keys))
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for key in keys:
                    row = connection.execute(
                        """
                        SELECT used FROM community_comment_rate_limits
                        WHERE created_date = ? AND identity_key = ?
                        """,
                        (created_date, key),
                    ).fetchone()
                    if row and int(row["used"]) >= daily_limit:
                        raise CommunityRateLimitExceeded("daily_limit")
                connection.executemany(
                    """
                    INSERT INTO community_comment_rate_limits
                        (created_date, identity_key, used, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(created_date, identity_key) DO UPDATE SET
                        used = community_comment_rate_limits.used + 1,
                        updated_at = excluded.updated_at
                    """,
                    [(created_date, key, updated_at) for key in keys],
                )
        except CommunityRateLimitExceeded:
            raise

    def create_comment(
        self,
        submission: CommentSubmission,
        actor_hash: str,
        *,
        status: str,
        moderation_reason: str = "",
        parent_context: dict[str, Any] | None = None,
        is_admin: bool = False,
        owner_hash: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in COMMENT_STATUSES:
            raise ValueError("invalid comment status")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        created_at = current.astimezone(timezone.utc).isoformat()
        self.initialize()
        parent_id = parent_context["id"] if parent_context else None
        root_id = parent_context["root_id"] if parent_context else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO community_comments
                    (page, parent_id, root_id, actor_hash, owner_hash, nickname, email, website,
                     content, status, moderation_reason, is_admin, created_at, created_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission.page,
                    parent_id,
                    root_id,
                    actor_hash,
                    owner_hash,
                    submission.nickname,
                    submission.email,
                    submission.website,
                    submission.content,
                    status,
                    moderation_reason[:300],
                    1 if is_admin else 0,
                    created_at,
                    current.date().isoformat(),
                ),
            )
            comment_id = int(cursor.lastrowid)
            if root_id is None:
                connection.execute(
                    "UPDATE community_comments SET root_id = ? WHERE id = ?",
                    (comment_id, comment_id),
                )
        return {
            "id": comment_id,
            "page": submission.page,
            "parent_id": parent_id,
            "root_id": root_id or comment_id,
            "nickname": submission.nickname,
            "website": submission.website,
            "content": submission.content,
            "status": status,
            "is_admin": bool(is_admin),
            "owned": bool(owner_hash),
            "author_key": public_author_key(actor_hash),
            "created_at": created_at,
            "reply_to_name": parent_context["nickname"] if parent_context else "",
        }

    def list_public_comments(
        self,
        page: str,
        *,
        limit: int = 300,
        include_nonpublished: bool = False,
        viewer_owner_hash: str = "",
    ) -> list[dict[str, Any]]:
        page = normalize_comment_page(page)
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.page, c.parent_id, c.root_id, c.actor_hash, c.owner_hash,
                       c.nickname, c.email, c.website,
                       c.content, c.status, c.is_admin, c.created_at,
                       c.moderation_reason, parent.nickname AS reply_to_name
                FROM community_comments AS c
                LEFT JOIN community_comments AS parent ON parent.id = c.parent_id
                WHERE c.page = ? AND c.status != 'deleted'
                  AND (? OR c.status = 'published')
                ORDER BY c.id ASC
                LIMIT ?
                """,
                (page, 1 if include_nonpublished else 0, max(1, min(500, int(limit)))),
            ).fetchall()
        comments = []
        for row in rows:
            comment = {
                "id": int(row["id"]),
                "page": str(row["page"]),
                "parent_id": int(row["parent_id"]) if row["parent_id"] else None,
                "root_id": int(row["root_id"]),
                "nickname": str(row["nickname"]),
                "website": str(row["website"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "is_admin": bool(row["is_admin"]),
                "owned": bool(
                    viewer_owner_hash
                    and str(row["owner_hash"] or "") == viewer_owner_hash
                ),
                "author_key": public_author_key(str(row["actor_hash"])),
                "created_at": str(row["created_at"]),
                "reply_to_name": str(row["reply_to_name"] or ""),
            }
            if include_nonpublished:
                comment["email"] = str(row["email"] or "")
                comment["moderation_reason"] = str(row["moderation_reason"] or "")
            comments.append(comment)
        return comments

    def update_comment_status(
        self,
        comment_id: int,
        status: str,
        moderation_reason: str = "",
    ) -> bool:
        if status not in COMMENT_MODERATION_STATUSES:
            raise CommunityValidationError(
                "invalid_status", "comment status is invalid"
            )
        reason = _single_line(moderation_reason, "moderation_reason", 300)
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE community_comments
                SET status = ?, moderation_reason = ?
                WHERE id = ? AND status != 'deleted'
                """,
                (status, reason, int(comment_id)),
            )
        return cursor.rowcount > 0

    def get_comment_avatar(self, comment_id: int) -> dict[str, str] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT email, nickname FROM community_comments WHERE id = ? AND status = 'published'",
                (int(comment_id),),
            ).fetchone()
        if row is None:
            return None
        return {"email": str(row["email"]), "nickname": str(row["nickname"])}

    def delete_comment(self, comment_id: int) -> list[int]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM community_comments WHERE id = ?
                    UNION ALL
                    SELECT c.id FROM community_comments AS c
                    JOIN descendants AS d ON c.parent_id = d.id
                )
                SELECT id FROM descendants
                """,
                (int(comment_id),),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE community_comments SET status = 'deleted' WHERE id IN ({placeholders})",
                    ids,
                )
        return ids

    @staticmethod
    def _feedback_message_dict(
        row: sqlite3.Row,
        *,
        viewer_owner_hash: str = "",
        include_private: bool = False,
    ) -> dict[str, Any]:
        message = {
            "id": int(row["id"]),
            "topic_id": int(row["topic_id"]),
            "nickname": str(row["nickname"]),
            "website": str(row["website"]),
            "content": str(row["content"]),
            "status": str(row["status"]),
            "is_admin": bool(row["is_admin"]),
            "owned": bool(
                viewer_owner_hash
                and str(row["owner_hash"] or "") == viewer_owner_hash
            ),
            "author_key": public_author_key(str(row["actor_hash"])),
            "created_at": str(row["created_at"]),
        }
        if include_private:
            message["email"] = str(row["email"] or "")
            message["moderation_reason"] = str(row["moderation_reason"] or "")
        return message

    @staticmethod
    def _feedback_room_message_dict(
        row: sqlite3.Row,
        *,
        viewer_owner_hash: str = "",
        include_private: bool = False,
    ) -> dict[str, Any]:
        message = {
            "id": int(row["id"]),
            "nickname": str(row["nickname"]),
            "website": str(row["website"]),
            "content": str(row["content"]),
            "status": str(row["status"]),
            "is_admin": bool(row["is_admin"]),
            "owned": bool(
                viewer_owner_hash
                and str(row["owner_hash"] or "") == viewer_owner_hash
            ),
            "author_key": public_author_key(str(row["actor_hash"])),
            "created_at": str(row["created_at"]),
        }
        if include_private:
            message["email"] = str(row["email"] or "")
            message["moderation_reason"] = str(row["moderation_reason"] or "")
        return message

    def add_feedback_room_message(
        self,
        submission: FeedbackSubmission,
        actor_hash: str,
        *,
        status: str,
        moderation_reason: str = "",
        is_admin: bool = False,
        owner_hash: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in COMMENT_STATUSES:
            raise ValueError("invalid feedback room message status")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        timestamp = current.astimezone(timezone.utc).isoformat()
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO community_feedback_room_messages
                    (actor_hash, owner_hash, nickname, email, website, content,
                     status, moderation_reason, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_hash,
                    owner_hash,
                    submission.nickname,
                    submission.email,
                    submission.website,
                    submission.content,
                    status,
                    moderation_reason[:300],
                    1 if is_admin else 0,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM community_feedback_room_messages WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return self._feedback_room_message_dict(
            row,
            viewer_owner_hash=owner_hash,
            include_private=is_admin,
        )

    def list_feedback_room_messages(
        self,
        *,
        include_nonpublished: bool = False,
        viewer_owner_hash: str = "",
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM community_feedback_room_messages
                    WHERE status != 'deleted' AND (? OR status = 'published')
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
                """,
                (1 if include_nonpublished else 0, max(1, min(500, int(limit)))),
            ).fetchall()
        return [
            self._feedback_room_message_dict(
                row,
                viewer_owner_hash=viewer_owner_hash,
                include_private=include_nonpublished,
            )
            for row in rows
        ]

    def create_feedback_topic(
        self,
        submission: FeedbackTopicSubmission,
        actor_hash: str,
        *,
        status: str,
        moderation_reason: str = "",
        is_admin: bool = False,
        owner_hash: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in COMMENT_STATUSES:
            raise ValueError("invalid feedback message status")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        timestamp = current.astimezone(timezone.utc).isoformat()
        self.initialize()
        with self._connect() as connection:
            topic_cursor = connection.execute(
                """
                INSERT INTO community_feedback_topics
                    (title, kind, status, actor_hash, owner_hash, nickname, email,
                     website, is_admin, resolution_note, created_at, updated_at)
                VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    submission.title,
                    submission.kind,
                    actor_hash,
                    owner_hash,
                    submission.author.nickname,
                    submission.author.email,
                    submission.author.website,
                    1 if is_admin else 0,
                    timestamp,
                    timestamp,
                ),
            )
            topic_id = int(topic_cursor.lastrowid)
            message_cursor = connection.execute(
                """
                INSERT INTO community_feedback_messages
                    (topic_id, actor_hash, owner_hash, nickname, email, website,
                     content, status, moderation_reason, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topic_id,
                    actor_hash,
                    owner_hash,
                    submission.author.nickname,
                    submission.author.email,
                    submission.author.website,
                    submission.author.content,
                    status,
                    moderation_reason[:300],
                    1 if is_admin else 0,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO community_feedback_events
                    (topic_id, event_type, detail, created_at)
                VALUES (?, 'created', ?, ?)
                """,
                (topic_id, submission.kind, timestamp),
            )
        return {
            "id": topic_id,
            "title": submission.title,
            "kind": submission.kind,
            "status": "open",
            "created_at": timestamp,
            "updated_at": timestamp,
            "message_id": int(message_cursor.lastrowid),
            "message_status": status,
        }

    def add_feedback_message(
        self,
        topic_id: int,
        submission: FeedbackSubmission,
        actor_hash: str,
        *,
        status: str,
        moderation_reason: str = "",
        is_admin: bool = False,
        owner_hash: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in COMMENT_STATUSES:
            raise ValueError("invalid feedback message status")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        timestamp = current.astimezone(timezone.utc).isoformat()
        self.initialize()
        with self._connect() as connection:
            topic = connection.execute(
                "SELECT id, status, merged_into_id FROM community_feedback_topics "
                "WHERE id = ? AND deleted_at IS NULL",
                (int(topic_id),),
            ).fetchone()
            if topic is None:
                raise CommunityValidationError("not_found", "feedback topic was not found")
            if str(topic["status"]) == "merged":
                raise CommunityValidationError("topic_merged", "feedback topic was merged")
            cursor = connection.execute(
                """
                INSERT INTO community_feedback_messages
                    (topic_id, actor_hash, owner_hash, nickname, email, website,
                     content, status, moderation_reason, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(topic_id),
                    actor_hash,
                    owner_hash,
                    submission.nickname,
                    submission.email,
                    submission.website,
                    submission.content,
                    status,
                    moderation_reason[:300],
                    1 if is_admin else 0,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE community_feedback_topics SET updated_at = ? WHERE id = ?",
                (timestamp, int(topic_id)),
            )
        return {
            "id": int(cursor.lastrowid),
            "topic_id": int(topic_id),
            "status": status,
            "created_at": timestamp,
        }

    def feedback_actor_history(self, actor_hash: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT page, content, status, created_at FROM (
                    SELECT 'feedback' AS page, m.content, m.status, m.created_at
                    FROM community_feedback_messages AS m
                    JOIN community_feedback_topics AS t ON t.id = m.topic_id
                    WHERE m.actor_hash = ? AND m.status != 'deleted'
                      AND t.deleted_at IS NULL
                    UNION ALL
                    SELECT 'feedback' AS page, content, status, created_at
                    FROM community_feedback_room_messages
                    WHERE actor_hash = ? AND status != 'deleted'
                )
                ORDER BY created_at ASC
                """,
                (actor_hash, actor_hash),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_feedback_topics(
        self,
        *,
        include_nonpublished: bool = False,
        viewer_owner_hash: str = "",
    ) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            topic_rows = connection.execute(
                """
                SELECT * FROM community_feedback_topics
                WHERE deleted_at IS NULL
                ORDER BY CASE status
                    WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1
                    WHEN 'resolved' THEN 2 ELSE 3 END,
                    updated_at DESC, id DESC
                """
            ).fetchall()
            message_rows = connection.execute(
                """
                SELECT * FROM community_feedback_messages
                WHERE status != 'deleted' AND (? OR status = 'published')
                ORDER BY id ASC
                """,
                (1 if include_nonpublished else 0,),
            ).fetchall()
            source_rows = connection.execute(
                "SELECT * FROM community_feedback_sources ORDER BY id ASC"
            ).fetchall()
            event_rows = connection.execute(
                "SELECT * FROM community_feedback_events ORDER BY id ASC"
            ).fetchall()
        messages: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in message_rows:
            messages[int(row["topic_id"])].append(
                self._feedback_message_dict(
                    row,
                    viewer_owner_hash=viewer_owner_hash,
                    include_private=include_nonpublished,
                )
            )
        sources: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in source_rows:
            try:
                snapshot = json.loads(str(row["snapshot_json"]))
            except (TypeError, ValueError):
                snapshot = []
            sources[int(row["topic_id"])].append(
                {
                    "id": int(row["id"]),
                    "page": str(row["page"]),
                    "root_comment_id": int(row["root_comment_id"]),
                    "comments": snapshot if isinstance(snapshot, list) else [],
                    "created_at": str(row["created_at"]),
                }
            )
        events: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in event_rows:
            events[int(row["topic_id"])].append(
                {
                    "id": int(row["id"]),
                    "type": str(row["event_type"]),
                    "detail": str(row["detail"]),
                    "created_at": str(row["created_at"]),
                }
            )
        topics = []
        for row in topic_rows:
            topic_messages = messages.get(int(row["id"]), [])
            topic_sources = sources.get(int(row["id"]), [])
            if (
                not include_nonpublished
                and str(row["status"]) != "merged"
                and not topic_messages
                and not topic_sources
            ):
                continue
            item = {
                "id": int(row["id"]),
                "title": str(row["title"]),
                "kind": str(row["kind"]),
                "status": str(row["status"]),
                "nickname": str(row["nickname"]),
                "website": str(row["website"]),
                "is_admin": bool(row["is_admin"]),
                "owned": bool(
                    viewer_owner_hash
                    and str(row["owner_hash"] or "") == viewer_owner_hash
                ),
                "author_key": public_author_key(str(row["actor_hash"])),
                "resolution_note": str(row["resolution_note"]),
                "merged_into_id": (
                    int(row["merged_into_id"]) if row["merged_into_id"] else None
                ),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "messages": topic_messages,
                "sources": topic_sources,
                "events": events.get(int(row["id"]), []),
            }
            if include_nonpublished:
                item["email"] = str(row["email"])
            topics.append(item)
        return topics

    def update_feedback_topic(
        self,
        topic_id: int,
        *,
        title: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        resolution_note: str | None = None,
    ) -> bool:
        updates: list[str] = []
        values: list[Any] = []
        if title is not None:
            updates.append("title = ?")
            values.append(_single_line(title, "title", 70, required=True))
        if kind is not None:
            normalized_kind = _single_line(kind, "kind", 20, required=True).lower()
            if normalized_kind not in FEEDBACK_KINDS:
                raise CommunityValidationError("invalid_kind", "feedback kind is invalid")
            updates.append("kind = ?")
            values.append(normalized_kind)
        if status is not None:
            normalized_status = _single_line(status, "status", 20, required=True).lower()
            if normalized_status not in FEEDBACK_STATUSES - {"merged"}:
                raise CommunityValidationError("invalid_status", "feedback status is invalid")
            updates.append("status = ?")
            values.append(normalized_status)
        if resolution_note is not None:
            updates.append("resolution_note = ?")
            values.append(_single_line(resolution_note, "resolution_note", 300))
        if not updates:
            raise CommunityValidationError("empty_update", "feedback update is empty")
        timestamp = datetime.now(timezone.utc).isoformat()
        updates.append("updated_at = ?")
        values.extend([timestamp, int(topic_id)])
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE community_feedback_topics SET {', '.join(updates)} "
                "WHERE id = ? AND status != 'merged' AND deleted_at IS NULL",
                values,
            )
            if cursor.rowcount:
                detail = json.dumps(
                    {"title": title, "kind": kind, "status": status},
                    ensure_ascii=False,
                )
                connection.execute(
                    """
                    INSERT INTO community_feedback_events
                        (topic_id, event_type, detail, created_at)
                    VALUES (?, 'updated', ?, ?)
                    """,
                    (int(topic_id), detail, timestamp),
                )
        return cursor.rowcount > 0

    def delete_feedback_topic(self, topic_id: int) -> list[int]:
        """Soft-delete a feedback card and any cards already merged into it."""
        self.initialize()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM community_feedback_topics
                WHERE deleted_at IS NULL
                  AND (id = ? OR (status = 'merged' AND merged_into_id = ?))
                ORDER BY id ASC
                """,
                (int(topic_id), int(topic_id)),
            ).fetchall()
            deleted_ids = [int(row["id"]) for row in rows]
            if not deleted_ids:
                return []
            placeholders = ",".join("?" for _ in deleted_ids)
            connection.execute(
                f"UPDATE community_feedback_topics "
                f"SET deleted_at = ?, updated_at = ? WHERE id IN ({placeholders})",
                [timestamp, timestamp, *deleted_ids],
            )
            connection.executemany(
                """
                INSERT INTO community_feedback_events
                    (topic_id, event_type, detail, created_at)
                VALUES (?, 'deleted', 'administrator', ?)
                """,
                [(deleted_id, timestamp) for deleted_id in deleted_ids],
            )
        return deleted_ids

    def attach_comment_tree_to_feedback(
        self,
        root_comment_id: int,
        *,
        topic_id: int | None = None,
        title: str = "",
        kind: str = "bug",
    ) -> int:
        if kind not in FEEDBACK_KINDS:
            raise CommunityValidationError("invalid_kind", "feedback kind is invalid")
        self.initialize()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            root = connection.execute(
                """
                SELECT * FROM community_comments
                WHERE id = ? AND status = 'published'
                """,
                (int(root_comment_id),),
            ).fetchone()
            if root is None:
                raise CommunityValidationError("not_found", "comment tree was not found")
            actual_root_id = int(root["root_id"] or root["id"])
            root = connection.execute(
                "SELECT * FROM community_comments WHERE id = ? AND status = 'published'",
                (actual_root_id,),
            ).fetchone()
            tree_rows = connection.execute(
                """
                SELECT c.id, c.parent_id, c.root_id, c.nickname, c.website, c.content,
                       c.is_admin, c.created_at, c.actor_hash
                FROM community_comments AS c
                WHERE c.root_id = ? AND c.status = 'published'
                ORDER BY c.id ASC
                """,
                (actual_root_id,),
            ).fetchall()
            snapshot = [
                {
                    "id": int(row["id"]),
                    "parent_id": int(row["parent_id"]) if row["parent_id"] else None,
                    "root_id": int(row["root_id"]),
                    "nickname": str(row["nickname"]),
                    "website": str(row["website"]),
                    "content": str(row["content"]),
                    "is_admin": bool(row["is_admin"]),
                    "author_key": public_author_key(str(row["actor_hash"])),
                    "created_at": str(row["created_at"]),
                }
                for row in tree_rows
            ]
            if topic_id is None:
                topic_title = _single_line(
                    title or str(root["content"])[:42], "title", 70, required=True
                )
                topic_cursor = connection.execute(
                    """
                    INSERT INTO community_feedback_topics
                        (title, kind, status, actor_hash, owner_hash, nickname, email,
                         website, is_admin, resolution_note, created_at, updated_at)
                    VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        topic_title,
                        kind,
                        str(root["actor_hash"]),
                        str(root["owner_hash"] or ""),
                        str(root["nickname"]),
                        str(root["email"]),
                        str(root["website"]),
                        int(root["is_admin"]),
                        timestamp,
                        timestamp,
                    ),
                )
                topic_id = int(topic_cursor.lastrowid)
            else:
                exists = connection.execute(
                    "SELECT 1 FROM community_feedback_topics "
                    "WHERE id = ? AND status != 'merged' AND deleted_at IS NULL",
                    (int(topic_id),),
                ).fetchone()
                if exists is None:
                    raise CommunityValidationError("not_found", "feedback topic was not found")
            connection.execute(
                """
                INSERT OR IGNORE INTO community_feedback_sources
                    (topic_id, page, root_comment_id, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(topic_id),
                    str(root["page"]),
                    actual_root_id,
                    json.dumps(snapshot, ensure_ascii=False),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE community_feedback_topics SET updated_at = ? WHERE id = ?",
                (timestamp, int(topic_id)),
            )
            moved_ids = [int(row["id"]) for row in tree_rows]
            if moved_ids:
                placeholders = ",".join("?" for _ in moved_ids)
                connection.execute(
                    f"""
                    UPDATE community_comments
                    SET status = 'deleted', moved_to_feedback_id = ?
                    WHERE id IN ({placeholders})
                    """,
                    [int(topic_id), *moved_ids],
                )
            connection.execute(
                """
                INSERT INTO community_feedback_events
                    (topic_id, event_type, detail, created_at)
                VALUES (?, 'source_moved', ?, ?)
                """,
                (int(topic_id), f"{root['page']}:{actual_root_id}", timestamp),
            )
        return int(topic_id)

    def get_feedback_room_avatar(self, message_id: int) -> dict[str, str] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT email, nickname FROM community_feedback_room_messages
                WHERE id = ? AND status = 'published'
                """,
                (int(message_id),),
            ).fetchone()
        if row is None:
            return None
        return {"email": str(row["email"]), "nickname": str(row["nickname"])}

    def merge_feedback_topics(
        self,
        target_topic_id: int,
        source_topic_ids: Iterable[int],
        *,
        title: str | None = None,
    ) -> list[int]:
        target_id = int(target_topic_id)
        source_ids = sorted({int(item) for item in source_topic_ids if int(item) != target_id})
        if not source_ids:
            raise CommunityValidationError("empty_merge", "select feedback topics to merge")
        self.initialize()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            target = connection.execute(
                "SELECT id FROM community_feedback_topics "
                "WHERE id = ? AND status != 'merged' AND deleted_at IS NULL",
                (target_id,),
            ).fetchone()
            if target is None:
                raise CommunityValidationError("not_found", "target feedback topic was not found")
            placeholders = ",".join("?" for _ in source_ids)
            rows = connection.execute(
                f"SELECT id FROM community_feedback_topics WHERE id IN ({placeholders}) "
                "AND status != 'merged' AND deleted_at IS NULL",
                source_ids,
            ).fetchall()
            found = sorted(int(row["id"]) for row in rows)
            if found != source_ids:
                raise CommunityValidationError("not_found", "a source feedback topic was not found")
            connection.execute(
                f"UPDATE community_feedback_messages SET topic_id = ? "
                f"WHERE topic_id IN ({placeholders})",
                [target_id, *source_ids],
            )
            for source_id in source_ids:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO community_feedback_sources
                        (topic_id, page, root_comment_id, snapshot_json, created_at)
                    SELECT ?, page, root_comment_id, snapshot_json, created_at
                    FROM community_feedback_sources WHERE topic_id = ?
                    """,
                    (target_id, source_id),
                )
            connection.execute(
                f"DELETE FROM community_feedback_sources WHERE topic_id IN ({placeholders})",
                source_ids,
            )
            connection.execute(
                f"""
                UPDATE community_feedback_topics
                SET status = 'merged', merged_into_id = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [target_id, timestamp, *source_ids],
            )
            if title is not None:
                connection.execute(
                    "UPDATE community_feedback_topics SET title = ?, updated_at = ? WHERE id = ?",
                    (_single_line(title, "title", 70, required=True), timestamp, target_id),
                )
            else:
                connection.execute(
                    "UPDATE community_feedback_topics SET updated_at = ? WHERE id = ?",
                    (timestamp, target_id),
                )
            detail = json.dumps({"merged": source_ids}, ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO community_feedback_events
                    (topic_id, event_type, detail, created_at)
                VALUES (?, 'merged', ?, ?)
                """,
                (target_id, detail, timestamp),
            )
        return source_ids

    def get_feedback_avatar(self, message_id: int) -> dict[str, str] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT email, nickname FROM community_feedback_messages
                WHERE id = ? AND status = 'published'
                """,
                (int(message_id),),
            ).fetchone()
        if row is None:
            return None
        return {"email": str(row["email"]), "nickname": str(row["nickname"])}

    def create_friend_application(
        self,
        submission: FriendApplicationSubmission,
        submitter_hash: str,
        *,
        tracking_hash: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        timestamp = current.astimezone(timezone.utc).isoformat()
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO friend_link_applications
                    (name, website, avatar, description, email, status,
                     moderation_note, submitter_hash, tracking_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?, ?, ?)
                """,
                (
                    submission.name,
                    submission.website,
                    submission.avatar,
                    submission.description,
                    submission.email,
                    submitter_hash,
                    tracking_hash,
                    timestamp,
                    timestamp,
                ),
            )
            application_id = int(cursor.lastrowid)
        return {
            "id": application_id,
            "name": submission.name,
            "website": submission.website,
            "avatar": submission.avatar,
            "description": submission.description,
            "email": submission.email,
            "status": "pending",
            "moderation_note": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def list_friend_applications(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in FRIEND_APPLICATION_STATUSES:
            raise CommunityValidationError("invalid_status", "application status is invalid")
        self.initialize()
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM friend_link_applications ORDER BY id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM friend_link_applications WHERE status = ? ORDER BY id DESC",
                    (status,),
                ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "website": str(row["website"]),
                "avatar": str(row["avatar"]),
                "description": str(row["description"]),
                "email": str(row["email"]),
                "status": str(row["status"]),
                "moderation_note": str(row["moderation_note"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def list_friend_applications_by_tracking_hashes(
        self, tracking_hashes: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Return only applications addressed by opaque visitor tracking tokens."""
        normalized = [
            value
            for value in dict.fromkeys(str(item).strip() for item in tracking_hashes)
            if value
        ]
        if not normalized:
            return []
        self.initialize()
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, name, website, avatar, description, status,
                       moderation_note, created_at, updated_at
                FROM friend_link_applications
                WHERE tracking_hash IN ({placeholders})
                ORDER BY id DESC
                """,
                normalized,
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "website": str(row["website"]),
                "avatar": str(row["avatar"]),
                "description": str(row["description"]),
                "status": str(row["status"]),
                "moderation_note": str(row["moderation_note"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def update_friend_application(
        self,
        application_id: int,
        status: str,
        moderation_note: str = "",
    ) -> bool:
        if status not in FRIEND_APPLICATION_STATUSES:
            raise CommunityValidationError("invalid_status", "application status is invalid")
        self.initialize()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE friend_link_applications
                SET status = ?, moderation_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, moderation_note[:300], timestamp, int(application_id)),
            )
        return cursor.rowcount > 0


class CommunityBurstLimiter:
    """Small in-memory burst guard; persistent daily quotas live in SQLite."""

    def __init__(
        self,
        minute_limit: int,
        *,
        now: Callable[[], float] = time.time,
    ):
        self.minute_limit = max(1, int(minute_limit))
        self._now = now
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, *identity_keys: str) -> None:
        timestamp = self._now()
        cutoff = timestamp - 60
        keys = list(dict.fromkeys(identity_keys))
        with self._lock:
            for key in keys:
                queue = self._events[key]
                while queue and queue[0] <= cutoff:
                    queue.popleft()
                if len(queue) >= self.minute_limit:
                    raise CommunityRateLimitExceeded("minute_limit")
            for key in keys:
                self._events[key].append(timestamp)
