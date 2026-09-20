"""integrations/location business operations and HTTP handlers. State is application-scoped."""
import os
from sleepy_app.common.environment import configured_value
from datetime import datetime, timezone
from flask import request
import time
import json
import urllib.request
import urllib.error
import urllib.parse
import hashlib
import ipaddress
from sleepy_app.common.errors import GeoIpClientError, GeoIpUpstreamError, GeoIpRateLimitExceeded, WeatherConfigError, WeatherUpstreamError, WeatherRateLimitExceeded

class LocationService:
    def __init__(self, runtime):
        self.runtime = runtime

    def normalize_public_ip(self, client_ip):
        try:
            address = ipaddress.ip_address(str(client_ip or '').strip())
        except ValueError as exc:
            raise GeoIpClientError('client IP is invalid') from exc
        if not address.is_global:
            raise GeoIpClientError('client IP is not public')
        return str(address)


    def _seniverse_api_key(self):
        key = os.environ.get('SLEEPY_SENIVERSE_API_KEY', '').strip()
        if not key:
            raise WeatherConfigError('Seniverse API key is not configured')
        return key


    def fetch_seniverse_json(self, endpoint, client_ip, extra_params=None):
        """Fetch one Seniverse weather endpoint for an explicit visitor IP."""
        location = self.normalize_public_ip(client_ip)
        if endpoint not in {'now', 'daily'}:
            raise ValueError('unsupported Seniverse weather endpoint')
        params = {
            'key': self._seniverse_api_key(),
            # Do not use location=ip here: that would resolve the backend server IP.
            'location': location,
            'language': 'zh-Hans',
            'unit': 'c',
        }
        params.update(extra_params or {})
        url = (
            f'https://api.seniverse.com/v3/weather/{endpoint}.json?'
            f'{urllib.parse.urlencode(params)}'
        )
        upstream_request = urllib.request.Request(
            url,
            headers={'User-Agent': 'tonks-home-weather/2.0'},
        )
        try:
            with urllib.request.urlopen(upstream_request, timeout=8) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise WeatherRateLimitExceeded(
                    'weather provider rate limited', retry_after=60
                ) from exc
            raise WeatherUpstreamError('weather provider HTTP error') from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise WeatherUpstreamError('weather provider is unavailable') from exc
        if not isinstance(payload, dict) or not payload.get('results'):
            raise WeatherUpstreamError('weather provider returned an invalid payload')
        return payload


    def _required_int(self, value, field_name, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise WeatherUpstreamError(
                f'weather provider returned invalid {field_name}'
            ) from exc
        if not minimum <= parsed <= maximum:
            raise WeatherUpstreamError(
                f'weather provider returned invalid {field_name}'
            )
        return parsed


    def fetch_seniverse_weather(self, client_ip):
        """Return normalized current weather and tomorrow forecast for one visitor."""
        location = self.normalize_public_ip(client_ip)
        now_payload = self.fetch_seniverse_json('now', location)
        daily_payload = self.fetch_seniverse_json(
            'daily', location, {'start': 0, 'days': 2}
        )
        try:
            now_result = now_payload['results'][0]
            daily_result = daily_payload['results'][0]
            location_data = now_result['location']
            now_data = now_result['now']
            daily_rows = daily_result['daily']
            tomorrow_data = daily_rows[1]
            city = str(location_data['name']).strip()
            path = str(location_data.get('path') or '').strip()
            path_parts = [part.strip() for part in path.split(',') if part.strip()]
            if not city:
                raise KeyError('location.name')
            result = {
                'location': {
                    'id': str(location_data.get('id') or '').strip(),
                    'city': city,
                    'region': path_parts[1] if len(path_parts) >= 2 else '',
                    'country': str(location_data.get('country') or '').strip(),
                    'path': path,
                    'timezone': str(location_data.get('timezone') or '').strip(),
                },
                'now': {
                    'text': str(now_data['text']).strip(),
                    'code': self._required_int(now_data['code'], 'weather code', 0, 99),
                    'temperature': self._required_int(
                        now_data['temperature'], 'temperature', -80, 80
                    ),
                },
                'tomorrow': {
                    'date': str(tomorrow_data['date']).strip(),
                    'text': str(tomorrow_data['text_day']).strip(),
                    'code': self._required_int(
                        tomorrow_data['code_day'], 'tomorrow weather code', 0, 99
                    ),
                    'low': self._required_int(tomorrow_data['low'], 'tomorrow low', -80, 80),
                    'high': self._required_int(tomorrow_data['high'], 'tomorrow high', -80, 80),
                },
                'cached_at': datetime.now(timezone.utc).isoformat(),
                'stale': False,
            }
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise WeatherUpstreamError('weather provider returned an invalid payload') from exc
        if not result['now']['text'] or not result['tomorrow']['date'] or not result['tomorrow']['text']:
            raise WeatherUpstreamError('weather provider returned an incomplete payload')
        return result


    def _weather_cache_key(self, client_ip):
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        ) or self.runtime.geoip_runtime_salt
        return hashlib.sha256(
            f'{salt}|weather-cache|{client_ip}'.encode('utf-8')
        ).hexdigest()


    def _prune_weather_state(self, now):
        for key, entry in list(self.runtime.weather_cache.items()):
            if entry['stale_until'] <= now:
                self.runtime.weather_cache.pop(key, None)
        for key, attempted_at in list(self.runtime.weather_last_attempt.items()):
            if now - attempted_at >= 60:
                self.runtime.weather_last_attempt.pop(key, None)
        while self.runtime.weather_upstream_attempts and now - self.runtime.weather_upstream_attempts[0] >= 60:
            self.runtime.weather_upstream_attempts.popleft()


    def resolve_visitor_weather(self, client_ip):
        """Resolve weather without storing or returning the visitor's raw IP."""
        normalized_ip = self.normalize_public_ip(client_ip)
        cache_key = self._weather_cache_key(normalized_ip)
        now = time.monotonic()
        stale_data = None

        with self.runtime.weather_lock:
            self._prune_weather_state(now)
            cached = self.runtime.weather_cache.get(cache_key)
            if cached and cached['fresh_until'] > now:
                result = dict(cached['data'])
                result['stale'] = False
                return result
            if cached:
                stale_data = dict(cached['data'])
            last_attempt = self.runtime.weather_last_attempt.get(cache_key)
            if last_attempt is not None and now - last_attempt < self.runtime.WEATHER_VISITOR_RETRY_SECONDS:
                if stale_data:
                    stale_data['stale'] = True
                    return stale_data
                retry_after = self.runtime.WEATHER_VISITOR_RETRY_SECONDS - (now - last_attempt)
                raise WeatherRateLimitExceeded(
                    'visitor weather lookup retried too quickly', retry_after=retry_after
                )
            if len(self.runtime.weather_upstream_attempts) >= self.runtime.WEATHER_UPSTREAM_LIMIT_PER_MINUTE:
                if stale_data:
                    stale_data['stale'] = True
                    return stale_data
                retry_after = 60 - (now - self.runtime.weather_upstream_attempts[0])
                raise WeatherRateLimitExceeded(
                    'weather provider request budget exhausted', retry_after=retry_after
                )
            self.runtime.weather_last_attempt[cache_key] = now
            self.runtime.weather_upstream_attempts.append(now)

        try:
            result = self.fetch_seniverse_weather(normalized_ip)
        except (WeatherConfigError, WeatherUpstreamError, WeatherRateLimitExceeded):
            if stale_data:
                stale_data['stale'] = True
                return stale_data
            raise

        with self.runtime.weather_lock:
            if len(self.runtime.weather_cache) >= self.runtime.WEATHER_CACHE_MAX_ENTRIES:
                oldest_key = min(
                    self.runtime.weather_cache, key=lambda key: self.runtime.weather_cache[key]['stale_until']
                )
                self.runtime.weather_cache.pop(oldest_key, None)
            self.runtime.weather_cache[cache_key] = {
                'fresh_until': now + self.runtime.WEATHER_CACHE_TTL_SECONDS,
                'stale_until': now + self.runtime.WEATHER_CACHE_STALE_SECONDS,
                'data': dict(result),
            }
        return result


    def fetch_ip_api_location(self, client_ip):
        """Resolve one validated public client IP without retaining or returning it."""
        address = self.normalize_public_ip(client_ip)
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


    def _geoip_cache_key(self, client_ip):
        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        ) or self.runtime.geoip_runtime_salt
        return hashlib.sha256(
            f'{salt}|geoip-cache|{client_ip}'.encode('utf-8')
        ).hexdigest()


    def _prune_geoip_state(self, now):
        for key, (expires_at, _) in list(self.runtime.geoip_cache.items()):
            if expires_at <= now:
                self.runtime.geoip_cache.pop(key, None)
        for key, attempted_at in list(self.runtime.geoip_last_attempt.items()):
            if now - attempted_at >= 60:
                self.runtime.geoip_last_attempt.pop(key, None)
        while self.runtime.geoip_upstream_attempts and now - self.runtime.geoip_upstream_attempts[0] >= 60:
            self.runtime.geoip_upstream_attempts.popleft()


    def resolve_geoip_location(self, client_ip):
        """Resolve and cache coarse location using only a salted in-memory IP hash."""
        normalized_ip = self.normalize_public_ip(client_ip)
        cache_key = self._geoip_cache_key(normalized_ip)
        now = time.monotonic()

        with self.runtime.geoip_lock:
            self._prune_geoip_state(now)
            cached = self.runtime.geoip_cache.get(cache_key)
            if cached:
                return dict(cached[1])
            last_attempt = self.runtime.geoip_last_attempt.get(cache_key)
            if last_attempt is not None and now - last_attempt < self.runtime.GEOIP_VISITOR_RETRY_SECONDS:
                retry_after = self.runtime.GEOIP_VISITOR_RETRY_SECONDS - (now - last_attempt)
                raise GeoIpRateLimitExceeded(
                    'visitor location lookup retried too quickly', retry_after=retry_after
                )
            if len(self.runtime.geoip_upstream_attempts) >= self.runtime.GEOIP_UPSTREAM_LIMIT_PER_MINUTE:
                retry_after = 60 - (now - self.runtime.geoip_upstream_attempts[0])
                raise GeoIpRateLimitExceeded(
                    'location provider request budget exhausted', retry_after=retry_after
                )
            self.runtime.geoip_last_attempt[cache_key] = now
            self.runtime.geoip_upstream_attempts.append(now)

        result = self.fetch_ip_api_location(normalized_ip)

        with self.runtime.geoip_lock:
            if len(self.runtime.geoip_cache) >= self.runtime.GEOIP_CACHE_MAX_ENTRIES:
                oldest_key = min(self.runtime.geoip_cache, key=lambda key: self.runtime.geoip_cache[key][0])
                self.runtime.geoip_cache.pop(oldest_key, None)
            self.runtime.geoip_cache[cache_key] = (now + self.runtime.GEOIP_CACHE_TTL_SECONDS, dict(result))
        return result


    def get_geoip_client_address(self, req):
        """Return the public client address already verified and resolved by Waitress."""
        return self.normalize_public_ip(req.remote_addr)


    def visitor_weather(self):
        """Return IP-personalized weather without exposing the API key or visitor IP."""
        try:
            result = self.resolve_visitor_weather(self.get_geoip_client_address(request))
            payload = {'success': True, **result}
            response = self.runtime.app.response_class(
                response=json.dumps(payload, ensure_ascii=False),
                status=200,
                mimetype='application/json',
            )
        except GeoIpClientError:
            response = self.runtime.app.response_class(
                response=json.dumps({
                    'success': False,
                    'code': 'weather unavailable',
                    'message': 'visitor location is unavailable',
                }, ensure_ascii=False),
                status=400,
                mimetype='application/json',
            )
        except WeatherConfigError:
            response = self.runtime.app.response_class(
                response=json.dumps({
                    'success': False,
                    'code': 'weather not configured',
                    'message': 'weather service is not configured',
                }, ensure_ascii=False),
                status=503,
                mimetype='application/json',
            )
        except WeatherRateLimitExceeded as exc:
            response = self.runtime.app.response_class(
                response=json.dumps({
                    'success': False,
                    'code': 'weather rate limited',
                    'message': 'weather lookup is temporarily rate limited',
                }, ensure_ascii=False),
                status=429,
                mimetype='application/json',
            )
            response.headers['Retry-After'] = str(exc.retry_after)
        except WeatherUpstreamError:
            response = self.runtime.app.response_class(
                response=json.dumps({
                    'success': False,
                    'code': 'weather upstream error',
                    'message': 'weather provider is temporarily unavailable',
                }, ensure_ascii=False),
                status=502,
                mimetype='application/json',
            )
        response.headers['Cache-Control'] = 'private, no-store'
        return response


    def geoip(self):
        """Return coarse location for the current visitor without exposing/storing their IP."""
        try:
            result = self.resolve_geoip_location(self.get_geoip_client_address(request))
            response = self.runtime.app.response_class(
                response=json.dumps(result, ensure_ascii=False),
                status=200,
                mimetype='application/json',
            )
        except GeoIpClientError:
            response = self.runtime.app.response_class(
                response=json.dumps({
                    'success': False,
                    'code': 'geoip unavailable',
                    'message': 'visitor location is unavailable',
                }, ensure_ascii=False),
                status=400,
                mimetype='application/json',
            )
        except GeoIpRateLimitExceeded as exc:
            response = self.runtime.app.response_class(
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
            response = self.runtime.app.response_class(
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

