from pathlib import Path
from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult
from core.skills import SkillsManager, resolve_skill_invocation_spec


def _get_explicit_skill_name(context: ToolContext) -> str:
    for msg in reversed(getattr(getattr(context, "conversation", None), "messages", []) or []):
        if getattr(msg, "role", "") != "user":
            continue
        metadata = getattr(msg, "metadata", {}) or {}
        payload = metadata.get("skill_run") if isinstance(metadata, dict) else None
        if isinstance(payload, dict):
            return str(payload.get("name") or "").strip().lower()
        break
    return ""


def _ensure_skill_load_allowed(skill_name: str, context: ToolContext, mgr: SkillsManager) -> str:
    skill = mgr.get(skill_name)
    if skill is None:
        return ""
    spec = resolve_skill_invocation_spec(skill)
    explicit_skill_name = _get_explicit_skill_name(context)
    if spec.disable_model_invocation and explicit_skill_name != skill.name:
        return (
            f"Skill '{skill.name}' disables model invocation and must be explicitly invoked by '/{skill.name}' "
            "before it can be loaded."
        )
    return ""


class LoadSkillTool(BaseTool):
    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def description(self) -> str:
        return (
            "Load the full SKILL.md entrypoint for a named skill and return its instructions, "
            "metadata, and available supporting resources. Use this after deciding a skill is relevant."
        )

    @property
    def group(self) -> str:
        return "read"

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Name of the skill to load"
                }
            },
            "required": ["skill"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        skill_name = str(arguments.get("skill") or "").strip().lower()
        mgr = SkillsManager(context.work_dir or ".")
        skill = mgr.get(skill_name)
        if not skill:
            available = ", ".join(sorted(item.name for item in mgr.list_skills()))
            return ToolResult(
                f"Skill '{skill_name}' not found. Available skills: {available or '(none)'}",
                is_error=True,
            )

        denial = _ensure_skill_load_allowed(skill_name, context, mgr)
        if denial:
            return ToolResult(denial, is_error=True)

        spec = resolve_skill_invocation_spec(skill)
        entrypoint = Path(skill.source)
        resources = mgr.list_resources(skill.name)
        lines = [
            f"Skill: {skill.name}",
            f"Entrypoint: {entrypoint}",
            f"Description: {skill.description or '(none)'}",
            f"Executor: {spec.executor}",
            f"Mode: {spec.mode}",
            f"Execution Mode: {spec.execution_mode}",
            f"User Invocable: {spec.user_invocable}",
            f"Disable Model Invocation: {spec.disable_model_invocation}",
        ]
        if spec.preferred_cli:
            lines.append(f"Preferred CLI: {', '.join(spec.preferred_cli)}")
        if spec.declared_tools:
            lines.append(f"Declared Tools: {', '.join(spec.declared_tools)}")
        if resources:
            lines.append("Available Resources:")
            for resource in resources[:40]:
                lines.append(f"- {resource}")
        else:
            lines.append("Available Resources: (none)")

        lines.append("")
        lines.append("--- SKILL.md ---")
        lines.append("")
        lines.append(skill.content)
        return ToolResult("\n".join(lines))


class ReadSkillResourceTool(BaseTool):
    @property
    def name(self) -> str:
        return "read_skill_resource"

    @property
    def description(self) -> str:
        return (
            "Read a supporting file referenced by a skill, such as a file under references/, templates/, or scripts/. "
            "Use this after load_skill when you need more detailed instructions or templates."
        )

    @property
    def group(self) -> str:
        return "read"

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Name of the skill that owns the resource"
                },
                "path": {
                    "type": "string",
                    "description": "Relative resource path inside the skill directory, such as 'references/commands.md'"
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based start line to read (default 1)"
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-based inclusive end line to read (defaults to end of file)"
                }
            },
            "required": ["skill", "path"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        skill_name = str(arguments.get("skill") or "").strip().lower()
        resource_path = str(arguments.get("path") or "").strip()
        start_line = int(arguments.get("start_line") or 1)
        end_line = arguments.get("end_line")
        end_line = int(end_line) if end_line is not None else None

        mgr = SkillsManager(context.work_dir or ".")
        skill = mgr.get(skill_name)
        if skill is None:
            available = ", ".join(sorted(item.name for item in mgr.list_skills()))
            return ToolResult(
                f"Skill '{skill_name}' not found. Available skills: {available or '(none)'}",
                is_error=True,
            )

        denial = _ensure_skill_load_allowed(skill_name, context, mgr)
        if denial:
            return ToolResult(denial, is_error=True)

        snippet = mgr.read_resource(
            skill.name,
            resource_path,
            start_line=start_line,
            end_line=end_line,
        )
        if snippet is None:
            available = ", ".join(mgr.list_resources(skill.name)[:40])
            return ToolResult(
                f"Resource '{resource_path}' was not found for skill '{skill.name}'. "
                f"Available resources: {available or '(none)'}",
                is_error=True,
            )

        content, total_lines, actual_start, actual_end = snippet
        resolved = mgr.resolve_resource_path(skill.name, resource_path)
        header = [
            f"Skill: {skill.name}",
            f"Resource: {resource_path}",
            f"Path: {resolved}",
            f"Lines {actual_start}-{actual_end} of {total_lines}:",
        ]
        if content:
            header.append(content)
        return ToolResult("\n".join(header))
