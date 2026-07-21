"""Web search providers package."""

from core.app.services.search_providers.base import BaseSearchProvider, SearchResult
from core.app.services.search_providers.factory import SearchProviderFactory

__all__ = ["BaseSearchProvider", "SearchResult", "SearchProviderFactory"]
