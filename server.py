#!/usr/bin/python3
# coding: utf-8
import os
from runtime_env import configured_value, load_env_file, migrate_sensitive_data_keys

load_env_file(os.environ.get('SLEEPY_ENV_FILE') or None)

import utils as u
from datetime import datetime, timedelta, timezone
from data import data as data_init
from flask import Flask, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import threading
import time
import uuid
import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import urllib.parse
import hashlib
import ipaddress
import re
from collections import deque
from markupsafe import escape
from analytics import BlogAnalytics, AgentActivityStore
from pet_ai import create_pet_ai_blueprint
from recommendations import (
    RecommendationRateLimiter,
    RecommendationRateLimitExceeded,
    RecommendationStore,
    RecommendationValidationError,
    recommendation_limit_from_env,
    validate_recommendation_filters,
    validate_recommendation_payload,
)


d = data_init()
migrate_sensitive_data_keys(d)
blog_analytics = BlogAnalytics()
agent_store = AgentActivityStore()
recommendation_store = RecommendationStore()
recommendation_limiter = RecommendationRateLimiter(
    minute_limit=recommendation_limit_from_env(
        'SLEEPY_RECOMMENDATION_MINUTE_LIMIT', 6, 60
    ),
    daily_limit=recommendation_limit_from_env(
        'SLEEPY_RECOMMENDATION_DAILY_LIMIT', 30, 1000
    ),
)

app = Flask(__name__, static_folder=None)
app.register_blueprint(create_pet_ai_blueprint())
# 如果前端通过反向代理（nginx）转发请求，请根据代理层数调整 x_for 值
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


@app.after_request
def add_configured_cors_headers(response):
    """Allow explicitly configured origins for deployments without a same-origin proxy."""
    origin = request.headers.get('Origin', '')
    allowed_origins = {
        item.strip()
        for item in os.environ.get('SLEEPY_CORS_ORIGINS', '').split(',')
        if item.strip()
    }
    if origin and origin in allowed_origins:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Client-ID'
        response.headers['Vary'] = 'Origin'
    return response


# === 音乐文件存储目录 ===
MUSIC_DIR = os.environ.get('SLEEPY_MUSIC_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'music'))
ALLOWED_MUSIC_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'}

if not os.path.exists(MUSIC_DIR):
    os.makedirs(MUSIC_DIR, exist_ok=True)

# === 博客图片存储目录 ===
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images')
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR, exist_ok=True)

# 在线用户统计（基于IP，5分钟内有访问算在线）
online_users = {}
online_lock = threading.Lock()
ONLINE_TIMEOUT = 120  # 秒

def update_online_users(key, is_mobile=False):
    """记录 key 的最后活跃时间和是否为手机端。
    key: 字符串（例如 'cid:xxx' 或 'ip:1.2.3.4'）
    is_mobile: 布尔，True 表示来自手机客户端
    """
    now = int(time.time())
    with online_lock:
        online_users[key] = {'t': now, 'mobile': bool(is_mobile)}

def get_online_count():
    now = int(time.time())
    mobile_count = 0
    active_count = 0
    with online_lock:
        # 计算并清理过期条目
        for key in list(online_users.keys()):
            entry = online_users.get(key)
            if not entry:
                continue
            if now - entry.get('t', 0) > ONLINE_TIMEOUT:
                del online_users[key]
                continue
            # 仍然活跃
            active_count += 1
            if entry.get('mobile'):
                mobile_count += 1
    return active_count, mobile_count

# 写锁，保护对 data.json 的写入
write_lock = threading.Lock()

# GeoIP 只在内存中保存加盐 IP 哈希与粗略位置，不保存原始 IP。
# 40/min 略低于 ip-api 免费端点的 45/min，给部署检查等操作留出余量。
GEOIP_CACHE_TTL_SECONDS = 12 * 60 * 60
GEOIP_CACHE_MAX_ENTRIES = 4096
GEOIP_VISITOR_RETRY_SECONDS = 10
GEOIP_UPSTREAM_LIMIT_PER_MINUTE = 40
geoip_lock = threading.Lock()
geoip_cache = {}
geoip_last_attempt = {}
geoip_upstream_attempts = deque()
geoip_runtime_salt = os.urandom(32).hex()

BLOG_SLUG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$')

# --- 辅助函数 ---


def reterr(code, message):
    ret = {
        'success': False,
        'code': code,
        'message': message
    }
    u.error(f'{code} - {message}')
    return u.format_dict(ret)


def showip(req, msg):
    ip1 = req.remote_addr
    try:
        ip2 = req.headers['X-Forwarded-For']
        u.infon(f'- Request: {ip1} / {ip2} : {msg}')
    except:
        ip2 = None
        u.infon(f'- Request: {ip1} : {msg}')


def get_request_key(req):
    """返回用于在线统计的请求标识：
    优先使用前端发送的 X-Client-ID（能区分同一公网下不同设备），否则使用客户端IP。
    返回格式示例： 'cid:xxxx' 或 'ip:1.2.3.4'
    """
    # 支持不同大小写的 header 名称
    cid = req.headers.get('X-Client-ID') or req.headers.get('X-Client-Id')
    if cid:
        return f'cid:{cid}'
    xff = req.headers.get('X-Forwarded-For', '')
    if xff:
        return "ip:" + xff.split(',')[0].strip()
    return f'ip:{req.remote_addr}'


class GeoIpClientError(ValueError):
    pass


class GeoIpUpstreamError(RuntimeError):
    pass


class GeoIpRateLimitExceeded(RuntimeError):
    def __init__(self, message, retry_after=GEOIP_VISITOR_RETRY_SECONDS):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def normalize_public_ip(client_ip):
    try:
        address = ipaddress.ip_address(str(client_ip or '').strip())
    except ValueError as exc:
        raise GeoIpClientError('client IP is invalid') from exc
    if not address.is_global:
        raise GeoIpClientError('client IP is not public')
    return str(address)


def fetch_ip_api_location(client_ip):
    """Resolve one validated public client IP without retaining or returning it."""
    address = normalize_public_ip(client_ip)
    encoded_ip = urllib.parse.quote(address, safe=':')
    fields = 'status,message,country,countryCode,regionName,city,lat,lon'
    url = (
        f'http://ip-api.com/json/{encoded_ip}'
        f'?lang=zh-CN&fields={urllib.parse.quote(fields, safe=",")}'
    )
    upstream_request = urllib.request.Request(
        url,
        headers={'User-Agent': 'tonks-home-geo/1.0'},
    )
    try:
        with urllib.request.urlopen(upstream_request, timeout=6) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise GeoIpRateLimitExceeded(
                'IP geolocation provider rate limited', retry_after=60
            ) from exc
        raise GeoIpUpstreamError('IP geolocation provider HTTP error') from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise GeoIpUpstreamError('IP geolocation provider is unavailable') from exc

    if payload.get('status') != 'success':
        raise GeoIpUpstreamError('IP geolocation provider rejected the lookup')

    try:
        lat = float(payload.get('lat'))
        lon = float(payload.get('lon'))
    except (TypeError, ValueError) as exc:
        raise GeoIpUpstreamError('IP geolocation returned invalid coordinates') from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeoIpUpstreamError('IP geolocation returned invalid coordinates')

    return {
        'success': True,
        'city': str(payload.get('city') or '').strip(),
        'region': str(payload.get('regionName') or '').strip(),
        'country': str(payload.get('countryCode') or payload.get('country') or '').strip(),
        'lat': lat,
        'lon': lon,
    }


def _geoip_cache_key(client_ip):
    salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
        configured_value(d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
    ) or geoip_runtime_salt
    return hashlib.sha256(
        f'{salt}|geoip-cache|{client_ip}'.encode('utf-8')
    ).hexdigest()


def _prune_geoip_state(now):
    for key, (expires_at, _) in list(geoip_cache.items()):
        if expires_at <= now:
            geoip_cache.pop(key, None)
    for key, attempted_at in list(geoip_last_attempt.items()):
        if now - attempted_at >= 60:
            geoip_last_attempt.pop(key, None)
    while geoip_upstream_attempts and now - geoip_upstream_attempts[0] >= 60:
        geoip_upstream_attempts.popleft()


def resolve_geoip_location(client_ip):
    """Resolve and cache coarse location using only a salted in-memory IP hash."""
    normalized_ip = normalize_public_ip(client_ip)
    cache_key = _geoip_cache_key(normalized_ip)
    now = time.monotonic()

    with geoip_lock:
        _prune_geoip_state(now)
        cached = geoip_cache.get(cache_key)
        if cached:
            return dict(cached[1])
        last_attempt = geoip_last_attempt.get(cache_key)
        if last_attempt is not None and now - last_attempt < GEOIP_VISITOR_RETRY_SECONDS:
            retry_after = GEOIP_VISITOR_RETRY_SECONDS - (now - last_attempt)
            raise GeoIpRateLimitExceeded(
                'visitor location lookup retried too quickly', retry_after=retry_after
            )
        if len(geoip_upstream_attempts) >= GEOIP_UPSTREAM_LIMIT_PER_MINUTE:
            retry_after = 60 - (now - geoip_upstream_attempts[0])
            raise GeoIpRateLimitExceeded(
                'location provider request budget exhausted', retry_after=retry_after
            )
        geoip_last_attempt[cache_key] = now
        geoip_upstream_attempts.append(now)

    result = fetch_ip_api_location(normalized_ip)

    with geoip_lock:
        if len(geoip_cache) >= GEOIP_CACHE_MAX_ENTRIES:
            oldest_key = min(geoip_cache, key=lambda key: geoip_cache[key][0])
            geoip_cache.pop(oldest_key, None)
        geoip_cache[cache_key] = (now + GEOIP_CACHE_TTL_SECONDS, dict(result))
    return result


def get_geoip_client_address(req):
    """Trust ProxyFix's forwarded address only when the direct peer is local Apache."""
    original = req.environ.get('werkzeug.proxy_fix.orig') or {}
    peer_value = original.get('REMOTE_ADDR') or req.remote_addr
    try:
        peer = ipaddress.ip_address(str(peer_value or '').strip())
    except ValueError as exc:
        raise GeoIpClientError('direct peer IP is invalid') from exc
    if peer.is_loopback:
        return req.remote_addr
    return str(peer)


def normalize_blog_slug(value):
    """Validate and normalize the public Astro article slug."""
    slug = str(value or '').strip().strip('/')
    if (
        not slug
        or '..' in slug
        or '//' in slug
        or not BLOG_SLUG_RE.fullmatch(slug)
    ):
        return None
    return slug


def get_blog_visitor_hash(req):
    """Return an anonymous stable identifier without storing a raw IP."""
    client_id = req.headers.get('X-Client-ID') or req.headers.get('X-Client-Id')
    if client_id:
        identity = f'cid:{str(client_id)[:200]}'
    else:
        forwarded = req.headers.get('X-Forwarded-For', '')
        ip = forwarded.split(',')[0].strip() if forwarded else (req.remote_addr or '')
        user_agent = req.headers.get('User-Agent', '')[:300]
        identity = f'ip:{ip}|ua:{user_agent}'
    salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
        configured_value(d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
    )
    return hashlib.sha256(f'{salt}|{identity}'.encode('utf-8')).hexdigest()


# === 管理员认证 ===

def verify_admin_secret():
    """验证管理员密钥（从 URL query param ?secret=xxx 传入）"""
    secret = request.args.get("secret", "")
    admin_secret = configured_value(d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
    return secret == admin_secret and secret != ""


def require_admin():
    """检查管理员密钥，验证失败返回错误响应；成功返回 None"""
    if not verify_admin_secret():
        return reterr(code='not authorized', message='invalid admin secret')
    return None


# === 桌宠推荐 ===

def get_recommendation_rate_limit_keys(req):
    """Return separate anonymous IP and client hashes without storing raw identifiers."""
    client_id = str(req.headers.get('X-Client-ID') or 'missing')[:80]
    remote = req.remote_addr or 'unknown'
    salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
        configured_value(d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
    )
    ip_key = hashlib.sha256(f'{salt}|recommendation-ip|{remote}'.encode('utf-8')).hexdigest()
    client_key = hashlib.sha256(
        f'{salt}|recommendation-client|{client_id}'.encode('utf-8')
    ).hexdigest()
    return ip_key, client_key


@app.route('/pet/recommendations', methods=['GET', 'POST'])
def pet_recommendations():
    """Submit or list song/book/game/anime recommendations for the site author."""
    if request.method == 'POST':
        if request.content_length is not None and request.content_length > 1024:
            return reterr(code='body too large', message='request body exceeds 1024 bytes')
        try:
            payload = request.get_json(force=False, silent=False)
            category, content, user_name, city = validate_recommendation_payload(payload)
            recommendation_limiter.check(*get_recommendation_rate_limit_keys(request))
        except RecommendationValidationError as exc:
            return reterr(code=exc.code, message=exc.message)
        except RecommendationRateLimitExceeded:
            return reterr(code='rate limited', message='recommendation limit exceeded')
        except Exception:
            return reterr(code='invalid JSON', message='expected a JSON object')

        try:
            recommendation = recommendation_store.create(category, content, user_name, city)
        except Exception:
            return reterr(code='server error', message='failed to save recommendation')
        return u.format_dict({
            'success': True,
            'code': 'OK',
            'recommendation': recommendation,
        })

    auth_err = require_admin()
    if auth_err:
        return auth_err
    try:
        category, created_date = validate_recommendation_filters(
            request.args.get('category'),
            request.args.get('date'),
        )
    except RecommendationValidationError as exc:
        return reterr(code=exc.code, message=exc.message)
    try:
        recommendations = recommendation_store.list(
            category=category,
            created_date=created_date,
        )
    except Exception:
        return reterr(code='server error', message='failed to read recommendations')
    return u.format_dict({
        'success': True,
        'recommendations': recommendations,
        'count': len(recommendations),
        'filters': {'category': category, 'date': created_date},
    })


@app.route('/pet/recommendations/<int:recommendation_id>', methods=['DELETE'])
def delete_pet_recommendation(recommendation_id):
    """Delete one recommendation using the existing administrator secret."""
    auth_err = require_admin()
    if auth_err:
        return auth_err
    try:
        deleted = recommendation_store.delete(recommendation_id)
    except Exception:
        return reterr(code='server error', message='failed to delete recommendation')
    if not deleted:
        return reterr(code='not found', message='recommendation not found')
    return u.format_dict({
        'success': True,
        'code': 'OK',
        'deleted': recommendation_id,
    })


# === 热力图强度计算 ===

def _percentile_thresholds(values):
    """从非零值列表计算 25/50/75 百分位阈值"""
    nonzero = sorted(v for v in values if v > 0)
    if not nonzero:
        return 0, 0, 0
    n = len(nonzero)
    return nonzero[int(n * 0.25)], nonzero[int(n * 0.50)], nonzero[int(n * 0.75)]


def _intensity_level(value, p25, p50, p75):
    """根据百分位阈值将数值映射到 0-4 强度等级"""
    if value == 0:
        return 0
    if value <= p25:
        return 1
    if value <= p50:
        return 2
    if value <= p75:
        return 3
    return 4


def calc_heatmap_intensity(activities):
    """基于 messageCount 的百分位计算 0-4 强度等级"""
    if not activities:
        return activities
    p25, p50, p75 = _percentile_thresholds(
        a.get('messageCount', 0) for a in activities
    )
    result = []
    for a in activities:
        entry = dict(a)
        entry['intensity'] = _intensity_level(entry.get('messageCount', 0), p25, p50, p75)
        result.append(entry)
    return result


# === 外部 API 辅助函数 ===

def fetch_blog_rss(count=2):
    """获取最新博客文章，先尝试 Atom 再尝试 RSS 2.0"""
    blog_base_url = os.environ.get(
        'SLEEPY_BLOG_BASE_URL', d.data.get('blog_base_url', 'https://blog.tonks.top')
    ).rstrip('/')
    atom_url = f'{blog_base_url}/atom.xml'
    rss_url = f'{blog_base_url}/rss.xml'
    posts = []

    # 先尝试 Atom
    try:
        req = urllib.request.Request(atom_url, headers={'User-Agent': 'SleepyBackend/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode('utf-8')

        root = ET.fromstring(content)
        atom_ns = 'http://www.w3.org/2005/Atom'

        def atom_find(el, tag):
            """查找元素，优先带命名空间"""
            found = el.find(f'{{{atom_ns}}}{tag}')
            if found is None:
                found = el.find(tag)
            return found

        def atom_findall(el, tag):
            found = el.findall(f'{{{atom_ns}}}{tag}')
            if not found:
                found = el.findall(tag)
            return found

        entries = atom_findall(root, 'entry')
        if entries:
            for entry in entries[:count]:
                title_el = atom_find(entry, 'title')
                title = title_el.text.strip() if title_el is not None and title_el.text else ''

                # 链接：优先 rel=alternate
                link = ''
                for link_el in atom_findall(entry, 'link'):
                    href = link_el.get('href', '')
                    rel = link_el.get('rel', 'alternate')
                    if rel == 'alternate' or not link:
                        link = href

                # 日期：published > updated
                pub_el = atom_find(entry, 'published')
                if pub_el is None:
                    pub_el = atom_find(entry, 'updated')
                date_str = pub_el.text.strip() if pub_el is not None and pub_el.text else ''

                # 摘要
                summary_el = atom_find(entry, 'summary')
                summary = summary_el.text.strip() if summary_el is not None and summary_el.text else ''

                if title and link:
                    posts.append({
                        'title': title,
                        'link': link,
                        'date': date_str,
                        'summary': summary
                    })

            if posts:
                return posts[:count]
    except Exception as e:
        u.error(f'Atom feed fetch failed: {e}')

    # Atom 失败，尝试 RSS 2.0
    try:
        req = urllib.request.Request(rss_url, headers={'User-Agent': 'SleepyBackend/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode('utf-8')

        root = ET.fromstring(content)
        channel = root.find('channel')
        if channel is not None:
            items = channel.findall('item')
            for item in items[:count]:
                title_el = item.find('title')
                link_el = item.find('link')
                date_el = item.find('pubDate')
                desc_el = item.find('description')

                title = title_el.text.strip() if title_el is not None and title_el.text else ''
                link = link_el.text.strip() if link_el is not None and link_el.text else ''
                date_str = date_el.text.strip() if date_el is not None and date_el.text else ''
                desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ''

                if title and link:
                    posts.append({
                        'title': title,
                        'link': link,
                        'date': date_str,
                        'summary': desc[:500] if desc else ''
                    })
    except Exception as e:
        u.error(f'RSS feed fetch failed: {e}')

    return posts[:count]


def _blog_image_path(blog_path):
    """将博客图片路径转为本地图片 URL 路径，并校验文件存在"""
    if not blog_path or not isinstance(blog_path, str):
        return None
    # 博客图片路径格式: /images/projects/xxx.png — 去掉 /images/ 前缀后拼到本地 IMAGES_DIR
    p = blog_path.lstrip('/')
    if p.startswith('images/'):
        p = p[len('images/'):]
    safe = os.path.normpath(p)
    if safe.startswith('..') or os.path.isabs(safe):
        return None
    if os.path.isfile(os.path.join(IMAGES_DIR, safe)):
        return f'/images/{safe.replace(os.sep, "/")}'
    return None


def _extract_links(entry, entry_type='project'):
    """提取链接，预览链接优先于源代码链接"""
    links = []
    if entry_type == 'project':
        preview = entry.get('links', '')
        if preview:
            links.append({'name': '预览', 'url': preview, 'type': 'preview'})
        source = entry.get('sourceCode', '')
        if source:
            links.append({'name': '源代码', 'url': source, 'type': 'source'})
    elif entry_type == 'timeline':
        raw_links = entry.get('links', [])
        if isinstance(raw_links, list):
            links = raw_links
        elif isinstance(raw_links, str) and raw_links:
            links = [{'name': '链接', 'url': raw_links, 'type': 'website'}]
    return links


def _extract_images(entry):
    """提取图片路径，转为本地可访问的 URL"""
    raw = entry.get('image', None)
    if raw is None:
        return []
    if isinstance(raw, str):
        paths = [raw]
    elif isinstance(raw, list):
        paths = raw
    else:
        return []
    result = []
    for p in paths:
        url = _blog_image_path(p)
        if url:
            result.append(url)
    return result


def fetch_blog_extra():
    """获取博客的额外数据：按时间倒序取最新的项目和时光机条目"""
    blog_data_url = os.environ.get(
        'SLEEPY_BLOG_DATA_URL', d.data.get('blog_data_url', 'https://blog.tonks.top/data')
    ).rstrip('/')
    result = {'featuredProject': None, 'featuredTimeline': None}

    # 获取最新项目（按 startDate 降序）
    try:
        projects_url = f'{blog_data_url}/projects.json'
        req = urllib.request.Request(projects_url, headers={'User-Agent': 'SleepyBackend/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            projects = json.loads(resp.read().decode('utf-8'))
            if isinstance(projects, list) and len(projects) > 0:
                projects.sort(key=lambda x: x.get('startDate', ''), reverse=True)
                p = projects[0]
                p['links'] = _extract_links(p, 'project')
                p['images'] = _extract_images(p)
                result['featuredProject'] = p
    except Exception as e:
        u.error(f'Blog projects fetch failed: {e}')

    # 获取最新时光机条目（按 startDate 降序）
    try:
        timeline_url = f'{blog_data_url}/timeline.json'
        req = urllib.request.Request(timeline_url, headers={'User-Agent': 'SleepyBackend/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            timeline = json.loads(resp.read().decode('utf-8'))
            if isinstance(timeline, list) and len(timeline) > 0:
                timeline.sort(key=lambda x: x.get('startDate', ''), reverse=True)
                t = timeline[0]
                t['links'] = _extract_links(t, 'timeline')
                t['images'] = _extract_images(t)
                result['featuredTimeline'] = t
    except Exception as e:
        u.error(f'Blog timeline fetch failed: {e}')

    return result


def fetch_github_contributions():
    """通过 GitHub GraphQL API 获取贡献热力图数据"""
    token = configured_value(d, 'SLEEPY_GITHUB_TOKEN', 'github_token', '')
    if not token or token == 'github_pat_xxx':
        return {'error': 'GitHub token not configured'}

    query = '''
    query {
      viewer {
        login
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
              }
            }
          }
        }
        repositories(first: 6, orderBy: {field: STARGAZERS, direction: DESC}, isFork: false) {
          nodes {
            name
            stargazerCount
            primaryLanguage {
              name
              color
            }
          }
        }
      }
    }
    '''

    try:
        data_bytes = json.dumps({'query': query}).encode('utf-8')
        req = urllib.request.Request(
            'https://api.github.com/graphql',
            data=data_bytes,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'User-Agent': 'SleepyBackend/1.0'
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        if 'errors' in result:
            return {'error': result['errors'][0].get('message', 'GitHub API error')}

        viewer = result['data']['viewer']
        calendar = viewer['contributionsCollection']['contributionCalendar']

        # 提取所有天并计算强度
        all_days = []
        counts = []
        for week in calendar['weeks']:
            for day in week['contributionDays']:
                all_days.append(day)
                counts.append(day['contributionCount'])

        l1, l2, l3 = _percentile_thresholds(counts)

        days_with_intensity = []
        for day in all_days:
            c = day['contributionCount']
            days_with_intensity.append({
                'date': day['date'],
                'count': c,
                'level': _intensity_level(c, l1, l2, l3)
            })

        # 技术栈汇总
        languages = {}
        for repo in viewer['repositories']['nodes']:
            lang = repo.get('primaryLanguage')
            if lang and lang.get('name'):
                name = lang['name']
                languages[name] = {
                    'name': name,
                    'color': lang.get('color', '#858585'),
                    'stars': languages.get(name, {}).get('stars', 0) + repo.get('stargazerCount', 0)
                }
        top_langs = sorted(languages.values(), key=lambda x: x['stars'], reverse=True)

        return {
            'username': viewer['login'],
            'totalContributions': calendar['totalContributions'],
            'days': days_with_intensity,
            'topLanguages': top_langs[:6]
        }
    except urllib.error.HTTPError as e:
        error_body = ''
        try:
            error_body = e.read().decode('utf-8')[:200]
        except:
            pass
        u.error(f'GitHub API HTTP {e.code}: {error_body}')
        return {'error': f'GitHub API HTTP {e.code}'}
    except urllib.error.URLError as e:
        u.error(f'GitHub API connection failed: {e.reason}')
        return {'error': f'Cannot connect to GitHub API: {e.reason}'}
    except Exception as e:
        u.error(f'GitHub API unexpected error: {e}')
        return {'error': str(e)}


def fetch_public_holidays(year=None, country_code='CN'):
    """获取公共节假日。Nager.Date 在国内不可用，直接用硬编码数据。"""
    if year is None:
        year = time.localtime().tm_year
    return hardcoded_holidays(year, country_code)

def hardcoded_holidays(year: int, country_code: str):
    """Nager.Date 不可用时的中国节假日兜底数据"""
    if country_code != 'CN':
        return []
    return [
        {'date': f'{year}-01-01', 'name': '元旦', 'countryCode': 'CN'},
        {'date': f'{year}-02-17', 'name': '春节', 'countryCode': 'CN'},
        {'date': f'{year}-04-05', 'name': '清明节', 'countryCode': 'CN'},
        {'date': f'{year}-05-01', 'name': '劳动节', 'countryCode': 'CN'},
        {'date': f'{year}-06-19', 'name': '端午节', 'countryCode': 'CN'},
        {'date': f'{year}-09-25', 'name': '中秋节', 'countryCode': 'CN'},
        {'date': f'{year}-10-01', 'name': '国庆节', 'countryCode': 'CN'},
    ]


# === 在线统计中间件 ===

@app.before_request
def track_online():
    # 在每次请求前统一统计在线信息，避免在各个路由重复调用
    try:
        # 不统计写入接口 /set
        if request.path == '/set':
            return

        key = get_request_key(request)
        # 读取 isMobile 自定义 header，支持 'true','1','yes' 等
        is_mobile_hdr = request.headers.get('isMobile') or request.headers.get('IsMobile')
        is_mobile = False
        if is_mobile_hdr is not None:
            try:
                v = str(is_mobile_hdr).strip().lower()
                if v in ('1', 'true', 'yes', 'on'):
                    is_mobile = True
            except:
                is_mobile = False
        update_online_users(key, is_mobile=is_mobile)
    except Exception:
        # 不影响主流程，记录异常即可
        u.error('track_online failed')


# =======================
# === 公开接口 (无需认证) ===
# =======================


@app.route('/')
def index():
    return u.format_dict({'success': True, 'service': 'personal-status-server'})


@app.route('/geoip')
def geoip():
    """Return coarse location for the current visitor without exposing/storing their IP."""
    try:
        result = resolve_geoip_location(get_geoip_client_address(request))
        response = app.response_class(
            response=json.dumps(result, ensure_ascii=False),
            status=200,
            mimetype='application/json',
        )
    except GeoIpClientError:
        response = app.response_class(
            response=json.dumps({
                'success': False,
                'code': 'geoip unavailable',
                'message': 'visitor location is unavailable',
            }, ensure_ascii=False),
            status=400,
            mimetype='application/json',
        )
    except GeoIpRateLimitExceeded as exc:
        response = app.response_class(
            response=json.dumps({
                'success': False,
                'code': 'geoip rate limited',
                'message': 'location lookup is temporarily rate limited',
            }, ensure_ascii=False),
            status=429,
            mimetype='application/json',
        )
        response.headers['Retry-After'] = str(exc.retry_after)
    except GeoIpUpstreamError:
        response = app.response_class(
            response=json.dumps({
                'success': False,
                'code': 'geoip upstream error',
                'message': 'location provider is temporarily unavailable',
            }, ensure_ascii=False),
            status=502,
            mimetype='application/json',
        )
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/query')
def query():
    d.load()
    showip(request, '/query')
    st = d.data['status']
    app_name = d.data.get('app_name', '')
    last_ts = d.data.get('timestamp', 0) or 0
    now_ts = int(time.time())

    # 超时检测：10 分钟未收到上报 → 写入"关机中"到 data.json
    TIMEOUT = 600
    if last_ts and now_ts - last_ts > TIMEOUT and app_name != '关机中':
        with write_lock:
            d.load()
            d.data['status'] = 1
            d.data['app_name'] = '关机中'
            d.data['timestamp'] = now_ts
            d.save()
        app_name = '关机中'
        st = 1

    try:
        stinfo = dict(d.data['status_list'][st])  # copy，避免修改原列表
        if st == 0 or app_name == '关机中':
            stinfo['name'] = app_name
    except:
        stinfo = {
            'status': st,
            'name': '未知'
        }
    timestamp = d.data.get('timestamp', None)
    ret = {
        'success': True,
        'status': st,
        'info': stinfo,
        'timestamp': timestamp
    }
    return u.format_dict(ret)


@app.route('/get/status_list')
def get_status_list():
    showip(request, '/get/status_list')
    stlst = d.dget('status_list')
    return u.format_dict(stlst)


@app.route('/online_count')
def online_count():
    active_count, mobile_count = get_online_count()
    return u.format_dict({"online_count": active_count, "mobile_count": mobile_count, "success": True})


@app.route('/set')
def set_normal():
    # 不记录为在线请求，保留最小日志
    status = escape(request.args.get("status"))
    app_name = escape(request.args.get("app_name"))
    timestamp = request.args.get("timestamp")
    if timestamp is not None:
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError):
            timestamp = None
    try:
        status = int(status)
    except:
        return reterr(
            code='bad request',
            message="argument 'status' must be a number"
        )
    secret = request.args.get("secret", "")
    u.info(f'status update requested: status={status}, app_name={app_name}')
    secret_real = configured_value(d, 'SLEEPY_STATUS_SECRET', 'secret', '')
    if secret == secret_real:
        # 用写锁保护写操作，一次性保存避免多次磁盘写入
        with write_lock:
            d.load()
            d.data['status'] = status
            d.data['app_name'] = app_name
            if timestamp is not None:
                d.data['timestamp'] = timestamp
            d.save()
        u.info('set success')
        ret = {
            'success': True,
            'code': 'OK',
            'set_to': status,
            'app_name':app_name
        }
        return u.format_dict(ret)
    else:
        return reterr(
            code='not authorized',
            message='invaild secret'
        )


# === Agent 活动热力图 ===

@app.route('/agent-activity', methods=['GET', 'POST'])
def agent_activity():
    """GET: 返回带强度等级的活动数据（跨机器聚合）；POST: 上传活动数据（需管理员密钥）"""
    if request.method == 'POST':
        # 管理员认证
        auth_err = require_admin()
        if auth_err:
            return auth_err

        try:
            body = request.get_json(force=True)
        except Exception:
            return reterr(code='bad request', message='invalid JSON body')

        if body is None:
            return reterr(code='bad request', message='request body is required')

        # ---- 解析 machineId + dailyActivity ----
        try:
            if isinstance(body, list):
                # 旧格式：裸数组，无 machineId
                machine_id = 'unknown'
                activities = body
            elif isinstance(body, dict):
                machine_id = str(body.get('machineId') or 'unknown')
                activities = body.get('dailyActivity', body.get('activities', []))
            else:
                return reterr(code='bad request', message='expected JSON object or array')
        except (AttributeError, TypeError):
            return reterr(code='bad request', message='expected JSON object or array')

        if not isinstance(activities, list):
            return reterr(code='bad request', message="expected JSON array of activities")

        # ---- 验证并归一化 ----
        normalized = []
        for a in activities:
            if not isinstance(a, dict):
                continue
            date = a.get('date', '')
            if not date:
                continue
            try:
                normalized.append({
                    'date': str(date),
                    'messageCount': int(a.get('messageCount', 0)),
                    'sessionCount': int(a.get('sessionCount', 0)),
                    'toolCallCount': int(a.get('toolCallCount', 0))
                })
            except (TypeError, ValueError):
                return reterr(code='bad request', message='activity counts must be integers')

        if not normalized:
            return u.format_dict({'success': True, 'code': 'OK', 'new': 0, 'total': 0, 'note': 'no valid activities to upsert'})

        # ---- 写入 SQLite（同一 machine+date 覆盖，不同 machine 共存） ----
        new_count, total = agent_store.upsert_activities(machine_id, normalized)

        u.info(f'Agent activity updated [{machine_id}]: {new_count} upserted, {total} total rows')
        return u.format_dict({
            'success': True, 'code': 'OK',
            'new': new_count, 'total': total, 'machineId': machine_id,
        })

    # GET: 返回热力图数据
    # 首次访问时尝试从 data.json 迁移旧数据（仅当 SQLite 仍为空时）
    d.load()
    legacy = d.data.get('agent_activity', [])
    if legacy:
        agent_store.migrate_from_json(legacy)

    activities = agent_store.get_aggregated_activities()
    if not activities:
        return u.format_dict({'success': True, 'activities': [], 'note': 'No activity data yet. POST to this endpoint with admin secret to upload.'})

    result = calc_heatmap_intensity(activities)
    return u.format_dict({'success': True, 'activities': result})


# === 博客文章 ===

@app.route('/blog-posts')
def blog_posts():
    """获取最新博客文章"""
    count_str = request.args.get('count', '2')
    try:
        count = int(count_str)
    except ValueError:
        count = 2
    count = max(1, min(count, 20))  # 限制 1-20

    posts = fetch_blog_rss(count=count)
    extra = fetch_blog_extra()
    return u.format_dict({
        'success': True,
        'count': len(posts),
        'posts': posts,
        'featuredProject': extra['featuredProject'],
        'featuredTimeline': extra['featuredTimeline']
    })


@app.route('/blog/views', methods=['GET'])
def blog_views():
    """Batch-read view totals for article slugs."""
    raw_slugs = request.args.getlist('slugs')
    if len(raw_slugs) == 1 and ',' in raw_slugs[0]:
        raw_slugs = raw_slugs[0].split(',')
    if len(raw_slugs) > 100:
        return reterr(code='bad request', message='at most 100 slugs are allowed')

    slugs = []
    for raw_slug in raw_slugs:
        slug = normalize_blog_slug(raw_slug)
        if slug is None:
            return reterr(code='bad request', message=f'invalid article slug: {raw_slug}')
        slugs.append(slug)

    response = u.format_dict({
        'success': True,
        'views': blog_analytics.get_views(slugs),
    })
    response.headers['Cache-Control'] = 'public, max-age=60'
    return response


@app.route('/blog/views/<path:slug>', methods=['POST'])
def record_blog_view(slug):
    """Count at most one view per anonymous visitor and 30-minute bucket."""
    normalized_slug = normalize_blog_slug(slug)
    if normalized_slug is None:
        return reterr(code='bad request', message='invalid article slug')

    views, counted = blog_analytics.record_view(
        normalized_slug,
        get_blog_visitor_hash(request),
    )
    response = u.format_dict({
        'success': True,
        'slug': normalized_slug,
        'views': views,
        'counted': counted,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


# === 博客图片服务 ===

@app.route('/images/<path:filename>')
def serve_image(filename):
    """提供博客项目/时光机图片"""
    # 安全检查：防止目录遍历
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..') or os.path.isabs(safe_path):
        return reterr(code='not found', message='image not found')
    file_path = os.path.join(IMAGES_DIR, safe_path)
    if not os.path.isfile(file_path):
        return reterr(code='not found', message=f'image not found: {filename}')
    # send_from_directory 需要正斜杠（Windows 兼容）
    return send_from_directory(IMAGES_DIR, safe_path.replace(os.sep, '/'), conditional=True)


# === 音乐管理 ===

@app.route('/music/list')
def music_list():
    """获取音乐文件列表（含 hasLyrics：是否有配对的 .lrc 歌词）"""
    d.load()
    music_files = d.data.get('music_files', [])
    result = []
    for m in music_files:
        item = dict(m)
        fname = os.path.basename(m.get('filename', ''))
        item['hasLyrics'] = bool(fname) and os.path.isfile(os.path.join(MUSIC_DIR, fname + '.lrc'))
        result.append(item)
    return u.format_dict({'success': True, 'music': result})


@app.route('/music/<path:filename>')
def music_stream(filename):
    """流媒体播放音乐文件"""
    # 安全检查：防止目录遍历
    safe_name = os.path.basename(filename)
    file_path = os.path.join(MUSIC_DIR, safe_name)

    if not os.path.isfile(file_path):
        return reterr(code='not found', message=f'music file not found: {safe_name}')

    ext = os.path.splitext(safe_name)[1].lower()
    mimetype_map = {
        '.mp3': 'audio/mpeg',
        '.wav': 'audio/wav',
        '.ogg': 'audio/ogg',
        '.flac': 'audio/flac',
        '.m4a': 'audio/mp4',
        '.aac': 'audio/aac',
    }
    mimetype = mimetype_map.get(ext, 'application/octet-stream')

    return send_from_directory(MUSIC_DIR, safe_name, mimetype=mimetype, conditional=True)


@app.route('/music/lyrics/<path:filename>')
def music_lyrics(filename):
    """获取音乐的 LRC 歌词（纯文本）；无配对 .lrc 则 not found（前端据此判定纯音乐）"""
    safe_name = os.path.basename(filename)
    lrc_name = safe_name + '.lrc'
    if not os.path.isfile(os.path.join(MUSIC_DIR, lrc_name)):
        return reterr(code='not found', message=f'lyrics not found: {safe_name}')
    return send_from_directory(MUSIC_DIR, lrc_name, mimetype='text/plain', conditional=True)


@app.route('/music/upload', methods=['POST'])
def music_upload():
    """上传音乐文件（需管理员密钥）"""
    auth_err = require_admin()
    if auth_err:
        return auth_err

    if 'file' not in request.files:
        return reterr(code='bad request', message='no file in request')

    file = request.files['file']
    if file.filename == '' or file.filename is None:
        return reterr(code='bad request', message='empty filename')

    # 检查扩展名
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_MUSIC_EXTENSIONS:
        return reterr(code='bad request', message=f'unsupported file type: {ext}. Allowed: {", ".join(ALLOWED_MUSIC_EXTENSIONS)}')

    # 安全文件名
    original_name = secure_filename(file.filename)
    # 如重名，添加 uuid 前缀
    base, ext = os.path.splitext(original_name)
    save_name = f"{base}_{uuid.uuid4().hex[:8]}{ext}"

    try:
        file.save(os.path.join(MUSIC_DIR, save_name))
    except Exception as e:
        u.error(f'Music upload failed: {e}')
        return reterr(code='server error', message=f'failed to save file: {e}')

    # 从表单获取元数据
    title = request.form.get('title', '').strip() or base
    artist = request.form.get('artist', '').strip() or 'Unknown'

    # 更新 data.json
    with write_lock:
        d.load()
        music_files = d.data.get('music_files', [])
        music_files.append({
            'filename': save_name,
            'title': title,
            'artist': artist
        })
        d.dset('music_files', music_files)

    # 可选：随音乐一并上传的 LRC 歌词，存为 <音频文件名>.lrc（无则纯音乐）
    has_lyrics = False
    lyrics_file = request.files.get('lyrics')
    if lyrics_file and lyrics_file.filename:
        try:
            lyrics_file.save(os.path.join(MUSIC_DIR, save_name + '.lrc'))
            has_lyrics = True
        except Exception as e:
            u.error(f'Lyrics save failed: {e}')

    u.info(f'Music uploaded: {save_name} title="{title}" artist="{artist}" lyrics={has_lyrics}')
    return u.format_dict({
        'success': True,
        'code': 'OK',
        'file': {
            'filename': save_name,
            'title': title,
            'artist': artist,
            'hasLyrics': has_lyrics
        }
    })


@app.route('/music/delete', methods=['POST'])
def music_delete():
    """删除音乐文件（需管理员密钥）"""
    auth_err = require_admin()
    if auth_err:
        return auth_err

    try:
        body = request.get_json(force=True)
    except Exception:
        return reterr(code='bad request', message='invalid JSON body')

    if body is None:
        return reterr(code='bad request', message='request body is required')

    try:
        if not isinstance(body, dict):
            return reterr(code='bad request', message='expected JSON object')
        filename = body.get('filename', '')
    except AttributeError:
        return reterr(code='bad request', message='expected JSON object')

    if not filename:
        return reterr(code='bad request', message='filename is required')

    safe_name = os.path.basename(filename)
    file_path = os.path.join(MUSIC_DIR, safe_name)

    # 从列表中移除
    with write_lock:
        d.load()
        music_files = d.data.get('music_files', [])
        new_list = [m for m in music_files if m.get('filename') != safe_name]

        if len(new_list) == len(music_files):
            # 没有找到对应的记录
            if os.path.isfile(file_path):
                # 文件存在但列表中没有记录，仍然删除文件
                os.remove(file_path)
                u.info(f'Music file deleted (orphan): {safe_name}')
                return u.format_dict({'success': True, 'code': 'OK', 'note': 'file was orphaned, removed from disk'})
            return reterr(code='not found', message=f'music file not found in list: {safe_name}')

        d.dset('music_files', new_list)

    # 删除物理文件
    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass
    except Exception as e:
        u.error(f'Failed to delete music file: {e}')

    # 联动删除配对的 .lrc 歌词
    try:
        os.remove(file_path + '.lrc')
    except FileNotFoundError:
        pass
    except Exception as e:
        u.error(f'Failed to delete lyrics file: {e}')

    u.info(f'Music deleted: {safe_name}')
    return u.format_dict({'success': True, 'code': 'OK', 'deleted': safe_name})


@app.route('/music/reorder', methods=['POST'])
def music_reorder():
    """调整音乐播放顺序（需管理员密钥）：按传入的 filename 顺序重排 data.json 的 music_files"""
    auth_err = require_admin()
    if auth_err:
        return auth_err

    try:
        body = request.get_json(force=True)
    except Exception:
        return reterr(code='bad request', message='invalid JSON body')

    if not isinstance(body, dict):
        return reterr(code='bad request', message='expected JSON object')

    order = body.get('order', [])
    if not isinstance(order, list):
        return reterr(code='bad request', message='order must be a list of filenames')

    with write_lock:
        d.load()
        music_files = d.data.get('music_files', [])
        by_name = {m.get('filename'): m for m in music_files}
        new_list = []
        added = set()
        for f in order:
            if f in by_name and f not in added:
                new_list.append(by_name[f])
                added.add(f)  # 去重：order 内重复的 filename 只取一次
        # 补上 order 未包含的条目（防止漏传导致丢歌）
        for m in music_files:
            if m.get('filename') not in added:
                new_list.append(m)
        d.dset('music_files', new_list)

    u.info(f'Music reordered: {len(new_list)} tracks')
    return u.format_dict({'success': True, 'code': 'OK'})


# === 日历管理 ===

@app.route('/calendar/events', methods=['GET', 'POST'])
def calendar_events():
    """GET: 获取日历事件列表；POST: 增/改/删 事件（需管理员密钥）"""
    if request.method == 'POST':
        auth_err = require_admin()
        if auth_err:
            return auth_err

        try:
            body = request.get_json(force=True)
        except Exception:
            return reterr(code='bad request', message='invalid JSON body')

        if body is None:
            return reterr(code='bad request', message='request body is required')

        try:
            if not isinstance(body, dict):
                return reterr(code='bad request', message='expected JSON object')
            action = body.get('action', 'add')
        except AttributeError:
            return reterr(code='bad request', message='expected JSON object')

        if action == 'add':
            event = body.get('event', {})
            if not event.get('date') or not event.get('name'):
                return reterr(code='bad request', message="event must have 'date' and 'name'")

            new_event = {
                'id': str(uuid.uuid4()),
                'date': str(event['date']),
                'name': str(event['name']),
                'type': str(event.get('type', 'personal'))
            }

            with write_lock:
                d.load()
                events = list(d.data.get('calendar_events', []))
                events.append(new_event)
                d.dset('calendar_events', events)

            u.info(f'Calendar event added: {new_event["date"]} - {new_event["name"]}')
            return u.format_dict({'success': True, 'code': 'OK', 'event': new_event})

        elif action == 'update':
            event = body.get('event', {})
            event_id = event.get('id', '')
            if not event_id:
                return reterr(code='bad request', message="event must have 'id' for update")

            with write_lock:
                d.load()
                events = list(d.data.get('calendar_events', []))
                found = False
                for i, e in enumerate(events):
                    if e.get('id') == event_id:
                        if 'date' in event:
                            events[i]['date'] = str(event['date'])
                        if 'name' in event:
                            events[i]['name'] = str(event['name'])
                        if 'type' in event:
                            events[i]['type'] = str(event['type'])
                        found = True
                        updated = events[i]
                        break

                if not found:
                    return reterr(code='not found', message=f'event not found: {event_id}')

                d.dset('calendar_events', events)

            u.info(f'Calendar event updated: {updated["date"]} - {updated["name"]}')
            return u.format_dict({'success': True, 'code': 'OK', 'event': updated})

        elif action == 'delete':
            event_id = body.get('event', {}).get('id', body.get('id', ''))
            if not event_id:
                return reterr(code='bad request', message="'id' is required for delete")

            with write_lock:
                d.load()
                events = list(d.data.get('calendar_events', []))
                new_events = [e for e in events if e.get('id') != event_id]

                if len(new_events) == len(events):
                    return reterr(code='not found', message=f'event not found: {event_id}')

                d.dset('calendar_events', new_events)

            u.info(f'Calendar event deleted: {event_id}')
            return u.format_dict({'success': True, 'code': 'OK', 'deleted': event_id})

        else:
            return reterr(code='bad request', message=f"unknown action: {action}. Use 'add', 'update', or 'delete'")

    # GET: 返回事件列表
    d.load()
    events = d.data.get('calendar_events', [])

    # 支持按月/日筛选
    date_filter = request.args.get('date', '')  # YYYY-MM-DD 或 YYYY-MM
    if date_filter:
        events = [e for e in events if e.get('date', '').startswith(date_filter)]

    type_filter = request.args.get('type', '')  # personal|holiday|work
    if type_filter:
        events = [e for e in events if e.get('type') == type_filter]

    # 按日期排序
    events = sorted(events, key=lambda e: e.get('date', ''))

    return u.format_dict({'success': True, 'events': events})


@app.route('/calendar/holidays')
def calendar_holidays():
    """获取公共节假日"""
    year_str = request.args.get('year', '')
    country = request.args.get('country', 'CN')

    try:
        year = int(year_str) if year_str else time.localtime().tm_year
    except ValueError:
        year = time.localtime().tm_year

    holidays = fetch_public_holidays(year=year, country_code=country)

    # 同时返回本地日历事件中 type=holiday 的条目
    d.load()
    local_holidays = [e for e in d.data.get('calendar_events', [])
                      if e.get('type') == 'holiday' and str(year) in e.get('date', '')]

    return u.format_dict({
        'success': True,
        'year': year,
        'country': country,
        'publicHolidays': holidays,
        'customHolidays': local_holidays
    })


# === GitHub 统计 ===

@app.route('/github/stats')
def github_stats():
    """获取 GitHub 贡献热力图和技术栈"""
    result = fetch_github_contributions()
    if 'error' in result:
        return u.format_dict({'success': False, 'error': result['error']})

    result['success'] = True
    return u.format_dict(result)


# ===================
# === 启动入口 ===
# ===================

# ===== 待办事项 =====

@app.route('/todos', methods=['GET', 'POST'])
def todos():
    """GET: 列出待办（自动清除超过1天的已完成项）；POST: 增/完成/删（需管理员密钥）"""
    if request.method == 'POST':
        auth_err = require_admin()
        if auth_err:
            return auth_err

        try:
            body = request.get_json(force=True)
        except Exception:
            return reterr(code='bad request', message='invalid JSON body')

        if body is None or not isinstance(body, dict):
            return reterr(code='bad request', message='expected JSON object')

        action = body.get('action', 'add')

        if action == 'add':
            text = body.get('text', '').strip()
            if not text:
                return reterr(code='bad request', message="'text' is required")

            new_todo = {
                'id': str(uuid.uuid4()),
                'text': text,
                'done': False,
                'completed_at': None,
            }

            with write_lock:
                d.load()
                todos_list = list(d.data.get('todos', []))
                todos_list.append(new_todo)
                d.dset('todos', todos_list)

            u.info(f'Todo added: {text}')
            return u.format_dict({'success': True, 'code': 'OK', 'todo': new_todo})

        elif action == 'complete':
            todo_id = body.get('id', '')
            if not todo_id:
                return reterr(code='bad request', message="'id' is required")

            with write_lock:
                d.load()
                todos_list = list(d.data.get('todos', []))
                found = None
                for t in todos_list:
                    if t.get('id') == todo_id:
                        t['done'] = True
                        t['completed_at'] = datetime.now(timezone.utc).isoformat()
                        found = t
                        break

                if found is None:
                    return reterr(code='not found', message=f'todo not found: {todo_id}')

                d.dset('todos', todos_list)

            u.info(f'Todo completed: {found["text"]}')
            return u.format_dict({'success': True, 'code': 'OK', 'todo': found})

        elif action == 'delete':
            todo_id = body.get('id', '')
            if not todo_id:
                return reterr(code='bad request', message="'id' is required")

            with write_lock:
                d.load()
                todos_list = list(d.data.get('todos', []))
                new_list = [t for t in todos_list if t.get('id') != todo_id]

                if len(new_list) == len(todos_list):
                    return reterr(code='not found', message=f'todo not found: {todo_id}')

                d.dset('todos', new_list)

            u.info(f'Todo deleted: {todo_id}')
            return u.format_dict({'success': True, 'code': 'OK', 'deleted': todo_id})

        else:
            return reterr(code='bad request', message=f"unknown action: {action}. Use 'add', 'complete', or 'delete'")

    # GET: 列出待办（自动清除超过 1 天的已完成项）
    d.load()
    todos_list = list(d.data.get('todos', []))

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    cutoff_str = cutoff.isoformat()
    cleaned = [
        t for t in todos_list
        if not t.get('done') or (t.get('completed_at') and t['completed_at'] > cutoff_str)
    ]

    if len(cleaned) != len(todos_list):
        with write_lock:
            d.data['todos'] = cleaned
            d.save()

    return u.format_dict({'success': True, 'todos': cleaned})


if __name__ == '__main__':
    from waitress import serve

    d.load()
    trusted_proxy = os.environ.get('SLEEPY_TRUSTED_PROXY', '127.0.0.1').strip()
    serve(app,
        host=os.environ.get('SLEEPY_HOST', d.data.get('host', '0.0.0.0')),
        port=int(os.environ.get('SLEEPY_PORT', d.data.get('port', 9010))),
        threads=16,
        trusted_proxy=trusted_proxy or None,
        trusted_proxy_headers={'x-forwarded-for', 'x-forwarded-proto'} if trusted_proxy else set(),
        clear_untrusted_proxy_headers=True,
    )
