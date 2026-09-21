"""Stage agent-authored methods for explicit comparison and publication."""
from typing import Any, Dict

from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.models.session_paths import normalize_work_dir


class ManageSkillTool(BaseTool):
    def __init__(self, service: Any = None):
        self.service = service

    @property
    def name(self) -> str:
        return "skill__manage"

    @property
    def display_name(self) -> str:
        return "提出技能候选"

    @property
    def category(self) -> str:
        return "skills"

    @property
    def description(self) -> str:
        return ("Propose a reusable method. create stages a new skill; patch stages a revision to an "
                "agent-created managed skill. Proposals stay inactive until isolated comparison passes "
                "and the user publishes them in Settings > Tools & Capabilities > Skills > Installed > More > Candidates.")

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "patch"]},
            "name": {"type": "string", "description": "Lowercase letters/digits/hyphens, 3–64 chars."},
            "scope": {"type": "string", "enum": ["project", "global"]},
            "description": {"type": "string", "maxLength": 200},
            "content": {"type": "string", "maxLength": 12000},
        }, "required": ["action", "name", "description", "content"], "additionalProperties": False}

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return "medium"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        if self.service is None:
            return ToolResult("Skill proposal service unavailable.", is_error=True)
        work_dir = normalize_work_dir(context.work_dir)
        ok, message = self.service.stage_candidate(
            name=str(arguments.get("name") or "").strip(),
            description=str(arguments.get("description") or "").strip(),
            content=str(arguments.get("content") or ""),
            action=str(arguments.get("action") or "").strip(),
            scope=str(arguments.get("scope") or ("project" if work_dir else "global")),
            work_dir=work_dir, reason="Proposed during the current task")
        return ToolResult("Candidate staged: " + message if ok else message, is_error=not ok,
                          metadata={"changed_domains": ["skill"]} if ok else {})
