"""Web Search Service

Refactored to use Provider pattern (inspired by Cherry Studio).
Providers: tavily, duckduckgo, brave, searxng
Removed: bing, google (high friction / unstable)
"""
from typing import List, Dict, Any, Optional
import re

from models.search_config import SearchConfig, SEARCH_PROVIDERS
from services.search_providers.factory import SearchProviderFactory
from services.search_providers.base import BaseSearchProvider


class SearchService:
    """Unified web search service using Provider factory pattern."""

    DEFAULT_MAX_RESULTS = 5
    MIN_MAX_RESULTS = 1
    MAX_MAX_RESULTS = 20

    def __init__(self, config: Optional[SearchConfig] = None):
        self.config = config or SearchConfig()
        self._provider: Optional[BaseSearchProvider] = None
        self._refresh_provider()

    def _refresh_provider(self):
        """Create provider instance from current config."""
        self._provider = SearchProviderFactory.create(
            self.config.provider,
            self.config.get_provider_config()
        )

    def update_config(self, config: SearchConfig):
        self.config = config
        self._refresh_provider()

    def is_available(self) -> bool:
        """Check if search is properly configured and provider is valid."""
        if not self.config.enabled:
            return False
        if not self._provider:
            return False

        # Provider-specific validation
        if self.config.provider == "searxng":
            return bool(self.config.api_base)
        if self.config.provider == "duckduckgo":
            return True  # Zero config
        # tavily, brave: require api_key
        return bool(self.config.api_key)

    def get_tool_schema(self, prepared_queries: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Return OpenAI-compatible tool schema for web search.

        Tool contract:
        - tool name: web_search
        - argument: additional_context (optional)
        - prepared queries are embedded in description for the model to reference
        """
        if not self.is_available():
            return None

        prepared_queries = [q.strip() for q in (prepared_queries or []) if isinstance(q, str) and q.strip()]
        prepared_hint = ""
        if prepared_queries:
            prepared_hint = "\n\nThis tool has been configured with search parameters based on the conversation context:\n- Prepared queries: \"" + "\", \"".join(prepared_queries) + "\""

        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Web search tool for finding current information, news, and real-time data from the internet. Use concise keyword queries; if results are irrelevant or empty, retry with a narrower query, source names, dates, or English/Chinese variants rather than repeating the same query." + prepared_hint,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Primary search query. Prefer 3-12 precise keywords, include dates/source names when useful."
                        },
                        "additional_context": {
                            "type": "string",
                            "description": "Optional query/context. Used when query is omitted."
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of search results to return. Default: 5. Allowed range: 1-20."
                        }
                    },
                    "additionalProperties": False
                }
            }
        }

    @staticmethod
    def normalize_query(query: str) -> str:
        """Normalize user/model search text into a bounded provider query."""
        text = str(query or "").strip()
        text = re.sub(r"\s+", " ", text)
        return text[:500]

    def _resolve_max_results(self, value: Any = None) -> int:
        """Resolve per-call result count override with safe bounds."""
        if value is None or value == "":
            value = getattr(self.config, "max_results", self.DEFAULT_MAX_RESULTS)
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            resolved = getattr(self.config, "max_results", self.DEFAULT_MAX_RESULTS)
        return max(self.MIN_MAX_RESULTS, min(int(resolved), self.MAX_MAX_RESULTS))

    async def search(self, query: str, max_results: Optional[int] = None) -> str:
        """Execute search and return formatted results."""
        if not self.is_available():
            return "Search is not configured or the selected provider is not available."

        if self._provider is None:
            return f"Unknown search provider: {self.config.provider}"

        query = self.normalize_query(query)
        if not query:
            return "No search query provided."

        try:
            limit = self._resolve_max_results(max_results)
            results = await self._provider.search(query, max_results=limit)
            if not results:
                return "No results found"

            # Use provider-specific formatting if available (e.g., Tavily AI summary)
            if hasattr(self._provider, "format_for_llm"):
                return self._provider.format_for_llm(results, include_date=self.config.include_date)

            return self._provider._format_results(results, include_date=self.config.include_date)
        except Exception as e:
            return f"Search error: {str(e)}"

    async def check(self) -> tuple[bool, Optional[str]]:
        """Check if the current provider is properly configured and reachable.

        Returns (is_valid, error_message).
        """
        if not self.config.enabled:
            return False, "Search is disabled"
        if self._provider is None:
            return False, f"Unknown provider: {self.config.provider}"
        return await self._provider.check()

    @staticmethod
    def list_providers() -> List[Dict[str, Any]]:
        """Return metadata of all available providers for UI."""
        return SearchProviderFactory.list_providers()
