"""Fail-closed browser navigation and enrolled-origin boundaries."""
from urllib.parse import urlsplit

PLATFORM_DOMAINS = {'upwork': 'upwork.com', 'fiverr': 'fiverr.com',
                    'guru': 'guru.com', 'peopleperhour': 'peopleperhour.com'}


def validate_platform_url(platform: str, url: str) -> str:
    domain = PLATFORM_DOMAINS.get(platform)
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        valid = (domain and parsed.scheme == 'https' and not parsed.username
                 and not parsed.password and parsed.port in (None, 443)
                 and (host == domain or host.endswith('.' + domain)))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f'URL is outside the approved {platform} website')
    return url


def validate_storage_state(platform: str, state: dict):
    if not isinstance(state, dict) or not isinstance(state.get('cookies', []), list) or not isinstance(state.get('origins', []), list):
        raise ValueError('invalid browser storage state')
    for entry in state.get('origins', []):
        validate_platform_url(platform, entry.get('origin', ''))
    for cookie in state.get('cookies', []):
        validate_platform_url(platform, 'https://' + cookie.get('domain', '').lstrip('.'))
