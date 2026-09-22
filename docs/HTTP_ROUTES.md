# HTTP 路由地图

当前静态路由导航；运行时的自动 HEAD/OPTIONS 不在此重复列出。迁移路由时同步更新，并由契约测试确认 URL 与方法没有改变。

| 方法 | URL | 当前实现 |
|---|---|---|
| GET | `/blog/friend-feeds` | `sleepy_app/blog/friend_feeds.py` · 每日好友 RSS/Atom 缓存 |
| GET / POST | `/pet/recommendations` | `sleepy_app/personal/recommendations.py` · `pet_recommendations` |
| DELETE | `/pet/recommendations/<int:recommendation_id>` | `sleepy_app/personal/recommendations.py` · `delete_pet_recommendation` |
| GET | `/` | `sleepy_app/status/service.py` · `index` |
| GET | `/weather` | `sleepy_app/integrations/location.py` · `visitor_weather` |
| GET | `/geoip` | `sleepy_app/integrations/location.py` · `geoip` |
| GET | `/query` | `sleepy_app/status/service.py` · `query` |
| GET | `/status-history` | `sleepy_app/status/service.py` · `status_history` |
| GET | `/get/status_list` | `sleepy_app/status/service.py` · `get_status_list` |
| GET | `/online_count` | `sleepy_app/status/service.py` · `online_count` |
| GET | `/set` | `sleepy_app/status/service.py` · `set_normal` |
| GET / POST | `/agent-activity` | `sleepy_app/status/service.py` · `agent_activity` |
| GET | `/blog-posts` | `sleepy_app/blog/service.py` · `blog_posts` |
| GET | `/blog/views` | `sleepy_app/blog/service.py` · `blog_views` |
| POST | `/blog/views/<path:slug>` | `sleepy_app/blog/service.py` · `record_blog_view` |
| GET / POST | `/blog/site-visits` | `sleepy_app/blog/service.py` · `blog_site_visits` |
| POST | `/blog/community/avatar-preview` | `sleepy_app/community/comments.py` · `blog_community_avatar_preview` |
| GET | `/blog/community/avatar/<int:comment_id>` | `sleepy_app/community/comments.py` · `blog_community_avatar` |
| GET | `/blog/community/feedback/avatar/<int:message_id>` | `sleepy_app/community/comments.py` · `blog_feedback_avatar` |
| GET | `/blog/community/feedback/room-avatar/<int:message_id>` | `sleepy_app/community/comments.py` · `blog_feedback_room_avatar` |
| GET | `/blog/community/likes` | `sleepy_app/community/comments.py` · `blog_community_likes` |
| POST | `/blog/community/likes/<path:target>` | `sleepy_app/community/comments.py` · `toggle_blog_community_like` |
| GET / POST | `/blog/community/comments/<page>` | `sleepy_app/community/comments.py` · `blog_community_comments` |
| PATCH | `/blog/community/comments/<int:comment_id>/pin` | `sleepy_app/community/comments.py` · `pin_blog_community_comment` |
| PATCH / DELETE | `/blog/community/comments/<int:comment_id>` | `sleepy_app/community/comments.py` · `manage_blog_community_comment` |
| GET / POST | `/blog/community/feedback` | `sleepy_app/community/feedback.py` · `blog_community_feedback` |
| POST | `/blog/community/feedback/messages` | `sleepy_app/community/feedback.py` · `add_blog_feedback_room_message` |
| POST | `/blog/community/feedback/<int:topic_id>/messages` | `sleepy_app/community/feedback.py` · `add_blog_feedback_message` |
| PATCH / DELETE | `/blog/community/feedback/<int:topic_id>` | `sleepy_app/community/feedback.py` · `manage_blog_feedback_topic` |
| POST | `/blog/community/feedback/from-comment` | `sleepy_app/community/feedback.py` · `convert_comment_tree_to_feedback` |
| POST | `/blog/community/feedback/merge` | `sleepy_app/community/feedback.py` · `merge_blog_feedback_topics` |
| GET / POST | `/blog/community/friend-applications` | `sleepy_app/community/feedback.py` · `blog_friend_applications` |
| POST | `/blog/community/friend-applications/status` | `sleepy_app/community/feedback.py` · `get_own_blog_friend_applications` |
| POST | `/blog/community/friend-applications/<int:application_id>` | `sleepy_app/community/feedback.py` · `update_blog_friend_application` |
| GET | `/images/<path:filename>` | `sleepy_app/media/service.py` · `serve_image` |
| GET | `/music/list` | `sleepy_app/media/service.py` · `music_list` |
| GET | `/music/<path:filename>` | `sleepy_app/media/service.py` · `music_stream` |
| GET | `/music/lyrics/<path:filename>` | `sleepy_app/media/service.py` · `music_lyrics` |
| GET | `/music/cover/<path:filename>` | `sleepy_app/media/service.py` · `music_cover` |
| POST | `/music/cover/upload` | `sleepy_app/media/service.py` · `music_cover_upload` |
| POST | `/music/upload` | `sleepy_app/media/service.py` · `music_upload` |
| POST | `/music/delete` | `sleepy_app/media/service.py` · `music_delete` |
| POST | `/music/reorder` | `sleepy_app/media/service.py` · `music_reorder` |
| GET / POST | `/calendar/events` | `sleepy_app/personal/service.py` · `calendar_events` |
| GET | `/calendar/holidays` | `sleepy_app/integrations/external.py` · `calendar_holidays` |
| GET | `/github/stats` | `sleepy_app/integrations/external.py` · `github_stats` |
| GET / POST | `/todos` | `sleepy_app/personal/service.py` · `todos` |
| GET / POST | `/blog/community/articles/<article_id>/comments` | `sleepy_app/community/articles.py` · `article_comments` |
| PATCH / DELETE | `/blog/community/articles/<article_id>/comments/<int:ident>` | `sleepy_app/community/articles.py` · `manage_article_comment` |
| GET | `/blog/community/articles/<article_id>/avatar/<int:ident>` | `sleepy_app/community/articles.py` · `article_comment_avatar` |
| POST | `/pet/reply` | `pet_ai/__init__.py` · 桌宠蓝图 |
