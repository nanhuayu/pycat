"""DuckDuckGo search provider — zero API key, lightweight."""

from typing import List, Dict, Any, Optional

from services.search_providers.base import BaseSearchProvider, SearchResult


class DuckDuckGoProvider(BaseSearchProvider):
    """DuckDuckGo search via the ddgs library.
    
    No API key required. Uses DDG's internal lite/html endpoints.
    Best for: quick, zero-config searches with decent result quality.
    """

    provider_id = "duckduckgo"
    display_name = "DuckDuckGo"
    requires_api_key = False
    official_url = "https://duckduckgo.com"
    api_key_url = ""

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError:
            raise RuntimeError(
                "ddgs library is not installed. Run: pip install ddgs"
            )

        # DDGS is synchronous; run in thread to not block event loop
        import asyncio
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, self._search_sync, query, max_results
        )
        return results

    def _search_sync(self, query: str, max_results: int) -> List[SearchResult]:
        from ddgs import DDGS

        results: List[SearchResult] = []
        with DDGS() as ddgs:
            for r in ddgs.text(
                query,
                region="wt-wt",
                safesearch="moderate",
                backend="auto",
                max_results=max_results,
            ):
                results.append(SearchResult(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    content=r.get("body", "")[:500],
                ))
        return results

    async def check(self) -> tuple[bool, Optional[str]]:
        """Check by attempting a lightweight search."""
        try:
            from ddgs import DDGS
        except ImportError:
            return False, "ddgs library not installed. Run: pip install ddgs"

        try:
            results = await self.search("test", max_results=1)
            return len(results) > 0, None
        except Exception as e:
            return False, str(e)
