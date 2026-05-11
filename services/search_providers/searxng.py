"""SearXNG self-hosted search provider."""

import httpx
from typing import List, Dict, Any, Optional

from services.search_providers.base import BaseSearchProvider, SearchResult


class SearxngProvider(BaseSearchProvider):
    """SearXNG self-hosted meta-search engine.
    
    No API key required if instance is public/self-hosted.
    Best for: privacy, aggregating multiple engines, self-hosting.
    """

    provider_id = "searxng"
    display_name = "SearXNG (自托管)"
    requires_api_key = False
    requires_api_base = True
    official_url = "https://docs.searxng.org"
    api_key_url = ""

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        api_base = self.config.get("api_base", "").rstrip("/")
        if not api_base:
            raise ValueError("SearXNG API base URL is not configured")

        url = f"{api_base}/search"
        params = {
            "q": query,
            "format": "json",
            "categories": "general",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        results: List[SearchResult] = []
        for item in data.get("results", [])[:max_results]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", "")[:500],
                published_date=item.get("publishedDate"),
            ))

        return results

    async def check(self) -> tuple[bool, Optional[str]]:
        api_base = self.config.get("api_base", "").rstrip("/")
        if not api_base:
            return False, "API base URL not configured"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{api_base}/search", params={"q": "test", "format": "json", "count": 1})
                resp.raise_for_status()
                data = resp.json()
                has_results = bool(data.get("results"))
                return has_results, None
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code}"
        except Exception as e:
            return False, str(e)
