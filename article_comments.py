"""Article discussions, separate from the three home-page rooms.

The manifest is a trusted build artifact, never supplied by a comment author.
Set SLEEPY_ARTICLE_MANIFEST to dist/community/comment-manifest.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
import threading

from flask import request, jsonify, Response, redirect
from community import (CommunityValidationError, CommunityRateLimitExceeded,
                       validate_comment_payload, community_limit_from_env)


def comment_id(value, optional=False):
    """Validate before binding a client supplied ID to SQLite's signed integer."""
    if optional and (value is None or value == ''):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CommunityValidationError('invalid_parent', '留言编号无效')
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit() or len(value) > 19:
            raise CommunityValidationError('invalid_parent', '留言编号无效')
        value = int(value)
    if not 0 < value <= 9223372036854775807:
        raise CommunityValidationError('invalid_parent', '留言编号无效')
    return value


class ArticleCommentStore:
    def __init__(self, community, manifest_path=None):
        self.community = community
        self.manifest_path = manifest_path
        self._ready = False
        self._lock = threading.Lock()
        self._manifest_stamp = None
        self._articles = {}

    def initialize(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.community.initialize()
            with self.community._connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS article_comments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        article_id TEXT NOT NULL,
                        block_id TEXT,
                        version TEXT NOT NULL,
                        quote TEXT NOT NULL DEFAULT '',
                        parent_id INTEGER REFERENCES article_comments(id),
                        root_id INTEGER REFERENCES article_comments(id),
                        nickname TEXT NOT NULL, email TEXT NOT NULL,
                        website TEXT NOT NULL, content TEXT NOT NULL,
                        actor_hash TEXT NOT NULL, owner_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('published','pending','rejected','deleted')),
                        moderation_reason TEXT NOT NULL DEFAULT '',
                        is_admin INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_article_comments_page
                        ON article_comments(article_id,status,id);
                    CREATE INDEX IF NOT EXISTS idx_article_comments_block
                        ON article_comments(article_id,block_id,status,id);
                    CREATE INDEX IF NOT EXISTS idx_article_comments_replies
                        ON article_comments(root_id,status,id);
                    CREATE INDEX IF NOT EXISTS idx_article_comments_actor
                        ON article_comments(actor_hash,id);
                """)
            self._ready = True

    def article(self, article_id):
        path = Path(self.manifest_path or os.environ.get('SLEEPY_ARTICLE_MANIFEST',
                    str(Path(__file__).with_name('article-comments-manifest.json'))))
        try:
            stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            with self._lock:
                if stamp != self._manifest_stamp:
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    if payload.get('schema') != 1:
                        raise ValueError('unsupported manifest')
                    self._articles = {a['id']: a for a in payload['articles']}
                    self._manifest_stamp = stamp
                article = self._articles.get(article_id)
        except (OSError, ValueError, KeyError, TypeError):
            raise CommunityValidationError('unavailable', '文章评论尚未配置，请稍后再试')
        if not article or article.get('draft') or article.get('encrypted'):
            raise CommunityValidationError('not_found', '这篇文章尚未开放评论')
        return article

    def context(self, article_id, payload):
        article = self.article(article_id)
        self.initialize()
        parent_id = comment_id(payload.get('parent_id'), optional=True)
        if parent_id:
            with self.community._connect() as db:
                parent = db.execute("SELECT * FROM article_comments WHERE id=? AND article_id=? AND status='published'",
                                    (parent_id, article_id)).fetchone()
                if not parent:
                    raise CommunityValidationError('invalid_parent', '回复的留言已不可用')
                root = db.execute('SELECT * FROM article_comments WHERE id=?', (parent['root_id'],)).fetchone()
            if root['status'] != 'published':
                raise CommunityValidationError('invalid_parent', '这条讨论已不可用')
            return dict(root), dict(parent)
        if payload.get('version') != article['version']:
            raise CommunityValidationError('stale_version', '文章已更新，请刷新后再留言；已输入的内容会保留')
        block = payload.get('block_id') or None
        quote = ''
        if block:
            match = next((b for b in article['blocks'] if b['id'] == block), None)
            if not match:
                raise CommunityValidationError('invalid_block', '这个段落已更新，请刷新后再试')
            quote = match['text']
        return {'article_id': article_id, 'block_id': block, 'version': article['version'], 'quote': quote}, None

    def create(self, submission, context, parent, actor, owner, status, reason, admin=False):
        self.initialize()
        with self.community._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck after moderation, which can take time.
            if parent and (not db.execute("SELECT 1 FROM article_comments WHERE id=? AND status='published'", (parent['id'],)).fetchone()
                           or not db.execute("SELECT 1 FROM article_comments WHERE id=? AND status='published'", (parent['root_id'],)).fetchone()):
                raise CommunityValidationError('invalid_parent', '回复的留言已不可用')
            cursor = db.execute('''INSERT INTO article_comments
                (article_id,block_id,version,quote,parent_id,root_id,nickname,email,website,content,
                 actor_hash,owner_hash,status,moderation_reason,is_admin,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (context['article_id'], context['block_id'], context['version'], '' if parent else context['quote'],
                 parent['id'] if parent else None, parent['root_id'] if parent else None,
                 submission.nickname, submission.email, submission.website, submission.content,
                 actor, owner, status, reason, int(admin), datetime.now(timezone.utc).isoformat()))
            ident = cursor.lastrowid
            if not parent:
                db.execute('UPDATE article_comments SET root_id=? WHERE id=?', (ident, ident))
        return ident

    def public(self, row, owner='', admin=False):
        result = {key: row[key] for key in ('id','article_id','block_id','version','quote','parent_id','root_id',
                                           'nickname','website','content','status','is_admin','created_at')}
        if row['status'] == 'deleted':
            result.update(nickname='', website='', content='这条留言已删除', quote='')
        result['owned'] = bool(owner and owner == row['owner_hash'])
        if admin:
            result['moderation_reason'] = row['moderation_reason']
        return result

    def listing(self, article_id, block=None, before=0, root=0, after=0, owner='', admin=False):
        self.article(article_id)
        self.initialize()
        visible = "status IN ('published','pending','rejected','deleted')" if admin else "status IN ('published','deleted')"
        with self.community._connect() as db:
            counts = {r['block_id'] or '': r['n'] for r in db.execute(
                "SELECT block_id,COUNT(*) n FROM article_comments WHERE article_id=? AND status='published' GROUP BY block_id", (article_id,))}
            if root:
                parent = db.execute(f'SELECT * FROM article_comments WHERE id=? AND article_id=? AND parent_id IS NULL AND {visible}', (root, article_id)).fetchone()
                if not parent:
                    raise CommunityValidationError('invalid_parent', '讨论不可用')
                rows = db.execute(f'SELECT * FROM article_comments WHERE root_id=? AND parent_id IS NOT NULL AND id>? AND {visible} ORDER BY id LIMIT 31', (root, after)).fetchall()
                comments = []
                for row in rows[:30]:
                    item = self.public(row, owner, admin)
                    item['quote'] = parent['quote'] if parent['status'] != 'deleted' else ''
                    target = db.execute('SELECT nickname,status FROM article_comments WHERE id=?', (row['parent_id'],)).fetchone()
                    item['reply_to_name'] = target['nickname'] if target and target['status'] != 'deleted' else ''
                    comments.append(item)
                return {'comments': comments, 'next_after': rows[29]['id'] if len(rows) > 30 else None}
            conditions, args = ['article_id=?', 'parent_id IS NULL', visible], [article_id]
            if block is not None:
                conditions.append('block_id=?'); args.append(block)
            if before:
                conditions.append('id<?'); args.append(before)
            rows = db.execute(f"SELECT * FROM article_comments WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT 21", args).fetchall()
            comments = []
            for row in rows[:20]:
                item = self.public(row, owner, admin)
                item['reply_count'] = db.execute(f'SELECT COUNT(*) FROM article_comments WHERE root_id=? AND parent_id IS NOT NULL AND {visible}', (row['id'],)).fetchone()[0]
                comments.append(item)
            return {'comments': comments, 'counts': counts, 'count': sum(counts.values()),
                    'next_before': rows[19]['id'] if len(rows) > 20 else None}

    def history(self, actor):
        self.initialize()
        with self.community._connect() as db:
            rows = db.execute('SELECT content,status,created_at FROM article_comments WHERE actor_hash=? ORDER BY id DESC LIMIT 20', (actor,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def manage(self, article_id, ident, status):
        self.article(article_id)
        ident = comment_id(ident)
        self.initialize()
        with self.community._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if status == 'published':
                # A child must not become public while any ancestor is hidden.
                blocked = db.execute('''WITH RECURSIVE ancestors(id,parent_id,status) AS (
                    SELECT p.id,p.parent_id,p.status FROM article_comments c
                    JOIN article_comments p ON p.id=c.parent_id
                    WHERE c.id=? AND c.article_id=?
                    UNION ALL SELECT p.id,p.parent_id,p.status FROM article_comments p
                    JOIN ancestors a ON p.id=a.parent_id
                ) SELECT 1 FROM ancestors WHERE status != 'published' LIMIT 1''', (ident, article_id)).fetchone()
                if blocked:
                    raise CommunityValidationError('invalid_parent', '请先恢复上级留言，再通过这条回复')
            if status in {'deleted', 'rejected'}:
                exists = db.execute('SELECT 1 FROM article_comments WHERE id=? AND article_id=?', (ident, article_id)).fetchone()
                if not exists:
                    return False
                db.execute('''WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM article_comments WHERE id=? AND article_id=?
                    UNION ALL SELECT c.id FROM article_comments c JOIN descendants d ON c.parent_id=d.id
                ) UPDATE article_comments SET status=? WHERE id IN (SELECT id FROM descendants)''', (ident, article_id, status))
                return True
            return db.execute('UPDATE article_comments SET status=? WHERE id=? AND article_id=?', (status, ident, article_id)).rowcount > 0


def register_article_comments(app, services):
    store = ArticleCommentStore(services['community_store'])
    app.extensions['article_comments'] = store

    def respond(**data):
        response = jsonify(success=True, **data)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/blog/community/articles/<article_id>/comments', methods=['GET', 'POST'])
    def article_comments(article_id):
        try:
            admin = services['verify_admin_secret']()
            if request.headers.get('X-Admin-Secret') and not admin:
                return jsonify(success=False, message='管理员密钥无效'), 401
            owner = services['get_community_owner_hash'](request)
            if request.method == 'GET':
                def integer(name):
                    value = request.args.get(name, '0')
                    if not value.isdigit() or len(value) > 15:
                        raise CommunityValidationError('invalid_cursor', '分页参数无效')
                    return int(value)
                return respond(**store.listing(article_id, request.args.get('block'), integer('before'),
                                               integer('root'), integer('after'), owner, admin))
            if request.content_length is not None and request.content_length > 8192:
                return jsonify(success=False, message='留言请求过大'), 413
            raw = request.stream.read(8193)
            if len(raw) > 8192:
                return jsonify(success=False, message='留言请求过大'), 413
            if not request.is_json:
                raise CommunityValidationError('invalid_body', '留言格式无效')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise CommunityValidationError('invalid_body', '留言格式无效')
            payload['parent_id'] = comment_id(payload.get('parent_id'), optional=True)
            submission = validate_comment_payload('about', payload)
            context, parent = store.context(article_id, {**payload, 'parent_id': submission.parent_id})
            actor = services['get_community_actor_hash'](submission.email)
            ip, client = services['get_community_rate_limit_keys'](request)
            services['community_comment_limiter'].check(ip, client, actor)
            store.community.reserve_comment_quota({f'email:{actor}', f'ip:{ip}', f'client:{client}'},
                daily_limit=community_limit_from_env('SLEEPY_COMMENT_DAILY_LIMIT', 20, 200))
            if admin:
                status, reason = 'published', 'admin'
            else:
                moderation = services['comment_moderator'].moderate(page=f'article:{article_id}',
                    nickname=submission.nickname, content=submission.content,
                    reply_to_name=parent['nickname'] if parent else '',
                    history=sorted(store.community.actor_history(actor) + store.history(actor),
                                   key=lambda item: item['created_at']))
                status = {'allow':'published', 'review':'pending', 'reject':'rejected'}[moderation.decision]
                reason = f'{moderation.category}: {moderation.reason}'
            ident = store.create(submission, context, parent, actor, owner, status, reason, admin)
            if status == 'rejected':
                return jsonify(success=False, message='评论未通过内容审核，请修改后再试'), 400
            return respond(id=ident, status=status, message='评论已发布' if status == 'published' else '评论已提交，等待审核')
        except CommunityValidationError as exc:
            return jsonify(success=False, code=exc.code, message=exc.message), 400
        except CommunityRateLimitExceeded:
            return jsonify(success=False, message='留言太频繁，请稍后再试'), 429
        except (ValueError, TypeError):
            return jsonify(success=False, message='留言格式无效'), 400

    @app.route('/blog/community/articles/<article_id>/comments/<int:ident>', methods=['PATCH', 'DELETE'])
    def manage_article_comment(article_id, ident):
        if not services['verify_admin_secret']():
            return jsonify(success=False, message='需要管理员身份'), 401
        try:
            ident = comment_id(ident)
            payload = request.get_json(silent=True) if request.method == 'PATCH' else {}
            if not isinstance(payload, dict):
                raise CommunityValidationError('invalid_body', '留言格式无效')
            status = 'deleted' if request.method == 'DELETE' else payload.get('status')
            if not isinstance(status, str) or status not in {'published','rejected','deleted'}:
                raise CommunityValidationError('invalid_status', '状态无效')
            if not store.manage(article_id, ident, status):
                return jsonify(success=False, message='留言不存在'), 404
            return respond()
        except CommunityValidationError as exc:
            return jsonify(success=False, message=exc.message), 400

    @app.route('/blog/community/articles/<article_id>/avatar/<int:ident>')
    def article_comment_avatar(article_id, ident):
        try:
            ident = comment_id(ident)
            store.article(article_id)
        except CommunityValidationError:
            return '', 404
        store.initialize()
        with store.community._connect() as db:
            row = db.execute("SELECT email FROM article_comments WHERE id=? AND article_id=? AND status='published'", (ident, article_id)).fetchone()
        if not row:
            return '', 404
        email = row['email']
        if request.args.get('fallback'):
            response = Response(services['community_avatar_svg'](email), mimetype='image/svg+xml')
            response.headers['Content-Security-Policy'] = "default-src 'none'"
        else:
            qq = services['community_qq_number'](email)
            response = redirect(services['community_qq_avatar_url'](qq) if qq else services['community_gravatar_url'](email))
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response
