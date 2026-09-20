"""blog/service business operations and HTTP handlers. State is application-scoped."""
import os
from sleepy_app.blog.slugs import normalize_blog_slug
from sleepy_app.blog.images import blog_image_url, normalize_blog_images
from sleepy_app.common.environment import configured_value
import sleepy_app.common.responses as u
from flask import request
import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import urllib.parse
import hashlib

class BlogService:
    def __init__(self, runtime):
        self.runtime = runtime

    def fetch_blog_rss(self, count=2):
        """获取最新博客文章，先尝试 Atom 再尝试 RSS 2.0"""
        blog_base_url = os.environ.get(
            'SLEEPY_BLOG_BASE_URL', self.runtime.d.data.get('blog_base_url', 'https://blog.tonks.top')
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
                            'summary': summary,
                            'category': next((el.get('term', '').strip() for el in atom_findall(entry, 'category') if el.get('term', '').strip()), '')
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
                            'summary': desc[:500] if desc else '',
                            'category': (item.findtext('category') or '').strip()
                        })
        except Exception as e:
            u.error(f'RSS feed fetch failed: {e}')

        return posts[:count]


    def _blog_image_path(self, blog_path):
        """Return the blog URL without local copies or per-image network requests."""
        return blog_image_url(blog_path, self._blog_image_base())


    def _blog_image_base(self):
        return os.environ.get('SLEEPY_BLOG_BASE_URL', self.runtime.d.data.get('blog_base_url', 'https://blog.tonks.top'))


    def _extract_links(self, entry, entry_type='project'):
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


    def _extract_images(self, entry):
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
            url = self._blog_image_path(p)
            if url:
                result.append(url)
        return result


    def fetch_blog_extra(self):
        """获取博客的额外数据：按时间倒序取最新的项目和时光机条目"""
        blog_data_url = os.environ.get(
            'SLEEPY_BLOG_DATA_URL', self.runtime.d.data.get('blog_data_url', 'https://blog.tonks.top/data')
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
                    p = normalize_blog_images(projects[0], self._blog_image_base())
                    p['links'] = self._extract_links(p, 'project')
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
                    t = normalize_blog_images(timeline[0], self._blog_image_base())
                    t['links'] = self._extract_links(t, 'timeline')
                    result['featuredTimeline'] = t
        except Exception as e:
            u.error(f'Blog timeline fetch failed: {e}')

        return result


    def enrich_blog_post_stats(self, posts):
        """Use the same local counters as the blog, without per-article HTTP requests."""
        enriched = [{**post, 'category': post.get('category', ''),
                     'stats': dict.fromkeys(('views', 'likes', 'comments'), None)} for post in posts]
        by_slug = {}
        for post in enriched:
            path = urllib.parse.unquote(urllib.parse.urlsplit(post.get('link', '')).path)
            if path.startswith('/posts/'):
                slug = normalize_blog_slug(path[len('/posts/'):])
                if slug:
                    by_slug.setdefault(slug, []).append(post)
        if not by_slug:
            return enriched
        readers = {
            'views': lambda: self.runtime.blog_analytics.get_views(by_slug),
            'likes': lambda: {key[5:]: value['count'] for key, value in
                              self.runtime.community_store.get_likes([f'post:{slug}' for slug in by_slug], '').items()},
            'comments': lambda: self.runtime.app.extensions['article_comments'].get_public_totals_by_slug(by_slug),
        }
        for metric, read in readers.items():
            try:
                totals = read()
                for slug, items in by_slug.items():
                    for post in items:
                        post['stats'][metric] = totals.get(slug)
            except Exception as exc:
                u.error(f'Blog {metric} totals unavailable: {exc}')
        return enriched


    def blog_posts(self):
        """获取最新博客文章"""
        count_str = request.args.get('count', '2')
        try:
            count = int(count_str)
        except ValueError:
            count = 2
        count = max(1, min(count, 20))  # 限制 1-20

        posts = self.enrich_blog_post_stats(self.fetch_blog_rss(count=count))
        extra = self.fetch_blog_extra()
        return u.format_dict({
            'success': True,
            'count': len(posts),
            'posts': posts,
            'featuredProject': extra['featuredProject'],
            'featuredTimeline': extra['featuredTimeline']
        })


    def blog_views(self):
        """Batch-read view totals for article slugs."""
        raw_slugs = request.args.getlist('slugs')
        if len(raw_slugs) == 1 and ',' in raw_slugs[0]:
            raw_slugs = raw_slugs[0].split(',')
        if len(raw_slugs) > 100:
            return self.runtime.common_security.reterr(code='bad request', message='at most 100 slugs are allowed')

        slugs = []
        for raw_slug in raw_slugs:
            slug = normalize_blog_slug(raw_slug)
            if slug is None:
                return self.runtime.common_security.reterr(code='bad request', message=f'invalid article slug: {raw_slug}')
            slugs.append(slug)

        response = u.format_dict({
            'success': True,
            'views': self.runtime.blog_analytics.get_views(slugs),
        })
        response.headers['Cache-Control'] = 'public, max-age=60'
        return response


    def record_blog_view(self, slug):
        """Count at most one view per anonymous visitor and 30-minute bucket."""
        normalized_slug = normalize_blog_slug(slug)
        if normalized_slug is None:
            return self.runtime.common_security.reterr(code='bad request', message='invalid article slug')

        views, counted = self.runtime.blog_analytics.record_view(
            normalized_slug,
            self.runtime.common_security.get_blog_visitor_hash(request),
        )
        response = u.format_dict({
            'success': True,
            'slug': normalized_slug,
            'views': views,
            'counted': counted,
        })
        response.headers['Cache-Control'] = 'no-store'
        return response


    def blog_site_visits(self):
        """Read or record entries shared by the blog and personal homepage."""
        if request.method == 'GET':
            response = u.format_dict({
                'success': True,
                'visits': self.runtime.blog_analytics.get_site_visits(),
            })
            response.headers['Cache-Control'] = 'public, max-age=30'
            return response

        visit_id = str(request.headers.get('X-Visit-ID') or '').strip()
        source = str(request.headers.get('X-Site-Source') or '').strip().lower()
        if not self.runtime.SITE_VISIT_ID_RE.fullmatch(visit_id):
            return self.runtime.common_security.reterr(code='bad request', message='invalid site visit id')
        if source not in {'blog', 'home'}:
            return self.runtime.common_security.reterr(code='bad request', message='invalid site source')

        salt = os.environ.get('SLEEPY_ANALYTICS_SALT') or str(
            configured_value(self.runtime.d, 'SLEEPY_ADMIN_SECRET', 'admin_secret', '')
        )
        visit_hash = hashlib.sha256(
            f'{salt}|site-visit|{source}|{visit_id}'.encode('utf-8')
        ).hexdigest()
        visits, counted = self.runtime.blog_analytics.record_site_visit(visit_hash, source)
        response = u.format_dict({
            'success': True,
            'visits': visits,
            'counted': counted,
        })
        response.headers['Cache-Control'] = 'no-store'
        return response

