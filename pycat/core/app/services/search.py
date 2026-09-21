"""One configured search engine, with explicit failures and bounded results."""
from typing import List, Dict, Any, Optional
import re

from pycat.models.search_config import (
    DEFAULT_MAX_RESULTS,
    MAX_MAX_RESULTS,
    MIN_MAX_RESULTS,
    SearchConfig,
)
from pycat.core.app.services.search_providers.factory import SearchProviderFactory
from pycat.core.app.services.search_providers.base import BaseSearchProvider


class SearchService:
    """Unified web search service using Provider factory pattern."""

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

        return (
            (not self._provider.requires_api_base or bool(self.config.api_base))
            and (not self._provider.requires_api_key or bool(self.config.api_key))
        )

    @staticmethod
    def normalize_query(query: str) -> str:
        """Normalize user/model search text into a bounded provider query."""
        text = str(query or "").strip()
        text = re.sub(r"\s+", " ", text)
        return text[:500]

    def _resolve_max_results(self) -> int:
        """Resolve the configured result count with safe bounds."""
        value = getattr(self.config, "max_results", DEFAULT_MAX_RESULTS)
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            resolved = DEFAULT_MAX_RESULTS
        return max(MIN_MAX_RESULTS, min(int(resolved), MAX_MAX_RESULTS))

    async def search(self, query: str) -> str:
        """Execute search and return formatted results."""
        if not self.is_available():
            return "Search is not configured or the selected provider is not available."

        if self._provider is None:
            return f"Unknown search provider: {self.config.provider}"

        query = self.normalize_query(query)
        if not query:
            return "No search query provided."

        try:
            limit = self._resolve_max_results()
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
