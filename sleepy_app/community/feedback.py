"""community/feedback business operations and HTTP handlers. State is application-scoped."""
import sleepy_app.common.responses as u
from flask import request
import secrets
from sleepy_app.community.store import CommunityRateLimitExceeded, CommunityValidationError, community_limit_from_env, validate_friend_application_payload, validate_feedback_message_payload, validate_feedback_topic_payload
from sleepy_app.community.moderation import ModerationResult

class FeedbackService:
    def __init__(self, runtime):
        self.runtime = runtime

    def moderate_feedback_submission(self, submission, *, is_admin=False):
        """Apply the existing community identity, quota, and moderation policy to feedback."""
        actor_hash = self.runtime.common_security.get_community_actor_hash(submission.email)
        owner_hash = self.runtime.common_security.get_community_owner_hash(request)
        ip_key, client_key = self.runtime.common_security.get_community_rate_limit_keys(request)
        self.runtime.community_comment_limiter.check(ip_key, client_key, actor_hash)
        daily_limit = community_limit_from_env('SLEEPY_COMMENT_DAILY_LIMIT', 20, 200)
        self.runtime.community_store.reserve_comment_quota(
            {f'email:{actor_hash}', f'ip:{ip_key}', f'client:{client_key}'},
            daily_limit=daily_limit,
        )
        history = self.runtime.community_store.actor_history(actor_hash)
        history.extend(self.runtime.community_store.feedback_actor_history(actor_hash))
        history.sort(key=lambda item: str(item.get('created_at') or ''))
        moderation = (
            ModerationResult('allow', 'admin', 'administrator feedback')
            if is_admin
            else self.runtime.comment_moderator.moderate(
                page='feedback',
                nickname=submission.nickname,
                content=submission.content,
                reply_to_name='',
                history=history,
            )
        )
        return (
            actor_hash,
            owner_hash,
            {'allow': 'published', 'reject': 'rejected', 'review': 'pending'}[
                moderation.decision
            ],
            f'{moderation.category}: {moderation.reason}',
        )


    def blog_community_feedback(self):
        """List feedback topics or create a moderated topic."""
        admin_requested = bool(
            request.args.get('secret') or request.headers.get('X-Admin-Secret')
        )
        if admin_requested and not self.runtime.common_security.verify_admin_secret():
            return self.runtime.common_security.reterr(code='not authorized', message='invalid admin secret'), 401
        if request.method == 'GET':
            try:
                include_nonpublished = self.runtime.common_security.verify_admin_secret()
                viewer_owner_hash = self.runtime.common_security.get_community_owner_hash(request)
                topics = self.runtime.community_store.list_feedback_topics(
                    include_nonpublished=include_nonpublished,
                    viewer_owner_hash=viewer_owner_hash,
                )
                room_messages = self.runtime.community_store.list_feedback_room_messages(
                    include_nonpublished=include_nonpublished,
                    viewer_owner_hash=viewer_owner_hash,
                )
            except Exception:
                return self.runtime.common_security.reterr(code='server error', message='failed to read feedback'), 500
            response = u.format_dict(
                {
                    'success': True,
                    'topics': topics,
                    'room_messages': room_messages,
                    'count': len(topics),
                }
            )
            response.headers['Cache-Control'] = 'no-store'
            return response
        if request.content_length is not None and request.content_length > 12288:
            return self.runtime.common_security.reterr(code='body too large', message='feedback request exceeds 12288 bytes'), 413
        try:
            payload = request.get_json(force=False, silent=False)
            submission = validate_feedback_topic_payload(payload)
            is_admin = self.runtime.common_security.verify_admin_secret()
            actor_hash, owner_hash, status, reason = self.moderate_feedback_submission(
                submission.author, is_admin=is_admin
            )
            topic = self.runtime.community_store.create_feedback_topic(
                submission,
                actor_hash,
                status=status,
                moderation_reason=reason,
                is_admin=is_admin,
                owner_hash=owner_hash,
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except CommunityRateLimitExceeded as exc:
            message = (
                'daily comment limit reached'
                if str(exc) == 'daily_limit'
                else 'too many comments in a short time'
            )
            return self.runtime.common_security.reterr(code='rate limited', message=message), 429
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid feedback object'), 400
        if status == 'rejected':
            return self.runtime.common_security.reterr(
                code='feedback rejected',
                message='反馈未通过内容审核，请避免广告、重复内容或无意义灌水',
            ), 400
        response = u.format_dict(
            {
                'success': True,
                'status': status,
                'topic': topic,
                'message': '反馈已发布' if status == 'published' else '反馈已提交，等待人工确认',
            }
        )
        response.headers['Cache-Control'] = 'no-store'
        return response, 201


    def add_blog_feedback_room_message(self):
        """Post a normal moderated chat message in the Feedback room."""
        if request.content_length is not None and request.content_length > 8192:
            return self.runtime.common_security.reterr(code='body too large', message='feedback message exceeds 8192 bytes'), 413
        try:
            payload = request.get_json(force=False, silent=False)
            submission = validate_feedback_message_payload(payload)
            is_admin = self.runtime.common_security.verify_admin_secret()
            actor_hash, owner_hash, status, reason = self.moderate_feedback_submission(
                submission, is_admin=is_admin
            )
            message_item = self.runtime.community_store.add_feedback_room_message(
                submission,
                actor_hash,
                status=status,
                moderation_reason=reason,
                is_admin=is_admin,
                owner_hash=owner_hash,
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except CommunityRateLimitExceeded as exc:
            message_text = (
                'daily comment limit reached'
                if str(exc) == 'daily_limit'
                else 'too many comments in a short time'
            )
            return self.runtime.common_security.reterr(code='rate limited', message=message_text), 429
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid feedback message'), 400
        if status == 'rejected':
            return self.runtime.common_security.reterr(
                code='feedback rejected',
                message='消息未通过内容审核，请避免广告、重复内容或无意义灌水',
            ), 400
        response = u.format_dict(
            {
                'success': True,
                'status': status,
                'message_item': message_item if status == 'published' else None,
                'message': '消息已发送' if status == 'published' else '消息已提交，等待人工确认',
            }
        )
        response.headers['Cache-Control'] = 'no-store'
        return response, 201


    def add_blog_feedback_message(self, topic_id):
        """Append a moderated public message to an existing feedback topic."""
        if request.content_length is not None and request.content_length > 8192:
            return self.runtime.common_security.reterr(code='body too large', message='feedback reply exceeds 8192 bytes'), 413
        try:
            payload = request.get_json(force=False, silent=False)
            submission = validate_feedback_message_payload(payload)
            is_admin = self.runtime.common_security.verify_admin_secret()
            actor_hash, owner_hash, status, reason = self.moderate_feedback_submission(
                submission, is_admin=is_admin
            )
            message = self.runtime.community_store.add_feedback_message(
                topic_id,
                submission,
                actor_hash,
                status=status,
                moderation_reason=reason,
                is_admin=is_admin,
                owner_hash=owner_hash,
            )
        except CommunityValidationError as exc:
            status_code = 404 if exc.code == 'not_found' else 400
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), status_code
        except CommunityRateLimitExceeded as exc:
            message_text = (
                'daily comment limit reached'
                if str(exc) == 'daily_limit'
                else 'too many comments in a short time'
            )
            return self.runtime.common_security.reterr(code='rate limited', message=message_text), 429
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid feedback reply'), 400
        if status == 'rejected':
            return self.runtime.common_security.reterr(
                code='feedback rejected',
                message='回复未通过内容审核，请避免广告、重复内容或无意义灌水',
            ), 400
        response = u.format_dict(
            {
                'success': True,
                'status': status,
                'message_item': message if status == 'published' else None,
                'message': '回复已发布' if status == 'published' else '回复已提交，等待人工确认',
            }
        )
        response.headers['Cache-Control'] = 'no-store'
        return response, 201


    def manage_blog_feedback_topic(self, topic_id):
        """Update or soft-delete a feedback topic as administrator."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err, 401
        if request.method == 'DELETE':
            try:
                deleted = self.runtime.community_store.delete_feedback_topic(topic_id)
            except Exception:
                return self.runtime.common_security.reterr(code='server error', message='failed to delete feedback topic'), 500
            if not deleted:
                return self.runtime.common_security.reterr(code='not found', message='feedback topic not found'), 404
            response = u.format_dict({'success': True, 'deleted': deleted})
            response.headers['Cache-Control'] = 'no-store'
            return response
        try:
            payload = request.get_json(force=False, silent=False)
            if not isinstance(payload, dict):
                raise CommunityValidationError('invalid_body', 'expected a JSON object')
            updated = self.runtime.community_store.update_feedback_topic(
                topic_id,
                title=payload.get('title') if 'title' in payload else None,
                kind=payload.get('kind') if 'kind' in payload else None,
                status=payload.get('status') if 'status' in payload else None,
                resolution_note=(
                    payload.get('resolution_note') if 'resolution_note' in payload else None
                ),
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid feedback update'), 400
        if not updated:
            return self.runtime.common_security.reterr(code='not found', message='feedback topic not found'), 404
        response = u.format_dict({'success': True, 'topic_id': topic_id})
        response.headers['Cache-Control'] = 'no-store'
        return response


    def convert_comment_tree_to_feedback(self):
        """Move one complete public comment tree into a feedback topic."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err, 401
        try:
            payload = request.get_json(force=False, silent=False)
            if not isinstance(payload, dict):
                raise CommunityValidationError('invalid_body', 'expected a JSON object')
            topic_id = self.runtime.community_store.attach_comment_tree_to_feedback(
                int(payload.get('root_comment_id')),
                topic_id=(int(payload['topic_id']) if payload.get('topic_id') else None),
                title=str(payload.get('title') or ''),
                kind=str(payload.get('kind') or 'bug').strip().lower(),
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except (TypeError, ValueError):
            return self.runtime.common_security.reterr(code='invalid_comment', message='root comment id is invalid'), 400
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to convert comment tree'), 500
        response = u.format_dict({'success': True, 'topic_id': topic_id})
        response.headers['Cache-Control'] = 'no-store'
        return response, 201


    def merge_blog_feedback_topics(self):
        """Merge selected feedback topics into one retained target without deleting history."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err, 401
        try:
            payload = request.get_json(force=False, silent=False)
            if not isinstance(payload, dict) or not isinstance(payload.get('source_topic_ids'), list):
                raise CommunityValidationError('invalid_body', 'source_topic_ids must be an array')
            merged = self.runtime.community_store.merge_feedback_topics(
                int(payload.get('target_topic_id')),
                [int(item) for item in payload['source_topic_ids']],
                title=(str(payload['title']) if 'title' in payload else None),
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except (TypeError, ValueError):
            return self.runtime.common_security.reterr(code='invalid_topic', message='feedback topic id is invalid'), 400
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to merge feedback topics'), 500
        response = u.format_dict(
            {'success': True, 'target_topic_id': int(payload['target_topic_id']), 'merged': merged}
        )
        response.headers['Cache-Control'] = 'no-store'
        return response


    def blog_friend_applications(self):
        """Submit a pending friend-link application or list applications for admins."""
        if request.method == 'GET':
            auth_err = self.runtime.common_security.require_admin()
            if auth_err:
                return auth_err, 401
            status = request.args.get('status') or None
            try:
                applications = self.runtime.community_store.list_friend_applications(status=status)
            except CommunityValidationError as exc:
                return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
            response = u.format_dict({'success': True, 'applications': applications})
            response.headers['Cache-Control'] = 'no-store'
            return response

        if request.content_length is not None and request.content_length > 12288:
            return self.runtime.common_security.reterr(code='body too large', message='application request exceeds 12288 bytes'), 413
        try:
            payload = request.get_json(force=False, silent=False)
            submission = validate_friend_application_payload(payload)
            ip_key, client_key = self.runtime.common_security.get_community_rate_limit_keys(request)
            self.runtime.friend_application_limiter.check(ip_key, client_key)
            tracking_token = secrets.token_urlsafe(32)
            application = self.runtime.community_store.create_friend_application(
                submission,
                self.runtime.common_security.get_blog_visitor_hash(request),
                tracking_hash=self.runtime.common_security.hash_friend_application_token(tracking_token),
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except CommunityRateLimitExceeded:
            return self.runtime.common_security.reterr(code='rate limited', message='friend-link application limit reached'), 429
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid friend-link application'), 400
        response = u.format_dict({
            'success': True,
            'status': application['status'],
            'application': {
                key: application[key]
                for key in (
                    'id', 'name', 'website', 'avatar', 'description', 'status',
                    'moderation_note', 'created_at', 'updated_at'
                )
            },
            'tracking_token': tracking_token,
            'message': '友链申请已提交，等待审核',
        })
        response.headers['Cache-Control'] = 'no-store'
        return response, 201


    def get_own_blog_friend_applications(self):
        """Return applications addressed by private tracking tokens held by the visitor."""
        if request.content_length is not None and request.content_length > 16384:
            return self.runtime.common_security.reterr(code='body too large', message='status request exceeds 16384 bytes'), 413
        try:
            payload = request.get_json(force=False, silent=False)
            if not isinstance(payload, dict) or not isinstance(payload.get('tokens'), list):
                raise CommunityValidationError('invalid_body', 'tokens must be an array')
            tokens = list(dict.fromkeys(str(item or '').strip() for item in payload['tokens']))
            if len(tokens) > 20:
                raise CommunityValidationError(
                    'too_many_tracking_tokens', 'at most 20 tracking tokens are allowed'
                )
            tracking_hashes = [self.runtime.common_security.hash_friend_application_token(token) for token in tokens]
            applications = self.runtime.community_store.list_friend_applications_by_tracking_hashes(
                tracking_hashes
            )
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected valid tracking tokens'), 400
        response = u.format_dict({'success': True, 'applications': applications})
        response.headers['Cache-Control'] = 'no-store'
        return response


    def update_blog_friend_application(self, application_id):
        """Approve or reject a friend-link application for the future management site."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err, 401
        try:
            payload = request.get_json(force=False, silent=False)
            if not isinstance(payload, dict):
                raise CommunityValidationError('invalid_body', 'expected a JSON object')
            status = str(payload.get('status') or '').strip().lower()
            note = str(payload.get('moderation_note') or '').strip()
            updated = self.runtime.community_store.update_friend_application(application_id, status, note)
        except CommunityValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message), 400
        except Exception:
            return self.runtime.common_security.reterr(code='invalid JSON', message='expected a valid application update'), 400
        if not updated:
            return self.runtime.common_security.reterr(code='not found', message='friend-link application not found'), 404
        response = u.format_dict({'success': True, 'status': status})
        response.headers['Cache-Control'] = 'no-store'
        return response

