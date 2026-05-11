"""Web search providers package."""

from services.search_providers.base import BaseSearchProvider, SearchResult
from services.search_providers.factory import SearchProviderFactory

__all__ = ["BaseSearchProvider", "SearchResult", "SearchProviderFactory"]
