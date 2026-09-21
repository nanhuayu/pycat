"""Base class for web search providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any, Optional


@dataclass
class SearchResult:
    """Normalized search result across all providers."""
    title: str
    url: str
    content: str
    published_date: Optional[str] = None


class BaseSearchProvider(ABC):
    """Abstract base class for search providers.
    
    Follows Cherry Studio's provider pattern:
    - Each provider encapsulates its own API/format logic
    - Returns normalized SearchResult list
    """

    provider_id: str = ""
    display_name: str = ""
    requires_api_key: bool = True
    requires_api_base: bool = False
    official_url: str = ""
    api_key_url: str = ""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """Execute search and return normalized results."""
        pass

    async def check(self) -> tuple[bool, Optional[str]]:
        """Check if provider is properly configured and reachable.
        
        Returns (is_valid, error_message).
        """
        try:
            results = await self.search("test", max_results=1)
            return True, None
        except Exception as e:
            return False, str(e)

    def _format_results(self, results: List[SearchResult], include_date: bool = True) -> str:
        """Format normalized results into markdown text for LLM consumption."""
        if not results:
            return "No results found"

        lines = ["**Search Results:**\n"]
        for i, r in enumerate(results, 1):
            suffix = f" - {r.published_date}" if include_date and r.published_date else ""
            lines.append(f"{i}. [{r.title}]({r.url}){suffix}")
            lines.append(f"   {r.content}\n")
        return "\n".join(lines)
