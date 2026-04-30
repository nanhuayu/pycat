"""
Search configuration model
"""
from dataclasses import dataclass, field
from typing import Dict, Any

@dataclass
class SearchConfig:
    """Configuration for web search providers"""
    enabled: bool = False
    provider: str = "tavily"  # tavily, google, bing, searxng
    api_key: str = ""
    api_base: str = ""  # For self-hosted (e.g., SearXNG)
    max_results: int = 5
    include_date: bool = True  # Include date in search results
    
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
        return cls(
            enabled=data.get("enabled", False),
            provider=data.get("provider", "tavily"),
            api_key=data.get("api_key", ""),
            api_base=data.get("api_base", ""),
            max_results=data.get("max_results", 5),
            include_date=data.get("include_date", True),
        )
