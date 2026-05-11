"""Tavily AI search provider."""

import httpx
from typing import List, Dict, Any

from services.search_providers.base import BaseSearchProvider, SearchResult


class TavilyProvider(BaseSearchProvider):
    """Tavily AI search — mature, AI-optimized search with summaries."""

    provider_id = "tavily"
    display_name = "Tavily AI"
    requires_api_key = True
    official_url = "https://tavily.com"
    api_key_url = "https://app.tavily.com/home"

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        api_key = self.config.get("api_key", "")
        if not api_key:
            raise ValueError("Tavily API key is not configured")

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
            "include_raw_content": False,
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return self._parse_results(data, max_results)

    def _parse_results(self, data: Dict[str, Any], max_results: int) -> List[SearchResult]:
        results: List[SearchResult] = []

        # Tavily sometimes returns an AI-generated answer at the top
        answer = data.get("answer")
        if answer:
            results.append(SearchResult(
                title="AI Summary",
                url="",
                content=answer,
            ))

        for item in data.get("results", [])[:max_results]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", "")[:500],
                published_date=item.get("published_date"),
            ))

        return results

    def format_for_llm(self, results: List[SearchResult], include_date: bool = True) -> str:
        """Tavily-specific formatting that highlights the AI summary."""
        if not results:
            return "No results found"

        lines = []
        # If first result is AI Summary, format it specially
        if results and results[0].title == "AI Summary":
            lines.append(f"**Summary**: {results[0].content}\n")
            results = results[1:]

        if results:
            lines.append("**Search Results:**\n")
            for i, r in enumerate(results, 1):
                suffix = f" - {r.published_date}" if include_date and r.published_date else ""
                lines.append(f"{i}. [{r.title}]({r.url}){suffix}")
                lines.append(f"   {r.content}...\n")

        return "\n".join(lines) if lines else "No results found"
