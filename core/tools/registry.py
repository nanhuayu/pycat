from typing import Dict, List, Any, Optional, Set
from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.permissions import ToolPermissionResolver

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._permission_resolver = ToolPermissionResolver()

    def register(self, tool: BaseTool):
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def unregister_prefix(self, prefix: str) -> None:
        if not prefix:
            return
        for name in [tool_name for tool_name in self._tools.keys() if tool_name.startswith(prefix)]:
            self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def get_all_tool_schemas(
        self,
        *,
        allowed_groups: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get OpenAI-compatible schemas, optionally filtered by group.

        Parameters
        ----------
        allowed_groups
            If provided, only return tools whose ``group`` is in this set.
            ``None`` means return all tools (no filtering).
        """
        schemas: List[Dict[str, Any]] = []
        for tool in self._tools.values():
            if allowed_groups is not None and tool.group not in allowed_groups:
                continue
            schemas.append(tool.to_openai_tool())
        return schemas

    def update_permissions(self, config: Dict[str, Any]):
        """Update permission settings."""
        self._permission_resolver.update(config)

    async def execute(self, tool_name: str, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute a tool with permission checking."""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(f"Tool '{tool_name}' not found", is_error=True)

        wrapped_context = self._permission_resolver.wrap_context(context, tool)
        
        try:
            result = await tool.execute(arguments, wrapped_context)
            # Apply output truncation
            if isinstance(result.content, str):
                result.content = tool.truncate_output(result.content)
            return result
        except Exception as e:
            return ToolResult(f"Tool execution error: {str(e)}", is_error=True)
