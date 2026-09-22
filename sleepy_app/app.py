"""Application factory; importing this module does not start the backend."""
import os
from flask import Flask
from sleepy_app.common.environment import load_env_file
from sleepy_app.common.logging import configure_community_logging
from sleepy_app.config import Paths
from sleepy_app.runtime import Runtime
from sleepy_app.community.articles import register_article_comments
from sleepy_app.community.avatars import community_qq_number, community_avatar_svg, community_qq_avatar_url

def create_app(paths=None):
    load_env_file(os.environ.get('SLEEPY_ENV_FILE') or None)
    configure_community_logging()
    paths = paths or Paths.from_env()
    app = Flask('sleepy', static_folder=None)
    runtime = Runtime(app, paths)
    app.extensions['sleepy_runtime'] = runtime
    app.add_url_rule('/blog-posts', endpoint='blog_posts', view_func=runtime.blog_service.blog_posts, **{})
    app.add_url_rule('/blog/views', endpoint='blog_views', view_func=runtime.blog_service.blog_views, **{'methods': ['GET']})
    app.add_url_rule('/blog/views/<path:slug>', endpoint='record_blog_view', view_func=runtime.blog_service.record_blog_view, **{'methods': ['POST']})
    app.add_url_rule('/blog/site-visits', endpoint='blog_site_visits', view_func=runtime.blog_service.blog_site_visits, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/blog/community/avatar-preview', endpoint='blog_community_avatar_preview', view_func=runtime.community_comments.blog_community_avatar_preview, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/avatar/<int:comment_id>', endpoint='blog_community_avatar', view_func=runtime.community_comments.blog_community_avatar, **{})
    app.add_url_rule('/blog/community/feedback/avatar/<int:message_id>', endpoint='blog_feedback_avatar', view_func=runtime.community_comments.blog_feedback_avatar, **{})
    app.add_url_rule('/blog/community/feedback/room-avatar/<int:message_id>', endpoint='blog_feedback_room_avatar', view_func=runtime.community_comments.blog_feedback_room_avatar, **{})
    app.add_url_rule('/blog/community/likes', endpoint='blog_community_likes', view_func=runtime.community_comments.blog_community_likes, **{'methods': ['GET']})
    app.add_url_rule('/blog/community/likes/<path:target>', endpoint='toggle_blog_community_like', view_func=runtime.community_comments.toggle_blog_community_like, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/comments/<page>', endpoint='blog_community_comments', view_func=runtime.community_comments.blog_community_comments, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/blog/community/comments/<int:comment_id>/pin', endpoint='pin_blog_community_comment', view_func=runtime.community_comments.pin_blog_community_comment, **{'methods': ['PATCH']})
    app.add_url_rule('/blog/community/comments/<int:comment_id>', endpoint='manage_blog_community_comment', view_func=runtime.community_comments.manage_blog_community_comment, **{'methods': ['PATCH', 'DELETE']})
    app.add_url_rule('/blog/community/feedback', endpoint='blog_community_feedback', view_func=runtime.community_feedback.blog_community_feedback, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/blog/community/feedback/messages', endpoint='add_blog_feedback_room_message', view_func=runtime.community_feedback.add_blog_feedback_room_message, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/feedback/<int:topic_id>/messages', endpoint='add_blog_feedback_message', view_func=runtime.community_feedback.add_blog_feedback_message, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/feedback/<int:topic_id>', endpoint='manage_blog_feedback_topic', view_func=runtime.community_feedback.manage_blog_feedback_topic, **{'methods': ['PATCH', 'DELETE']})
    app.add_url_rule('/blog/community/feedback/from-comment', endpoint='convert_comment_tree_to_feedback', view_func=runtime.community_feedback.convert_comment_tree_to_feedback, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/feedback/merge', endpoint='merge_blog_feedback_topics', view_func=runtime.community_feedback.merge_blog_feedback_topics, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/friend-applications', endpoint='blog_friend_applications', view_func=runtime.community_feedback.blog_friend_applications, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/blog/community/friend-applications/status', endpoint='get_own_blog_friend_applications', view_func=runtime.community_feedback.get_own_blog_friend_applications, **{'methods': ['POST']})
    app.add_url_rule('/blog/community/friend-applications/<int:application_id>', endpoint='update_blog_friend_application', view_func=runtime.community_feedback.update_blog_friend_application, **{'methods': ['POST']})
    app.add_url_rule('/calendar/holidays', endpoint='calendar_holidays', view_func=runtime.integrations_external.calendar_holidays, **{})
    app.add_url_rule('/github/stats', endpoint='github_stats', view_func=runtime.integrations_external.github_stats, **{})
    app.add_url_rule('/weather', endpoint='visitor_weather', view_func=runtime.integrations_location.visitor_weather, **{})
    app.add_url_rule('/geoip', endpoint='geoip', view_func=runtime.integrations_location.geoip, **{})
    app.add_url_rule('/images/<path:filename>', endpoint='serve_image', view_func=runtime.media_service.serve_image, **{})
    app.add_url_rule('/music/list', endpoint='music_list', view_func=runtime.media_service.music_list, **{})
    app.add_url_rule('/music/<path:filename>', endpoint='music_stream', view_func=runtime.media_service.music_stream, **{})
    app.add_url_rule('/music/lyrics/<path:filename>', endpoint='music_lyrics', view_func=runtime.media_service.music_lyrics, **{})
    app.add_url_rule('/music/cover/<path:filename>', endpoint='music_cover', view_func=runtime.media_service.music_cover, **{})
    app.add_url_rule('/music/cover/upload', endpoint='music_cover_upload', view_func=runtime.media_service.music_cover_upload, **{'methods': ['POST']})
    app.add_url_rule('/music/upload', endpoint='music_upload', view_func=runtime.media_service.music_upload, **{'methods': ['POST']})
    app.add_url_rule('/music/delete', endpoint='music_delete', view_func=runtime.media_service.music_delete, **{'methods': ['POST']})
    app.add_url_rule('/music/reorder', endpoint='music_reorder', view_func=runtime.media_service.music_reorder, **{'methods': ['POST']})
    app.add_url_rule('/pet/recommendations', endpoint='pet_recommendations', view_func=runtime.personal_recommendations.pet_recommendations, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/pet/recommendations/<int:recommendation_id>', endpoint='delete_pet_recommendation', view_func=runtime.personal_recommendations.delete_pet_recommendation, **{'methods': ['DELETE']})
    app.add_url_rule('/calendar/events', endpoint='calendar_events', view_func=runtime.personal_service.calendar_events, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/todos', endpoint='todos', view_func=runtime.personal_service.todos, **{'methods': ['GET', 'POST']})
    app.add_url_rule('/', endpoint='index', view_func=runtime.status_service.index, **{})
    app.add_url_rule('/query', endpoint='query', view_func=runtime.status_service.query, **{})
    app.add_url_rule('/status-history', endpoint='status_history', view_func=runtime.status_service.status_history, **{})
    app.add_url_rule('/get/status_list', endpoint='get_status_list', view_func=runtime.status_service.get_status_list, **{})
    app.add_url_rule('/online_count', endpoint='online_count', view_func=runtime.status_service.online_count, **{})
    app.add_url_rule('/set', endpoint='set_normal', view_func=runtime.status_service.set_normal, **{})
    app.add_url_rule('/agent-activity', endpoint='agent_activity', view_func=runtime.status_service.agent_activity, **{'methods': ['GET', 'POST']})
    app.after_request(runtime.common_security.add_configured_cors_headers)
    app.before_request(runtime.status_service.track_online)
    register_article_comments(app, {
        'community_store': runtime.community_store,
        'community_comment_limiter': runtime.community_comment_limiter,
        'comment_moderator': runtime.comment_moderator,
        'verify_admin_secret': runtime.common_security.verify_admin_secret,
        'get_community_owner_hash': runtime.common_security.get_community_owner_hash,
        'get_community_actor_hash': runtime.common_security.get_community_actor_hash,
        'get_community_rate_limit_keys': runtime.common_security.get_community_rate_limit_keys,
        'community_qq_number': community_qq_number,
        'community_avatar_svg': community_avatar_svg,
        'community_qq_avatar_url': community_qq_avatar_url,
    })
    app.extensions["article_comments"].manifest_path = paths.article_manifest
    from sleepy_app.community.polls import register_polls
    register_polls(app, runtime, paths)
    from sleepy_app.blog.friend_feeds import register_friend_feeds
    register_friend_feeds(app, paths)
    return app
