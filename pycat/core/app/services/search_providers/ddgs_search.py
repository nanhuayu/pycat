"""Explicit Google/DuckDuckGo routing through the existing ddgs dependency."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from pycat.core.app.services.search_providers.base import BaseSearchProvider, SearchError, SearchResult


class DuckDuckGoProvider(BaseSearchProvider):
    provider_id = 'duckduckgo'
    display_name = 'DuckDuckGo'
    requires_api_key = False
    official_url = 'https://duckduckgo.com'

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return await asyncio.to_thread(self._search_sync, query, max_results)

    def _search_sync(self, query, max_results):
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

        limit = max(1, min(int(max_results), 20))
        results = []
        seen = set()
        try:
            with DDGS(timeout=15) as search:
                for item in search.text(query, region='wt-wt', safesearch='moderate', backend=self.provider_id, max_results=limit):
                    url = str(item.get('href') or '')
                    if urlparse(url).scheme not in {'http', 'https'} or not urlparse(url).netloc or url in seen:
                        continue
                    seen.add(url)
                    results.append(SearchResult(str(item.get('title') or '')[:300], url, str(item.get('body') or '')[:500]))
                    if len(results) >= limit:
                        break
        except TimeoutException as exc:
            raise SearchError(f"{self.display_name} search timed out. Try again later.",
                              code="search_timeout", retryable=True) from exc
        except RatelimitException as exc:
            raise SearchError(f"{self.display_name} rate-limited the search. Wait before retrying.",
                              code="search_rate_limited", retryable=True) from exc
        except DDGSException as exc:
            if str(exc).strip().rstrip('.') == 'No results found':
                # DDGS also uses this exception when an HTTP challenge is discarded.
                # Its public API does not expose the response, so do not claim a
                # confirmed block or a confirmed empty search here.
                raise SearchError(
                    f"{self.display_name} returned no usable results. DDGS can report this for empty results "
                    "or a blocked/verification response; this is not evidence that no matching pages exist. "
                    "Check the provider connection or choose another configured provider. "
                    "Avoid repeating the same request.",
                    code="search_unavailable",
                ) from exc
            raise SearchError(f"{self.display_name} search failed: {exc}") from exc
        return results


class GoogleProvider(DuckDuckGoProvider):
    provider_id = 'google'
    display_name = 'Google'
    official_url = 'https://www.google.com'
