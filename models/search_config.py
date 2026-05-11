"""Search configuration model

Refactored to support multiple provider types with flexible config fields.
Providers: tavily, duckduckgo, brave, searxng
Removed: bing, google (unstable or high-friction setup)
"""
from dataclasses import dataclass, field
from typing import Dict, Any

# Valid provider IDs
SEARCH_PROVIDERS = ["tavily", "duckduckgo", "brave", "searxng"]
# Default provider — duckduckgo requires zero setup
DEFAULT_PROVIDER = "duckduckgo"


@dataclass
class SearchConfig:
    """Configuration for web search providers.
    
    Provider-specific fields:
    - tavily: api_key
    - duckduckgo: no config needed
    - brave: api_key
    - searxng: api_base
    """
    enabled: bool = False
    provider: str = DEFAULT_PROVIDER
    api_key: str = ""           # Tavily, Brave
    api_base: str = ""          # SearXNG base URL
    max_results: int = 5
    include_date: bool = True   # Include date in search results

    # Legacy migration: google_cx is no longer used but kept for loading old configs
    google_cx: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "max_results": self.max_results,
            "include_date": self.include_date,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SearchConfig":
        provider = data.get("provider", DEFAULT_PROVIDER)
        # Migrate removed providers to duckduckgo (zero-config fallback)
        if provider in ("bing", "google"):
            provider = DEFAULT_PROVIDER

        return cls(
            enabled=data.get("enabled", False),
            provider=provider if provider in SEARCH_PROVIDERS else DEFAULT_PROVIDER,
            api_key=data.get("api_key", ""),
            api_base=data.get("api_base", ""),
            max_results=data.get("max_results", 5),
            include_date=data.get("include_date", True),
            google_cx=data.get("google_cx", ""),
        )

    def get_provider_config(self) -> Dict[str, Any]:
        """Return config dict for the current provider."""
        return {
            "api_key": self.api_key,
            "api_base": self.api_base,
        }
