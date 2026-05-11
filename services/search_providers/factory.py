"""Search provider factory — creates provider instances by ID."""

from typing import Dict, Any, Optional, Type

from services.search_providers.base import BaseSearchProvider
from services.search_providers.tavily import TavilyProvider
from services.search_providers.duckduckgo import DuckDuckGoProvider
from services.search_providers.brave import BraveProvider
from services.search_providers.searxng import SearxngProvider


# Registry of all available providers
_PROVIDER_REGISTRY: Dict[str, Type[BaseSearchProvider]] = {
    TavilyProvider.provider_id: TavilyProvider,
    DuckDuckGoProvider.provider_id: DuckDuckGoProvider,
    BraveProvider.provider_id: BraveProvider,
    SearxngProvider.provider_id: SearxngProvider,
}

# Ordered list for UI display (most recommended first)
_PROVIDER_DISPLAY_ORDER = [
    DuckDuckGoProvider.provider_id,   # Zero config — most accessible
    TavilyProvider.provider_id,        # Best AI-optimized results
    BraveProvider.provider_id,         # Privacy-focused, free tier
    SearxngProvider.provider_id,       # Self-hosted
]


class SearchProviderFactory:
    """Factory for creating search provider instances.
    
    Cherry Studio pattern: provider ID → instance with config.
    """

    @classmethod
    def create(cls, provider_id: str, config: Dict[str, Any]) -> Optional[BaseSearchProvider]:
        """Create a provider instance by ID."""
        provider_cls = _PROVIDER_REGISTRY.get(provider_id)
        if provider_cls is None:
            return None
        return provider_cls(config)

    @classmethod
    def get_provider_class(cls, provider_id: str) -> Optional[Type[BaseSearchProvider]]:
        return _PROVIDER_REGISTRY.get(provider_id)

    @classmethod
    def list_providers(cls) -> list[dict]:
        """Return list of provider metadata for UI configuration.
        
        Each item contains: id, name, requires_api_key, requires_api_base, official_url, api_key_url
        """
        result = []
        for pid in _PROVIDER_DISPLAY_ORDER:
            pcls = _PROVIDER_REGISTRY.get(pid)
            if pcls:
                result.append({
                    "id": pcls.provider_id,
                    "name": pcls.display_name,
                    "requires_api_key": pcls.requires_api_key,
                    "requires_api_base": pcls.requires_api_base,
                    "official_url": pcls.official_url,
                    "api_key_url": pcls.api_key_url,
                })
        return result

    @classmethod
    def is_valid_provider(cls, provider_id: str) -> bool:
        return provider_id in _PROVIDER_REGISTRY
