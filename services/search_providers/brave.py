"""Brave Search API provider."""

import httpx
from typing import List, Dict, Any, Optional

from services.search_providers.base import BaseSearchProvider, SearchResult


class BraveProvider(BaseSearchProvider):
    """Brave Search API — privacy-focused search with free tier (2000 queries/month).
    
    Docs: https://api.search.brave.com/app/documentation
    """

    provider_id = "brave"
    display_name = "Brave Search"
    requires_api_key = True
    official_url = "https://search.brave.com"
    api_key_url = "https://api.search.brave.com/app/keys"

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        api_key = self.config.get("api_key", "")
        if not api_key:
            raise ValueError("Brave Search API key is not configured")

        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        params = {
            "q": query,
            "count": min(max_results, 20),
            "offset": 0,
            "mkt": "zh-CN",
            "safesearch": "off",
            "text_decorations": False,
            "spellcheck": True,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        return self._parse_results(data, max_results)

    def _parse_results(self, data: Dict[str, Any], max_results: int) -> List[SearchResult]:
        results: List[SearchResult] = []
        web_results = data.get("web", {}).get("results", [])

        for item in web_results[:max_results]:
            date = item.get("age") or item.get("page_age")
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("description", "")[:500],
                published_date=date,
            ))

        return results

    async def check(self) -> tuple[bool, Optional[str]]:
        """Check API key validity with a lightweight query."""
        api_key = self.config.get("api_key", "")
        if not api_key:
            return False, "API key not configured"

        try:
            url = "https://api.search.brave.com/res/v1/web/search"
            headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
            params = {"q": "test", "count": 1}

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 401:
                    return False, "Invalid API key"
                resp.raise_for_status()
                data = resp.json()
                has_results = bool(data.get("web", {}).get("results"))
                return has_results, None
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            return False, str(e)
