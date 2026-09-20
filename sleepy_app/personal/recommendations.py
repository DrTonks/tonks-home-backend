"""personal/recommendations business operations and HTTP handlers. State is application-scoped."""
import sleepy_app.common.responses as u
from flask import request
from sleepy_app.personal.recommendation_store import RecommendationRateLimitExceeded, RecommendationValidationError, validate_recommendation_filters, validate_recommendation_payload
from sleepy_app.common.errors import GeoIpClientError, GeoIpUpstreamError, GeoIpRateLimitExceeded

class RecommendationService:
    def __init__(self, runtime):
        self.runtime = runtime

    def get_automatic_recommendation_city(self, req):
        """Best-effort coarse city lookup; never stores or returns the visitor IP."""
        try:
            client_ip = self.runtime.integrations_location.get_geoip_client_address(req)
            return str(self.runtime.integrations_location.resolve_geoip_location(client_ip).get('city') or 'unknown')[:50]
        except (GeoIpClientError, GeoIpUpstreamError, GeoIpRateLimitExceeded):
            return 'unknown'


    def pet_recommendations(self):
        """Submit or list song/book/game/anime recommendations for the site author."""
        if request.method == 'POST':
            if request.content_length is not None and request.content_length > 1024:
                return self.runtime.common_security.reterr(code='body too large', message='request body exceeds 1024 bytes')
            try:
                payload = request.get_json(force=False, silent=False)
                category, content, user_name, city = validate_recommendation_payload(payload)
                is_admin = self.runtime.common_security.verify_admin_secret()
                rate_limit_keys = None
                if not is_admin:
                    ip_key, client_key = self.runtime.common_security.get_recommendation_rate_limit_keys(request)
                    self.runtime.recommendation_limiter.check(ip_key, client_key)
                    rate_limit_keys = {
                        f'ip:{ip_key}', f'client:{client_key}',
                    }
                    # The frontend weather system already caches the visitor's city.
                    # Reuse it when supplied and keep GeoIP as a compatibility fallback
                    # for older clients or visitors without a usable location cache.
                    if city == 'unknown':
                        city = self.get_automatic_recommendation_city(request)
            except RecommendationValidationError as exc:
                return self.runtime.common_security.reterr(code=exc.code, message=exc.message)
            except RecommendationRateLimitExceeded as exc:
                if str(exc) == 'daily_limit':
                    return self.runtime.common_security.reterr(code='daily limit reached', message='one recommendation per day')
                return self.runtime.common_security.reterr(code='rate limited', message='recommendation limit exceeded')
            except Exception:
                return self.runtime.common_security.reterr(code='invalid JSON', message='expected a JSON object')

            try:
                if is_admin:
                    recommendation = self.runtime.recommendation_store.create(category, content, user_name, city)
                else:
                    recommendation = self.runtime.recommendation_store.create_public_once_per_day(
                        category, content, user_name, city, rate_limit_keys or set()
                    )
            except RecommendationRateLimitExceeded:
                return self.runtime.common_security.reterr(code='daily limit reached', message='one recommendation per day')
            except Exception:
                return self.runtime.common_security.reterr(code='server error', message='failed to save recommendation')
            return u.format_dict({
                'success': True,
                'code': 'OK',
                'recommendation': recommendation,
            })

        is_admin = self.runtime.common_security.verify_admin_secret()
        try:
            category, created_date = validate_recommendation_filters(
                request.args.get('category'),
                request.args.get('date') if is_admin else None,
            )
        except RecommendationValidationError as exc:
            return self.runtime.common_security.reterr(code=exc.code, message=exc.message)
        try:
            recommendations = self.runtime.recommendation_store.list(
                category=category,
                created_date=created_date,
                limit=None if is_admin else 10,
            )
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to read recommendations')
        return u.format_dict({
            'success': True,
            'recommendations': recommendations,
            'count': len(recommendations),
            'filters': {'category': category, 'date': created_date},
            'scope': 'admin' if is_admin else 'public',
        })


    def delete_pet_recommendation(self, recommendation_id):
        """Delete one recommendation using the existing administrator secret."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err
        try:
            deleted = self.runtime.recommendation_store.delete(recommendation_id)
        except Exception:
            return self.runtime.common_security.reterr(code='server error', message='failed to delete recommendation')
        if not deleted:
            return self.runtime.common_security.reterr(code='not found', message='recommendation not found')
        return u.format_dict({
            'success': True,
            'code': 'OK',
            'deleted': recommendation_id,
        })

