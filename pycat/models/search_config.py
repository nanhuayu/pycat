"""Search configuration; public engines and optional API providers."""
from dataclasses import dataclass, field
from typing import Dict, Any

# Valid provider IDs
SEARCH_PROVIDERS = ["tavily", "duckduckgo", "bing", "google", "brave", "searxng"]
# Default provider — duckduckgo requires zero setup
DEFAULT_PROVIDER = "duckduckgo"
DEFAULT_MAX_RESULTS = 8
MIN_MAX_RESULTS = 1
MAX_MAX_RESULTS = 20


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
    max_results: int = DEFAULT_MAX_RESULTS
    include_date: bool = True   # Include date in search results

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
        return cls(
            enabled=data.get("enabled", False),
            provider=provider if provider in SEARCH_PROVIDERS else DEFAULT_PROVIDER,
            api_key=data.get("api_key", ""),
            api_base=data.get("api_base", ""),
            max_results=data.get("max_results", DEFAULT_MAX_RESULTS),
            include_date=data.get("include_date", True),
        )

    def get_provider_config(self) -> Dict[str, Any]:
        """Return config dict for the current provider."""
        return {
            "api_key": self.api_key,
            "api_base": self.api_base,
        }
