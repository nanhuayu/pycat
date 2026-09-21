import threading
from typing import Dict, List, Any, Iterable, Optional
from pycat.models.contracts.tooling import ToolPermissionConfig
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.models.contracts.tooling import ToolDescriptor, ToolSelectionPolicy

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._lock = threading.RLock()

    def register(self, tool: BaseTool):
        """Register a tool instance."""
        with self._lock:
            tools = dict(self._tools)
            tools[tool.name] = tool
            self._tools = tools

    def unregister(self, name: str) -> None:
        with self._lock:
            tools = dict(self._tools)
            tools.pop(name, None)
            self._tools = tools

    def unregister_prefix(self, prefix: str) -> None:
        if not prefix:
            return
        self.replace_prefix(prefix, ())

    def replace_prefix(self, prefix: str, tools: Iterable[BaseTool]) -> None:
        """Atomically replace every tool under one public-name prefix."""
        clean_prefix = str(prefix or "")
        if not clean_prefix:
            return
        replacements = {tool.name: tool for tool in tools}
        if any(not name.startswith(clean_prefix) for name in replacements):
            raise ValueError(f"replacement tool does not use prefix {clean_prefix!r}")
        with self._lock:
            updated = {
                name: tool
                for name, tool in self._tools.items()
                if not name.startswith(clean_prefix)
            }
            updated.update(replacements)
            self._tools = updated

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> List[BaseTool]:
        return list(self._tools.values())

    def list_descriptors(self, *, availability: Optional[Dict[str, bool]] = None) -> List[ToolDescriptor]:
        availability = availability or {}
        return [
            tool.descriptor(available=availability.get(tool.name, True))
            for tool in self._tools.values()
        ]

    def get_all_tool_schemas(
        self,
        *,
        tool_selection: Optional[ToolSelectionPolicy] = None,
        tool_permissions: Optional[ToolPermissionConfig] = None,
    ) -> List[Dict[str, Any]]:
        """Get OpenAI-compatible schemas with optional filtering.
        """
        schemas: List[Dict[str, Any]] = []
        permissions = tool_permissions or ToolPermissionConfig()
        for tool in self._tools.values():
            descriptor = tool.descriptor()
            if tool_selection is not None and not tool_selection.allows(descriptor):
                continue
            # Effective visibility filter: per-tool override -> category default.
            policy = permissions.resolve(tool.name, descriptor.category)
            if policy.action == "deny":
                continue
            schema = tool.to_openai_tool()
            fn = schema.get("function") if isinstance(schema, dict) else None
            if isinstance(fn, dict):
                fn["x_pycat_category"] = descriptor.category
                fn["x_pycat_source"] = descriptor.source
            schemas.append(schema)
        return schemas

    async def execute(self, tool_name: str, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute a tool.

        Permission checks are resolved before reaching the registry, using the
        request-scoped RunPolicy. The registry is only the tool catalog and
        invocation boundary.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(f"Tool '{tool_name}' not found", is_error=True)

        try:
            result = await tool.execute(arguments, context)
            return result
        except Exception as e:
            return ToolResult(f"Tool execution error: {str(e)}", is_error=True)
