"""Extracted compatibility-preserving domain functions; no startup side effects."""

import os
def get_waitress_proxy_settings():
    """Build one fail-closed proxy trust policy for the production WSGI server."""
    trusted_proxy = os.environ.get('SLEEPY_TRUSTED_PROXY', '127.0.0.1').strip()
    settings = {
        'trusted_proxy': trusted_proxy or None,
        'trusted_proxy_headers': (
            {'x-forwarded-for', 'x-forwarded-proto'} if trusted_proxy else set()
        ),
        'clear_untrusted_proxy_headers': True,
    }
    if not trusted_proxy:
        return settings

    raw_count = os.environ.get('SLEEPY_TRUSTED_PROXY_COUNT', '1').strip()
    try:
        trusted_proxy_count = int(raw_count)
    except ValueError as exc:
        raise RuntimeError('SLEEPY_TRUSTED_PROXY_COUNT must be an integer') from exc
    if not 1 <= trusted_proxy_count <= 10:
        raise RuntimeError('SLEEPY_TRUSTED_PROXY_COUNT must be between 1 and 10')
    settings['trusted_proxy_count'] = trusted_proxy_count
    return settings
