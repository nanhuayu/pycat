"""Bounded Bing RSS search using the existing HTTP client."""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from pycat.core.app.services.search_providers.base import BaseSearchProvider, SearchResult


class BingProvider(BaseSearchProvider):
    provider_id = 'bing'
    display_name = 'Bing'
    requires_api_key = False
    official_url = 'https://www.bing.com'
    MAX_BYTES = 1_000_000

    def __init__(self, config, *, transport=None):
        super().__init__(config)
        self._transport = transport

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raw = bytearray()
        async with httpx.AsyncClient(timeout=15, transport=self._transport, follow_redirects=True) as client:
            async with client.stream('GET', 'https://www.bing.com/search', params={'q': query, 'format': 'rss'}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self.MAX_BYTES:
                        raise RuntimeError('Bing RSS response exceeds 1 MB')
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise RuntimeError('Bing RSS contains an unsupported XML declaration')
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise RuntimeError('Bing did not return valid RSS; try again later or select another engine') from exc
        if root.tag != 'rss' or root.find('channel') is None:
            raise RuntimeError('Bing did not return RSS; try again later or select another engine')
        results = []
        seen = set()
        for item in root.findall('./channel/item'):
            url = (item.findtext('link') or '').strip()
            if urlparse(url).scheme not in {'https', 'http'} or not urlparse(url).netloc or url in seen:
                continue
            seen.add(url)
            description = html.unescape(re.sub('<[^>]+>', '', item.findtext('description') or ''))
            results.append(SearchResult((item.findtext('title') or '')[:300], url, description[:500], item.findtext('pubDate')))
            if len(results) >= max(1, min(int(max_results), 20)):
                break
        return results
