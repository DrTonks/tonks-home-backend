# coding: utf-8
"""交互式查看并设置博客文章浏览量。

默认操作脚本同目录下的 analytics.sqlite3。测试或维护其他副本时，可通过
SLEEPY_ANALYTICS_DB 环境变量指定数据库路径。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


LOCK_TIMEOUT_SECONDS = 10


def database_path() -> Path:
    configured = os.environ.get("SLEEPY_ANALYTICS_DB")
    return Path(configured) if configured else Path(__file__).with_name("analytics.sqlite3")


def connect_existing(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在：{path}")
    connection = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={LOCK_TIMEOUT_SECONDS * 1000}")
    # WAL 模式与线上 BlogAnalytics 保持一致；已有 WAL 数据库不会因此丢失数据。
    connection.execute("PRAGMA journal_mode=WAL")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_views'"
    ).fetchone()
    if table is None:
        connection.close()
        raise RuntimeError("数据库中不存在 article_views 表，已停止操作。")
    return connection


def list_views(connection: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = connection.execute(
        "SELECT slug, views FROM article_views ORDER BY slug COLLATE NOCASE"
    ).fetchall()
    return [(str(row["slug"]), int(row["views"])) for row in rows]


def print_views(rows: list[tuple[str, int]]) -> None:
    print("\n当前文章浏览量：")
    if not rows:
        print("  （暂无记录）")
        return
    width = len(str(len(rows)))
    for index, (slug, views) in enumerate(rows, 1):
        print(f"  {index:>{width}}. {views:>10}  {slug}")


def resolve_slug(value: str, rows: list[tuple[str, int]]) -> str:
    value = value.strip().strip("/")
    if not value:
        raise ValueError("文章不能为空。")
    if value.isdecimal():
        index = int(value)
        if 1 <= index <= len(rows):
            return rows[index - 1][0]
    return value


def parse_views(value: str) -> int:
    value = value.strip()
    if not value.isdecimal():
        raise ValueError("浏览量必须是大于或等于 0 的整数。")
    result = int(value)
    if result > 9_223_372_036_854_775_807:
        raise ValueError("浏览量超出 SQLite 整数范围。")
    return result


def set_views(connection: sqlite3.Connection, slug: str, views: int) -> None:
    timestamp = int(time.time())
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO article_views (slug, views, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                views = excluded.views,
                updated_at = excluded.updated_at
            """,
            (slug, views, timestamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def interactive(connection: sqlite3.Connection) -> int:
    while True:
        rows = list_views(connection)
        print_views(rows)
        raw_slug = input("\n输入序号或文章 slug（直接回车退出）：")
        if not raw_slug.strip():
            print("未作修改。")
            return 0
        try:
            slug = resolve_slug(raw_slug, rows)
            current = dict(rows).get(slug, 0)
            views = parse_views(input(f"输入 {slug} 的最终浏览量（当前 {current}）："))
        except ValueError as error:
            print(f"输入错误：{error}")
            continue

        answer = input(
            f"确认将“{slug}”的浏览量设置为 {views}？"
            " 此操作是设置最终值，不是累加。[y/N]："
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("已取消，本次未修改。")
            continue

        set_views(connection, slug, views)
        actual = connection.execute(
            "SELECT views FROM article_views WHERE slug = ?", (slug,)
        ).fetchone()
        print(f"设置成功：{slug} = {int(actual['views'])}")
        if input("继续修改其他文章？[y/N]：").strip().lower() not in {"y", "yes"}:
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查看并设置文章浏览量")
    parser.add_argument("--list", action="store_true", help="只列出浏览量")
    parser.add_argument("--slug", help="非交互模式下要设置的文章 slug")
    parser.add_argument("--views", help="非交互模式下要设置的最终浏览量")
    parser.add_argument("--yes", action="store_true", help="确认非交互修改")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = database_path().resolve()
    print(f"数据库：{path}")
    try:
        with closing(connect_existing(path)) as connection:
            if args.list:
                print_views(list_views(connection))
                return 0
            if args.slug is not None or args.views is not None:
                if args.slug is None or args.views is None or not args.yes:
                    raise ValueError("非交互修改必须同时提供 --slug、--views 和 --yes。")
                slug = resolve_slug(args.slug, [])
                views = parse_views(args.views)
                set_views(connection, slug, views)
                print(f"设置成功：{slug} = {views}")
                return 0
            return interactive(connection)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"\n操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
