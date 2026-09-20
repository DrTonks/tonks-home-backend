"""Construct one set of stores, locks, caches and services per application."""
import os
from sleepy_app.common.environment import migrate_sensitive_data_keys
from sleepy_app.common.data import data as data_init
import threading
import re
from collections import deque
from sleepy_app.blog.storage import BlogAnalytics, AgentActivityStore
from pet_ai import create_pet_ai_blueprint
from sleepy_app.personal.recommendation_store import RecommendationRateLimiter, RecommendationStore, recommendation_limit_from_env
from sleepy_app.community.store import CommunityBurstLimiter, CommunityStore, community_limit_from_env
from sleepy_app.community.moderation import CommentModerationService
from sleepy_app.blog.service import BlogService
from sleepy_app.common.security import SecurityService
from sleepy_app.community.comments import CommentService
from sleepy_app.community.feedback import FeedbackService
from sleepy_app.integrations.external import ExternalService
from sleepy_app.integrations.location import LocationService
from sleepy_app.media.service import MediaService
from sleepy_app.personal.recommendations import RecommendationService
from sleepy_app.personal.service import PersonalService
from sleepy_app.status.service import StatusService

class Runtime:
    def __init__(self, app, paths):
        self.d = data_init(paths.data_file)
        migrate_sensitive_data_keys(self.d)
        self.blog_analytics = BlogAnalytics(paths.analytics_db)
        self.agent_store = AgentActivityStore(paths.agent_db)
        self.recommendation_store = RecommendationStore(paths.recommendations_db)
        self.recommendation_limiter = RecommendationRateLimiter(minute_limit=recommendation_limit_from_env('SLEEPY_RECOMMENDATION_MINUTE_LIMIT', 6, 60), daily_limit=recommendation_limit_from_env('SLEEPY_RECOMMENDATION_DAILY_LIMIT', 30, 1000))
        self.community_store = CommunityStore(paths.community_db)
        self.comment_moderator = CommentModerationService()
        self.community_comment_limiter = CommunityBurstLimiter(community_limit_from_env('SLEEPY_COMMENT_MINUTE_LIMIT', 3, 60))
        self.community_like_limiter = CommunityBurstLimiter(community_limit_from_env('SLEEPY_LIKE_MINUTE_LIMIT', 30, 300))
        self.friend_application_limiter = CommunityBurstLimiter(community_limit_from_env('SLEEPY_FRIEND_APPLICATION_MINUTE_LIMIT', 2, 20))
        self.app = app
        self.app.register_blueprint(create_pet_ai_blueprint())
        self.MUSIC_DIR = str(paths.music)
        self.ALLOWED_MUSIC_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'}
        self.ALLOWED_COVER_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
        self.MAX_COVER_BYTES = 5 * 1024 * 1024
        if not os.path.exists(self.MUSIC_DIR):
            os.makedirs(self.MUSIC_DIR, exist_ok=True)
        self.IMAGES_DIR = str(paths.images)
        if not os.path.exists(self.IMAGES_DIR):
            os.makedirs(self.IMAGES_DIR, exist_ok=True)
        self.online_users = {}
        self.online_lock = threading.Lock()
        self.ONLINE_TIMEOUT = 120
        self.write_lock = threading.Lock()
        self.GITHUB_CACHE_TTL_SECONDS = 24 * 60 * 60
        self.GITHUB_CACHE_VERSION = 2
        self.GITHUB_CACHE_FILE = str(paths.github_cache)
        self.github_cache_lock = threading.Lock()
        self.GEOIP_CACHE_TTL_SECONDS = 12 * 60 * 60
        self.GEOIP_CACHE_MAX_ENTRIES = 4096
        self.GEOIP_VISITOR_RETRY_SECONDS = 10
        self.GEOIP_UPSTREAM_LIMIT_PER_MINUTE = 40
        self.geoip_lock = threading.Lock()
        self.geoip_cache = {}
        self.geoip_last_attempt = {}
        self.geoip_upstream_attempts = deque()
        self.geoip_runtime_salt = os.urandom(32).hex()
        self.WEATHER_CACHE_TTL_SECONDS = 60 * 60
        self.WEATHER_CACHE_STALE_SECONDS = 6 * 60 * 60
        self.WEATHER_CACHE_MAX_ENTRIES = 4096
        self.WEATHER_VISITOR_RETRY_SECONDS = 10
        self.WEATHER_UPSTREAM_LIMIT_PER_MINUTE = 20
        self.weather_lock = threading.Lock()
        self.weather_cache = {}
        self.weather_last_attempt = {}
        self.weather_upstream_attempts = deque()
        self.SITE_VISIT_ID_RE = re.compile('^[A-Za-z0-9_-]{16,128}$')
        self.FRIEND_APPLICATION_TOKEN_RE = re.compile('^[A-Za-z0-9_-]{32,128}$')
        self.COMMUNITY_IDENTITY_TOKEN_RE = re.compile('^[A-Za-z0-9_-]{32,128}$')
        self.blog_service = BlogService(self)
        self.common_security = SecurityService(self)
        self.community_comments = CommentService(self)
        self.community_feedback = FeedbackService(self)
        self.integrations_external = ExternalService(self)
        self.integrations_location = LocationService(self)
        self.media_service = MediaService(self)
        self.personal_recommendations = RecommendationService(self)
        self.personal_service = PersonalService(self)
        self.status_service = StatusService(self)
