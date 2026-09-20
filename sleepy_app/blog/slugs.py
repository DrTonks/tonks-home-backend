"""Extracted compatibility-preserving domain functions; no startup side effects."""

import re

BLOG_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
def normalize_blog_slug(value):
    """Validate and normalize the public Astro article slug."""
    slug = str(value or '').strip().strip('/')
    if (
        not slug
        or '..' in slug
        or '//' in slug
        or not BLOG_SLUG_RE.fullmatch(slug)
    ):
        return None
    return slug
