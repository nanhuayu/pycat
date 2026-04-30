"""
Web Search Service
Provides unified interface for multiple search providers (Tavily, Google, etc.)
"""
import httpx
from typing import List, Dict, Any, Optional
from datetime import datetime

from models.search_config import SearchConfig


class SearchService:
    """Unified web search service"""
    
    PROVIDERS = {
        "tavily": "Tavily AI",
        "google": "Google (SerpAPI)",
        "searxng": "SearXNG (Self-hosted)",
    }
    
    def __init__(self, config: Optional[SearchConfig] = None):
        self.config = config or SearchConfig()
    
    def update_config(self, config: SearchConfig):
        self.config = config
    
    def is_available(self) -> bool:
        """Check if search is properly configured"""
        if not self.config.enabled:
            return False
        if self.config.provider == "searxng":
            return bool(self.config.api_base)
        return bool(self.config.api_key)
    
    def get_tool_schema(self, prepared_queries: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Return OpenAI-compatible tool schema for web search.

        Cherry Studio style:
        - tool name: builtin_web_search
        - argument: additionalContext (optional)
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
                "name": "builtin_web_search",
                "description": "Web search tool for finding current information, news, and real-time data from the internet." + prepared_hint,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "additionalContext": {
                            "type": "string",
                            "description": "Optional additional context, keywords, or specific focus to enhance or replace the search terms"
                        }
                    },
                    "additionalProperties": False
                }
            }
        }
    
    async def search(self, query: str) -> str:
        """Execute search and return formatted results"""
        if not self.is_available():
            return "Search is not configured"
        
        try:
            if self.config.provider == "tavily":
                return await self._search_tavily(query)
            elif self.config.provider == "google":
                return await self._search_google(query)
            elif self.config.provider == "searxng":
                return await self._search_searxng(query)
            else:
                return f"Unknown search provider: {self.config.provider}"
        except Exception as e:
            return f"Search error: {str(e)}"
    
    async def _search_tavily(self, query: str) -> str:
        """Search using Tavily API"""
        url = "https://api.tavily.com/search"
        
        payload = {
            "api_key": self.config.api_key,
            "query": query,
            "max_results": self.config.max_results,
            "include_answer": True,
            "include_raw_content": False,
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        
        return self._format_tavily_results(data)
    
    def _format_tavily_results(self, data: Dict) -> str:
        """Format Tavily response into readable text"""
        lines = []
        
        # Include AI answer if available
        if data.get("answer"):
            lines.append(f"**Summary**: {data['answer']}\n")
        
        # Include search results
        results = data.get("results", [])
        if results:
            lines.append("**Search Results:**\n")
            for i, r in enumerate(results[:self.config.max_results], 1):
                title = r.get("title", "")
                url = r.get("url", "")
                content = r.get("content", "")[:300]
                if self.config.include_date and r.get("published_date"):
                    lines.append(f"{i}. [{title}]({url}) - {r['published_date']}")
                else:
                    lines.append(f"{i}. [{title}]({url})")
                lines.append(f"   {content}...\n")
        
        return "\n".join(lines) if lines else "No results found"
    
    async def _search_google(self, query: str) -> str:
        """Search using Google (via SerpAPI)"""
        url = "https://serpapi.com/search"
        
        params = {
            "api_key": self.config.api_key,
            "q": query,
            "num": self.config.max_results,
            "engine": "google",
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        
        return self._format_google_results(data)
    
    def _format_google_results(self, data: Dict) -> str:
        """Format Google/SerpAPI response"""
        lines = []
        
        # Knowledge graph
        if data.get("knowledge_graph"):
            kg = data["knowledge_graph"]
            if kg.get("description"):
                lines.append(f"**{kg.get('title', 'Info')}**: {kg['description']}\n")
        
        # Organic results
        results = data.get("organic_results", [])
        if results:
            lines.append("**Search Results:**\n")
            for i, r in enumerate(results[:self.config.max_results], 1):
                title = r.get("title", "")
                link = r.get("link", "")
                snippet = r.get("snippet", "")
                lines.append(f"{i}. [{title}]({link})")
                lines.append(f"   {snippet}\n")
        
        return "\n".join(lines) if lines else "No results found"
    
    async def _search_searxng(self, query: str) -> str:
        """Search using self-hosted SearXNG"""
        base = self.config.api_base.rstrip("/")
        url = f"{base}/search"
        
        params = {
            "q": query,
            "format": "json",
            "categories": "general",
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        
        lines = ["**Search Results:**\n"]
        for i, r in enumerate(data.get("results", [])[:self.config.max_results], 1):
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")[:300]
            lines.append(f"{i}. [{title}]({url})")
            lines.append(f"   {content}\n")
        
        return "\n".join(lines) if lines else "No results found"
