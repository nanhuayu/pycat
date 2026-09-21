"""One discriminated tool for project knowledge, using the shared executor."""
import json

from pycat.core.tools.base import BaseTool, ToolContext, ToolResult


class ManageWikiTool(BaseTool):
    def __init__(self, service=None):
        self.service = service

    @property
    def name(self):
        return "state__wiki"

    @property
    def display_name(self):
        return "项目知识"

    @property
    def category(self):
        return "state"

    @property
    def description(self):
        return "Search, read and curate workspace knowledge. Apply a synthesized conclusion with conditions and pinned ContentRef sources; read the current digest before editing."

    def assess_risk(self, arguments, context):
        return "medium" if arguments.get("action") in {"apply", "delete"} else "low"

    @property
    def input_schema(self):
        return {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "read", "apply", "delete"]},
            "query": {"type": "string"}, "id": {"type": "string"},
            "title": {"type": "string", "maxLength": 120}, "summary": {"type": "string", "maxLength": 300},
            "body": {"type": "string", "maxLength": 12000}, "expected_digest": {"type": "string"},
            "sources": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
                key: {"type": "string"} for key in ("id", "kind", "name", "digest", "workspace", "conversation_id", "locator", "ref")},
                "required": ["id", "kind", "digest", "workspace"], "additionalProperties": False}},
            "offset": {"type": "integer", "minimum": 0},
        }, "required": ["action"], "additionalProperties": False}

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if self.service is None:
            return ToolResult("Project knowledge service unavailable.", is_error=True)
        action = arguments.get("action")
        work_dir = str(context.work_dir or "")
        try:
            if action == "search":
                return ToolResult(json.dumps(self.service.search(work_dir, arguments.get("query", ""), offset=int(arguments.get("offset", 0))), ensure_ascii=False))
            if action == "read":
                return ToolResult(json.dumps(self.service.read(work_dir, arguments.get("id", "")), ensure_ascii=False))
            if action == "apply":
                ok, message = self.service.apply(work_dir, arguments)
                return ToolResult(message, is_error=not ok, metadata={"changed_domains": ["wiki"]} if ok else {})
            if action == "delete":
                self.service.delete(work_dir, arguments.get("id", ""), expected_digest=arguments.get("expected_digest", ""))
                return ToolResult("Project knowledge deleted.", metadata={"changed_domains": ["wiki"]})
            return ToolResult("Unsupported knowledge action.", is_error=True)
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
