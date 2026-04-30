from core.tools.base import BaseTool, ToolContext, ToolResult
from typing import Dict, Any, Optional

class WebSearchTool(BaseTool):
    def __init__(self, search_service, prepared_queries=None):
        self.search_service = search_service
        self.prepared_queries = prepared_queries or []

    @property
    def name(self) -> str:
        return "builtin_web_search"

    @property
    def description(self) -> str:
        base_desc = "Web search tool for finding current information, news, and real-time data from the internet."
        if self.prepared_queries:
             base_desc += f"\n\nPrepared queries: {', '.join(self.prepared_queries)}"
        return base_desc

    @property
    def category(self) -> str:
        return "read"

    @property
    def group(self) -> str:
        return "search"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        query = arguments.get("query", "")
        if not query:
            # Fallback to prepared queries if user didn't provide one?
            # Or assume user provided additionalContext in old schema.
            # Let's support 'additionalContext' too if that's what SearchService used.
            # But SearchService.get_tool_schema defined "additionalContext".
            # The actual search implementation takes 'query'.
            # If the schema says "additionalContext", the model sends "additionalContext".
            # We should probably align schema.
            # SearchService implementation seems to take `query` in `search()`.
            # But `get_tool_schema` defines `additionalContext`.
            # This implies the prompt/code handles the merging of prepared queries + additionalContext.
            
            # Let's stick to simple "query" for this Tool wrapper for now, 
            # unless we want to exactly match what SearchService did.
            # SearchService seemed to return a schema that implies the *backend* would use prepared_queries + additionalContext.
            pass

        # Use the query provided
        result = await self.search_service.search(query)
        return ToolResult(result)
