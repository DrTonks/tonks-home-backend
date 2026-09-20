"""Extracted compatibility-preserving domain functions; no startup side effects."""

import hashlib
import re
def community_avatar_svg(email):
    """Create a deterministic local fallback avatar without exposing the email."""
    digest = hashlib.sha256(str(email or '').strip().casefold().encode('utf-8')).hexdigest()
    color_a = f'#{digest[0:6]}'
    color_b = f'#{digest[6:12]}'
    color_c = f'#{digest[12:18]}'
    x = 18 + int(digest[18:20], 16) % 60
    y = 18 + int(digest[20:22], 16) % 60
    radius = 12 + int(digest[22:24], 16) % 18
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">'
        f'<rect width="96" height="96" rx="22" fill="{color_a}"/>'
        f'<path d="M0 72 Q30 42 58 66 T96 42 V96 H0Z" fill="{color_b}" opacity=".82"/>'
        f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{color_c}" opacity=".9"/>'
        '<path d="M14 18h22M60 78h22" stroke="white" stroke-opacity=".52" stroke-width="4" stroke-linecap="round"/>'
        '</svg>'
    )
    return svg

def community_qq_number(email):
    """Return a QQ number only for numeric QQ-family mailbox addresses."""
    normalized = str(email or '').strip().casefold()
    match = re.fullmatch(
        r'(?P<uin>[0-9]{5,12})@(?:qq\.com|foxmail\.com|vip\.qq\.com)',
        normalized,
    )
    return match.group('uin') if match else None

def community_gravatar_url(email):
    """Build a Gravatar URL from the private, normalized email value."""
    normalized = str(email or '').strip().casefold()
    email_hash = hashlib.md5(normalized.encode('utf-8')).hexdigest()
    return f'https://www.gravatar.com/avatar/{email_hash}?s=96&d=404'

def community_qq_avatar_url(qq_number):
    """Build the primary QQ avatar URL from a validated numeric UIN."""
    return f'https://q1.qlogo.cn/g?b=qq&nk={qq_number}&s=640'
