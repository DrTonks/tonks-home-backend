"""common/security business operations and HTTP handlers. State is application-scoped."""
import os
from sleepy_app.common.environment import configured_value
import sleepy_app.common.responses as u
from flask import request
import hashlib
from sleepy_app.community.store import CommunityValidationError, normalize_email

class SecurityService:
    def __init__(self, runtime):
        self.runtime = runtime

    def add_configured_cors_headers(self, response):
        """Allow explicitly configured origins for deployments without a same-origin proxy."""
        origin = request.headers.get('Origin', '')
        allowed_origins = {
            item.strip()
            for item in os.environ.get('SLEEPY_CORS_ORIGINS', '').split(',')
            if item.strip()
        }
        if origin and origin in allowed_origins:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = (
                'Content-Type, X-Client-ID, X-Visit-ID, X-Site-Source, '
                'Authorization, X-Admin-Secret, X-Community-Identity'
            )
            response.headers['Vary'] = 'Origin'
        return response


    def reterr(self, code, message):
        ret = {
            'success': False,
            'code': code,
            'message': message
        }
        u.error(f'{code} - {message}')
        return u.format_dict(ret)


    def showip(self, req, msg):
        u.infon(f'- Request: {req.remote_addr} : {msg}')


    def get_request_key(self, req):
        """返回用于在线统计的请求标识：
        优先使用前端发送的 X-Client-ID（能区分同一公网下不同设备），否则使用客户端IP。
        返回格式示例： 'cid:xxxx' 或 'ip:1.2.3.4'
        """
        # 支持不同大小写的 header 名称
        cid = req.headers.get('X-Client-ID') or req.headers.get('X-Client-Id')
        if cid:
            return f'cid:{cid}'
        return f'ip:{req.remote_addr}'


    def get_blog_visitor_hash(self, req):
        """Return an anonymous stable identifier without storing a raw IP."""
        client_id = req.headers.get('X-Client-ID') or req.headers.get('X-Client-Id')
        if client_id:
            identity = f'cid:{str(client_id)[:200]}'
        else:
            ip = req.remote_addr or ''
            user_agent = req.headers.get('User-Agent', '')[:300]
            identity = f'ip:{ip}|ua:{user_agent}'
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        return hashlib.sha256(f'{salt}|{identity}'.encode('utf-8')).hexdigest()


    def get_community_rate_limit_keys(self, req):
        """Return salted IP/client keys for public interaction rate limits."""
        client_id = str(req.headers.get('X-Client-ID') or 'missing')[:200]
        remote = req.remote_addr or 'unknown'
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        ip_key = hashlib.sha256(
            f'{salt}|community-ip|{remote}'.encode('utf-8')
        ).hexdigest()
        client_key = hashlib.sha256(
            f'{salt}|community-client|{client_id}'.encode('utf-8')
        ).hexdigest()
        return ip_key, client_key


    def get_community_actor_hash(self, email):
        """Group comment history by normalized email without exposing it to the model."""
        normalized = normalize_email(email)
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        return hashlib.sha256(
            f'{salt}|community-email|{normalized}'.encode('utf-8')
        ).hexdigest()


    def get_community_owner_hash(self, req):
        """Hash the browser's opaque cross-site token for message ownership hints."""
        token = str(req.headers.get('X-Community-Identity') or '').strip()
        if not self.runtime.COMMUNITY_IDENTITY_TOKEN_RE.fullmatch(token):
            return ''
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        return hashlib.sha256(
            f'{salt}|community-owner|{token}'.encode('utf-8')
        ).hexdigest()


    def hash_friend_application_token(self, token):
        """Hash an opaque application lookup token before it reaches SQLite."""
        normalized = str(token or '').strip()
        if not self.runtime.FRIEND_APPLICATION_TOKEN_RE.fullmatch(normalized):
            raise CommunityValidationError(
                'invalid_tracking_token', 'friend-link tracking token is invalid'
            )
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        return hashlib.sha256(
            f'{salt}|friend-application|{normalized}'.encode('utf-8')
        ).hexdigest()


    def verify_admin_secret(self):
        """验证管理员密钥（兼容 query param 与 X-Admin-Secret header）。"""
        secret = request.args.get("secret", "") or request.headers.get("X-Admin-Secret", "")
        admin_secret = configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        return secret == admin_secret and secret != ""


    def require_admin(self):
        """检查管理员密钥，验证失败返回错误响应；成功返回 None"""
        if not self.verify_admin_secret():
            return self.reterr(code='not authorized', message='invalid admin secret')
        return None


    def get_recommendation_rate_limit_keys(self, req):
        """Return separate anonymous IP and client hashes without storing raw identifiers."""
        client_id = str(req.headers.get('X-Client-ID') or 'missing')[:80]
        remote = req.remote_addr or 'unknown'
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        ip_key = hashlib.sha256(f'{salt}|recommendation-ip|{remote}'.encode('utf-8')).hexdigest()
        client_key = hashlib.sha256(
            f'{salt}|recommendation-client|{client_id}'.encode('utf-8')
        ).hexdigest()
        return ip_key, client_key

