"""Article polls. Definitions are imported by deployment, votes are immutable."""
import json
import re
import threading
from pathlib import Path
from flask import request, jsonify
from sleepy_app.community.store import CommunityValidationError, CommunityRateLimitExceeded, CommunityBurstLimiter


class PollStore:
    def __init__(self, community, public_path):
        self.community = community
        self.public_path = Path(public_path)
        self._lock = threading.Lock()
        self._ready = False
        self._stamp = None
        self._active = {}

    def initialize(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.community.initialize()
            with self.community._connect() as db:
                db.executescript('''
                CREATE TABLE IF NOT EXISTS article_poll_definitions (
                  id TEXT NOT NULL, version TEXT NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY(id, version));
                CREATE TABLE IF NOT EXISTS article_poll_votes (
                  poll_id TEXT NOT NULL, owner_hash TEXT NOT NULL, option_id TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(poll_id, owner_hash));
                CREATE INDEX IF NOT EXISTS idx_poll_options ON article_poll_votes(poll_id, option_id);
                ''')
            self._ready = True

    @staticmethod
    def validate(p):
        key = lambda s: isinstance(s, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', s)
        if not isinstance(p, dict) or not key(p.get('id')) or not re.fullmatch('[0-9a-f]{64}', str(p.get('version', ''))):
            raise ValueError('Invalid poll ID/version')
        options = p.get('options')
        if not isinstance(options, list) or not 2 <= len(options) <= 8:
            raise ValueError('Invalid poll options')
        for o in options:
            if not isinstance(o, dict) or not key(o.get('id')) or not isinstance(o.get('label'), str) or not 1 <= len(o['label']) <= 200:
                raise ValueError('Invalid poll option')
        ids = [o['id'] for o in options]
        if len(set(ids)) != len(ids) or (p.get('answer') is not None and p['answer'] not in ids):
            raise ValueError('Invalid poll answer')
        if not isinstance(p.get('title'), str) or not 1 <= len(p['title']) <= 200 or not isinstance(p.get('explanation'), str) or len(p['explanation']) > 1000:
            raise ValueError('Invalid poll text')

    def sync(self, payload):
        if not isinstance(payload, dict) or payload.get('schema') != 1 or not isinstance(payload.get('polls'), list) or len(payload['polls']) > 10000:
            raise ValueError('Invalid poll manifest')
        polls = payload['polls']
        for p in polls:
            self.validate(p)
        if len({p['id'] for p in polls}) != len(polls):
            raise ValueError('Duplicate poll ID')
        self.initialize()
        with self.community._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for p in polls:
                old = db.execute('SELECT body FROM article_poll_definitions WHERE id=?', (p['id'],)).fetchall()
                # Keep all versions for release rollback, but never reinterpret existing votes.
                for row in old:
                    previous = json.loads(row['body'])
                    semantics = lambda x: (x['title'], sorted((o['id'], o['label']) for o in x['options']), x.get('answer'), x.get('explanation'))
                    if semantics(previous) != semantics(p):
                        raise ValueError(f"Poll {p['id']} changed; use a new ID for changed questions/options")
                encoded = json.dumps(p, ensure_ascii=False, sort_keys=True)
                same = db.execute('SELECT body FROM article_poll_definitions WHERE id=? AND version=?', (p['id'],p['version'])).fetchone()
                if same and same['body'] != encoded:
                    raise ValueError('Poll version collision')
                db.execute('INSERT OR IGNORE INTO article_poll_definitions VALUES (?,?,?)', (p['id'],p['version'],encoded))
        return len(polls)

    def active(self, ident, version):
        try:
            stat = self.public_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            with self._lock:
                if stamp != self._stamp:
                    manifest = json.loads(self.public_path.read_text(encoding='utf-8'))
                    if manifest['schema'] != 1:
                        raise ValueError()
                    self._active = {p['id']: p['version'] for p in manifest['polls']}
                    self._stamp = stamp
                if self._active.get(ident) != version:
                    raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise CommunityValidationError('unavailable', '投票暂未开放或文章已更新，请刷新后重试')

    def state(self, ident, version, owner, option=None):
        self.active(ident, version)
        self.initialize()
        with self.community._connect() as db:
            if option is not None:
                db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM article_poll_definitions WHERE id=? AND version=?', (ident,version)).fetchone()
            if not row:
                raise CommunityValidationError('unavailable', '投票尚未同步到服务器')
            poll = json.loads(row['body'])
            if option is not None:
                if not owner or not isinstance(option, str) or option not in [o['id'] for o in poll['options']]:
                    raise CommunityValidationError('invalid_vote', '请选择有效选项，并允许保存站点身份')
                db.execute('INSERT OR IGNORE INTO article_poll_votes(poll_id,owner_hash,option_id) VALUES (?,?,?)', (ident,owner,option))
            voted = db.execute('SELECT option_id FROM article_poll_votes WHERE poll_id=? AND owner_hash=?', (ident,owner)).fetchone() if owner else None
            result = {'success':True, 'voted':bool(voted), 'version':version}
            if voted:
                counts = {r['option_id']:r['n'] for r in db.execute('SELECT option_id,COUNT(*) n FROM article_poll_votes WHERE poll_id=? GROUP BY option_id', (ident,))}
                result.update(choice=voted['option_id'], counts={o['id']:counts.get(o['id'],0) for o in poll['options']}, total=sum(counts.values()), answer=poll.get('answer'), explanation=poll['explanation'])
            return result


def register_polls(app, runtime, paths):
    import os
    store = PollStore(runtime.community_store, os.environ.get('SLEEPY_POLL_PUBLIC_MANIFEST') or Path(paths.article_manifest).with_name('polls.json'))
    app.extensions['article_polls'] = store
    limiter = CommunityBurstLimiter(30)

    @app.route('/blog/community/polls/<ident>', methods=['GET', 'POST'])
    def article_poll(ident):
        try:
            owner = runtime.common_security.get_community_owner_hash(request)
            version = request.args.get('version','')
            option = None
            if request.method == 'POST':
                if not request.is_json or (request.content_length or 0) > 2048:
                    raise CommunityValidationError('invalid_body', '投票格式无效')
                body = request.get_json(silent=True)
                if not isinstance(body, dict) or not isinstance(body.get('option'), str):
                    raise CommunityValidationError('invalid_body', '请选择一个选项')
                option = body['option']
                if not owner:
                    raise CommunityValidationError('invalid_vote', '请允许保存站点身份后再投票')
                ip, client = runtime.common_security.get_community_rate_limit_keys(request)
                limiter.check(ip, client, owner)
            response = jsonify(store.state(ident, version, owner, option))
        except CommunityRateLimitExceeded:
            response = jsonify(success=False,message='操作过于频繁，请稍后重试')
            response.status_code = 429
        except CommunityValidationError as exc:
            response = jsonify(success=False,message=str(exc))
            response.status_code = 400
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Vary'] = 'X-Community-Identity'
        return response
