from __future__ import annotations

import logging
from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.mcp.naming import build_mcp_tool_name

logger = logging.getLogger(__name__)


class McpProxyTool(BaseTool):
    """Delegate execution to an external MCP server through ToolManager."""

    def __init__(self, tool_manager: Any, config: Any, tool_name: str, schema: Dict[str, Any]):
        self.tool_manager = tool_manager
        self.config = config
        self.real_tool_name = tool_name
        self._schema = schema
        self._name = build_mcp_tool_name(config.name, tool_name)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._schema.get("description", "")

    @property
    def category(self) -> str:
        return "misc"

    @property
    def group(self) -> str:
        return "mcp"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return self._schema.get("parameters", {})

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            conversation_id = None
            try:
                if getattr(context, "conversation", None) is not None:
                    conversation_id = getattr(context.conversation, "id", None)
            except Exception as exc:
                logger.debug("Failed to resolve conversation id for MCP proxy %s: %s", self._name, exc)
                conversation_id = None

            result = await self.tool_manager.call_tool(
                self._name,
                arguments,
                work_dir=context.work_dir,
                conversation_id=conversation_id,
            )
            return ToolResult(str(result))
        except Exception as exc:
            logger.warning("MCP proxy execution failed for %s: %s", self._name, exc)
            return ToolResult(f"MCP Tool Error: {exc}", is_error=True)