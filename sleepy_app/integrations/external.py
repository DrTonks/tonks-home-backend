"""integrations/external business operations and HTTP handlers. State is application-scoped."""
import os
from pathlib import Path
from sleepy_app.integrations.calendar import HolidayCalendar
from sleepy_app.status.heatmap import _percentile_thresholds, _intensity_level
from sleepy_app.common.environment import configured_value
import sleepy_app.common.responses as u
from datetime import datetime, timezone
from flask import request
import time
import json
import tempfile
import urllib.request
import urllib.error
import urllib.parse

class ExternalService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calendar = HolidayCalendar(Path(runtime.CALENDAR_CACHE_DIR))

    def _read_github_stats_cache(self):
        try:
            with open(self.runtime.GITHUB_CACHE_FILE, 'r', encoding='utf-8') as file:
                cached = json.load(file)
            if cached.get('version') != self.runtime.GITHUB_CACHE_VERSION:
                return None
            if not isinstance(cached.get('data'), dict):
                return None
            return cached
        except (OSError, ValueError, TypeError):
            return None


    def _write_github_stats_cache(self, result, now=None):
        now = time.time() if now is None else now
        cache_dir = os.path.dirname(os.path.abspath(self.runtime.GITHUB_CACHE_FILE))
        os.makedirs(cache_dir, exist_ok=True)
        payload = {
            'version': self.runtime.GITHUB_CACHE_VERSION,
            'cachedAt': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'cachedAtEpoch': now,
            'data': result,
        }
        fd, tmp_path = tempfile.mkstemp(prefix='github_stats_', suffix='.json', dir=cache_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(payload, file, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.runtime.GITHUB_CACHE_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


    def fetch_github_contributions(self):
        """Return a fresh 24-hour cache entry, refreshing it lazily when expired."""
        now = time.time()
        with self.runtime.github_cache_lock:
            cached = self._read_github_stats_cache()
            if cached and now - float(cached.get('cachedAtEpoch', 0)) < self.runtime.GITHUB_CACHE_TTL_SECONDS:
                return cached['data']

            result = self._fetch_github_contributions_from_api()
            if 'error' not in result:
                try:
                    self._write_github_stats_cache(result, now)
                except OSError as exc:
                    u.error(f'GitHub cache write failed: {exc}')
                return result

            # A stale cache is preferable to hiding the GitHub card during a
            # temporary API, network, or token failure.
            if cached:
                return cached['data']
            return result


    def _aggregate_github_languages(self, viewer):
        """Count owned repositories and distinct external contribution repositories."""
        languages = {}
        counted_repositories = set()

        def add_repository(repo):
            repo_key = repo.get('nameWithOwner') or repo.get('name')
            lang = repo.get('primaryLanguage')
            if not repo_key or repo_key.lower() in counted_repositories:
                return
            if not lang or not lang.get('name'):
                return
            counted_repositories.add(repo_key.lower())
            name = lang['name']
            repo_count = languages.get(name, {}).get('repoCount', 0) + 1
            languages[name] = {
                'name': name,
                'color': lang.get('color', '#858585'),
                'repoCount': repo_count,
                # Compatibility alias: this value is now a repository count.
                'stars': repo_count
            }

        for repo in viewer['repositories']['nodes']:
            add_repository(repo)

        viewer_login = viewer['login'].lower()
        for contribution in viewer['contributionsCollection'].get(
            'commitContributionsByRepository', []
        ):
            repo = contribution.get('repository') or {}
            owner_login = (repo.get('owner') or {}).get('login')
            total_count = (contribution.get('contributions') or {}).get('totalCount', 0)
            if owner_login and owner_login.lower() == viewer_login:
                continue
            if total_count < 1:
                continue
            add_repository(repo)

        return sorted(
            languages.values(),
            key=lambda item: (-item['repoCount'], item['name'].lower())
        )


    def _fetch_github_contributions_from_api(self):
        """通过 GitHub GraphQL API 获取贡献热力图数据"""
        token = configured_value(self.runtime.d, 'SLEEPY_GITHUB_TOKEN', 'github_token', '')
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
              commitContributionsByRepository(maxRepositories: 100) {
                repository {
                  nameWithOwner
                  owner {
                    login
                  }
                  primaryLanguage {
                    name
                    color
                  }
                }
                contributions {
                  totalCount
                }
              }
            }
            repositories(
              first: 100
              affiliations: [OWNER]
              orderBy: {field: UPDATED_AT, direction: DESC}
              isFork: false
            ) {
              nodes {
                name
                nameWithOwner
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

            top_langs = self._aggregate_github_languages(viewer)

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


    def fetch_public_holidays(self, year=None, country_code='CN'):
        if country_code != 'CN':
            return []
        return self.calendar.get(year or time.localtime().tm_year)['publicHolidays']

    def calendar_holidays(self):
        country = request.args.get('country', 'CN')
        try:
            year = int(request.args.get('year') or time.localtime().tm_year)
            if country != 'CN':
                return u.format_dict({'success': False, 'message': '仅支持中国节假日'}), 400
            result = self.calendar.get(year)
        except ValueError:
            return u.format_dict({'success': False, 'message': '节假日年份超出支持范围'}), 400
        self.runtime.d.load()
        local_holidays = [e for e in self.runtime.d.data.get('calendar_events', [])
                          if e.get('type') == 'holiday' and e.get('date', '').startswith(f'{year}-')]
        return u.format_dict(dict(success=True, year=year, country=country,
                                  customHolidays=local_holidays, **result))


    def github_stats(self):
        """获取 GitHub 贡献热力图和技术栈"""
        result = self.fetch_github_contributions()
        if 'error' in result:
            return u.format_dict({'success': False, 'error': result['error']})

        result['success'] = True
        return u.format_dict(result)

