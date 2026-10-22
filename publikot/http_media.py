"""HTTPS object-storage transport, isolated from API credentials."""
import ipaddress
import os
import re
from urllib.parse import urlsplit

from aiohttp.resolver import DefaultResolver


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        proxies = {urlsplit(os.getenv(name, '')).hostname for name in
                   ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')}
        if host not in proxies and any(not ipaddress.ip_address(r['host']).is_global for r in records):
            raise OSError('Non-public media host')
        return records


def public_https_url(value):
    """API-supplied storage hosts may change; do not pin them to one cloud vendor."""
    if not isinstance(value, str) or not value or len(value) > 16384:
        return False
    if '\\' in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return False
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username is not None
                or url.password is not None or url.port not in (None, 443) or url.fragment):
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            host = url.hostname.rstrip('.').lower()
            return ('.' in host and not host.endswith(('.localhost', '.local', '.internal'))
                    and bool(re.fullmatch(r'[a-z0-9.-]+', host))
                    and not re.fullmatch(r'[0-9.]+', host))
    except ValueError:
        return False
