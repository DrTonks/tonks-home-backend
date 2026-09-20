"""community/comments business operations and HTTP handlers. State is application-scoped."""
from sleepy_app.community.avatars import community_avatar_svg, community_qq_number, community_qq_avatar_url
import sleepy_app.common.responses as u
from flask import Response, redirect, request
import urllib.request
import urllib.error
import urllib.parse
from sleepy_app.community.store import CommunityRateLimitExceeded, CommunityValidationError, community_limit_from_env, normalize_comment_page, normalize_email, normalize_like_target, validate_comment_payload
from sleepy_app.community.moderation import ModerationResult

class CommentService:
    def __init__(self, runtime):
        self.runtime = runtime

    def blog_community_avatar_preview(self):
        """Resolve an avatar for a visitor identity without storing or returning its email."""
        if request.content_length is not None and request.content_length > 2048:
            return self.runtime.common_security.reterr(code='body too large', message='avatar request exceeds 2048 bytes')
        try:
            payload = request.get_json(force=False, silent=False)
            if not isinstance(payload, dict):
                raise CommunityValidationError('invalid_payload', 'request body must be an object')
            email = normalize_email(payload.get('email'))
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except Exception:
            return self.runtime.common_security.reterr(code='bad request', message='invalid JSON body'), 400

        qq_number = community_qq_number(email)
        if qq_number:
            avatar_url = community_qq_avatar_url(qq_number)
        else:
            avatar_url = 'data:image/svg+xml,' + urllib.parse.quote(community_avatar_svg(email), safe='')
        response = u.format_dict({'success': True, 'avatar_url': avatar_url})
        response.headers['Cache-Control'] = 'no-store'
        return response


    def blog_community_avatar(self, comment_id):
        """Use QQ avatars for numeric QQ mailboxes, otherwise a private local SVG."""
        avatar = self.runtime.community_store.get_comment_avatar(comment_id)
        if avatar is None:
            return self.runtime.common_security.reterr(code='not found', message='avatar not found'), 404

        fallback = request.args.get('fallback', '')
        qq_number = community_qq_number(avatar['email'])
        if qq_number and not fallback:
            response = redirect(community_qq_avatar_url(qq_number), code=302)
            response.headers['Cache-Control'] = 'public, max-age=3600'
        else:
            response = Response(community_avatar_svg(avatar['email']), mimetype='image/svg+xml')
            response.headers['Cache-Control'] = 'public, max-age=86400'
            response.headers['Content-Security-Policy'] = "default-src 'none'"
        return response


    def blog_feedback_avatar(self, message_id):
        """Resolve a feedback-message avatar without exposing its stored email."""
        avatar = self.runtime.community_store.get_feedback_avatar(message_id)
        if avatar is None:
            return self.runtime.common_security.reterr(code='not found', message='avatar not found'), 404
        fallback = request.args.get('fallback', '')
        qq_number = community_qq_number(avatar['email'])
        if qq_number and not fallback:
            response = redirect(community_qq_avatar_url(qq_number), code=302)
            response.headers['Cache-Control'] = 'public, max-age=3600'
        else:
            response = Response(community_avatar_svg(avatar['email']), mimetype='image/svg+xml')
            response.headers['Cache-Control'] = 'public, max-age=86400'
            response.headers['Content-Security-Policy'] = "default-src 'none'"
        return response


    def blog_feedback_room_avatar(self, message_id):
        """Resolve a feedback-room chat avatar without exposing its stored email."""
        avatar = self.runtime.community_store.get_feedback_room_avatar(message_id)
        if avatar is None:
            return self.runtime.common_security.reterr(code='not found', message='avatar not found'), 404
        fallback = request.args.get('fallback', '')
        qq_number = community_qq_number(avatar['email'])
        if qq_number and not fallback:
            response = redirect(community_qq_avatar_url(qq_number), code=302)
            response.headers['Cache-Control'] = 'public, max-age=3600'
        else:
            response = Response(community_avatar_svg(avatar['email']), mimetype='image/svg+xml')
            response.headers['Cache-Control'] = 'public, max-age=86400'
            response.headers['Content-Security-Policy'] = "default-src 'none'"
        return response


    def blog_community_likes(self):
        """Read like totals and the current anonymous visitor's state."""
        raw_targets = request.args.getlist('targets')
        if len(raw_targets) == 1 and ',' in raw_targets[0]:
            raw_targets = raw_targets[0].split(',')
        if len(raw_targets) > 100:
            return self.runtime.common_security.reterr(code='bad request', message='at most 100 targets are allowed')
        try:
            targets = [normalize_like_target(value) for value in raw_targets]
            likes = self.runtime.community_store.get_likes(targets, self.runtime.common_security.get_blog_visitor_hash(request))
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message)
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to read likes')
        response = u.format_dict({'success': True, 'likes': likes})
        response.headers['Cache-Control'] = 'no-store'
        return response


    def toggle_blog_community_like(self, target):
        """Toggle one contribution for the current anonymous visitor and target."""
        try:
            normalized = normalize_like_target(target)
            ip_key, client_key = self.runtime.common_security.get_community_rate_limit_keys(request)
            self.runtime.community_like_limiter.check(ip_key, client_key)
            count, liked = self.runtime.community_store.toggle_like(
                normalized,
                self.runtime.common_security.get_blog_visitor_hash(request),
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message)
        except CommunityRateLimitExceeded:
            return self.runtime.common_security.reterr(code='rate limited', message='too many like changes')
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to update like')
        response = u.format_dict({
            'success': True,
            'target': normalized,
            'count': count,
            'liked': liked,
        })
        response.headers['Cache-Control'] = 'no-store'
        return response


    def blog_community_comments(self, page):
        """List published comments or submit one AI-moderated comment/reply."""
        try:
            normalized_page = normalize_comment_page(page)
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message)

        admin_requested = bool(
            request.args.get('secret') or request.headers.get('X-Admin-Secret')
        )
        if admin_requested and not self.runtime.common_security.verify_admin_secret():
            return self.runtime.common_security.reterr(code='not authorized', message='invalid admin secret'), 401

        if request.method == 'GET':
            try:
                comments = self.runtime.community_store.list_public_comments(
                    normalized_page,
                    include_nonpublished=self.runtime.common_security.verify_admin_secret(),
                    viewer_owner_hash=self.runtime.common_security.get_community_owner_hash(request),
                )
            except Exception:
                return self.runtime.common_security.reterr(code='server error', message='failed to read comments')
            response = u.format_dict({
                'success': True,
                'page': normalized_page,
                'comments': comments,
                'count': len(comments),
            })
            response.headers['Cache-Control'] = 'no-store'
            return response

        if request.content_length is not None and request.content_length > 8192:
            return self.runtime.common_security.reterr(code='body too large', message='comment request exceeds 8192 bytes')
        try:
            payload = request.get_json(force=False, silent=False)
            submission = validate_comment_payload(normalized_page, payload)
            is_admin = self.runtime.common_security.verify_admin_secret()
            actor_hash = self.runtime.common_security.get_community_actor_hash(submission.email)
            owner_hash = self.runtime.common_security.get_community_owner_hash(request)
            parent_context = self.runtime.community_store.get_parent_context(
                normalized_page,
                submission.parent_id,
            )
            ip_key, client_key = self.runtime.common_security.get_community_rate_limit_keys(request)
            self.runtime.community_comment_limiter.check(ip_key, client_key, actor_hash)
            daily_limit = community_limit_from_env(
                'SLEEPY_COMMENT_DAILY_LIMIT', 20, 200
            )
            self.runtime.community_store.reserve_comment_quota(
                {
                    f'email:{actor_hash}',
                    f'ip:{ip_key}',
                    f'client:{client_key}',
                },
                daily_limit=daily_limit,
            )
            history = self.runtime.community_store.actor_history(actor_hash)
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message)
        except CommunityRateLimitExceeded as exc:
            message = (
                'daily comment limit reached'
                if str(exc) == 'daily_limit'
                else 'too many comments in a short time'
            )
            return self.runtime.common_security.reterr(code='rate limited', message=message)
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid comment object')

        moderation = (
            ModerationResult('allow', 'admin', 'administrator comment')
            if is_admin
            else self.runtime.comment_moderator.moderate(
                page=normalized_page,
                nickname=submission.nickname,
                content=submission.content,
                reply_to_name=parent_context['nickname'] if parent_context else '',
                history=history,
            )
        )
        status = {
            'allow': 'published',
            'reject': 'rejected',
            'review': 'pending',
        }[moderation.decision]
        try:
            comment = self.runtime.community_store.create_comment(
                submission,
                actor_hash,
                status=status,
                moderation_reason=f'{moderation.category}: {moderation.reason}',
                parent_context=parent_context,
                is_admin=is_admin,
                owner_hash=owner_hash,
            )
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to save comment')

        if status == 'rejected':
            return self.runtime.common_security.reterr(
                code='comment rejected',
                message='评论未通过内容审核，请避免广告、重复内容或无意义灌水',
            )
        response = u.format_dict({
            'success': True,
            'status': status,
            'comment': comment if status == 'published' else None,
            'message': (
                '评论已发布'
                if status == 'published'
                else '评论已提交，等待人工确认'
            ),
        })
        response.headers['Cache-Control'] = 'no-store'
        return response


    def pin_blog_community_comment(self, comment_id):
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err, 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('is_pinned'), bool):
            return self.runtime.common_security.reterr(code='invalid_pin', message='is_pinned must be a boolean'), 400
        if not self.runtime.community_store.set_comment_pin(comment_id, payload['is_pinned']):
            return self.runtime.common_security.reterr(code='not found', message='published comment not found'), 404
        response = u.format_dict({'success': True, 'comment_id': comment_id, 'is_pinned': payload['is_pinned']})
        response.headers['Cache-Control'] = 'no-store'
        return response


    def manage_blog_community_comment(self, comment_id):
        """Moderate or soft-delete a comment using the existing admin secret."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err, 401
        if request.method == 'PATCH':
            try:
                payload = request.get_json(force=False, silent=False)
                if not isinstance(payload, dict):
                    raise CommunityValidationError(
                        'invalid_body', 'expected a JSON object'
                    )
                status = str(payload.get('status') or '').strip().lower()
                reason = str(payload.get('moderation_reason') or '').strip()
                updated = self.runtime.community_store.update_comment_status(
                    comment_id,
                    status,
                    reason,
                )
            except CommunityValidationError as exc:
                return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
            except (TypeError, ValueError):
                return self.runtime.common_security.reterr(code='bad request', message='comment id is invalid'), 400
            except Exception:
                return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid moderation update'), 400
            if not updated:
                return self.runtime.common_security.reterr(code='not found', message='comment not found'), 404
            response = u.format_dict({
                'success': True,
                'comment_id': comment_id,
                'status': status,
            })
            response.headers['Cache-Control'] = 'no-store'
            return response
        try:
            deleted = self.runtime.community_store.delete_comment(comment_id)
        except (TypeError, ValueError):
            return self.runtime.common_security.reterr(code='bad request', message='comment id is invalid'), 400
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to delete comment'), 500
        if not deleted:
            return self.runtime.common_security.reterr(code='not found', message='comment not found'), 404
        response = u.format_dict({'success': True, 'deleted': deleted})
        response.headers['Cache-Control'] = 'no-store'
        return response

