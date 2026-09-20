"""Blog owns image files; this module only constructs validated public URLs."""
from urllib.parse import quote, unquote, urlsplit


def blog_image_url(value, base_url):
    if not isinstance(value, str) or not value:
        return None
    try:
        base = urlsplit(base_url)
        source = urlsplit(value)
    except (ValueError, TypeError):
        return None
    if base.scheme not in ('http', 'https') or not base.netloc or base.username or base.password:
        return None
    if source.netloc or source.scheme:
        if source.scheme != base.scheme or source.netloc != base.netloc:
            return None
    if source.query or source.fragment or value.startswith('//'):
        return None
    path = unquote(source.path)
    if '\\' in path or '%' in path or any(ord(c) < 32 for c in path):
        return None
    if not path.startswith('/images/') or any(p in ('', '.', '..') for p in path[1:].split('/')):
        return None
    # Origin is trusted configuration, never supplied by an image request.
    return f'{base.scheme}://{base.netloc}' + quote(path, safe='/')


def normalize_blog_images(entry, base_url):
    """Keep public API shapes; callers manage only the original image field."""
    result = dict(entry)
    raw = entry.get('image')
    values = raw if isinstance(raw, list) else [raw]
    images = [url for value in values if (url := blog_image_url(value, base_url))]
    result['images'] = images
    result['image'] = images if isinstance(raw, list) else (images[0] if images else None)
    return result
