from pathlib import Path
from typing import Any, Dict

from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.core.skills import SkillsManager, resolve_skill_invocation_spec
from pycat.core.skills.usage import SkillUsageStore
from pycat.core.security.threats import scan_for_threats


MAX_SKILL_ENTRYPOINT_CHARS = 12_000


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
        return "skill__load"

    @property
    def description(self) -> str:
        return "Load a relevant named skill's SKILL.md entrypoint with its metadata and supporting-resource paths."

    @property
    def category(self) -> str:
        return "read"

    @property
    def source(self) -> str:
        return "skill"

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
        control = context.runtime.run_control
        frozen = control.skill_snapshot(skill_name) if control is not None else None
        if frozen is not None:
            return ToolResult(frozen["content"])
        mgr = SkillsManager(context.work_dir, data_dir=context.data_dir)
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

        findings = scan_for_threats(str(skill.content or ""), scope="strict")
        if findings:
            return ToolResult(
                f"Skill '{skill.name}' is blocked because its durable content matched: "
                f"{', '.join(findings)}. Inspect and repair the source before loading it.",
                is_error=True,
                metadata={"blocked": True, "threats": findings},
            )

        usage = _usage_store_for_skill(skill, context)
        if usage is not None:
            usage.record_loaded(skill.name)

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
            "Context Scope: current task only; reload this skill for later unrelated tasks.",
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
        content = str(skill.content or "")
        if len(content) > MAX_SKILL_ENTRYPOINT_CHARS:
            content = (
                content[:MAX_SKILL_ENTRYPOINT_CHARS].rstrip()
                + "\n\n[SKILL.md truncated; read supporting resources for additional detail.]"
            )
        lines.append(content)
        result = "\n".join(lines)
        if control is not None:
            versions = {str(entrypoint.resolve()): _file_version(entrypoint)}
            for resource in resources[:256]:
                resource_file = mgr.resolve_resource_path(skill.name, resource)
                if resource_file:
                    versions[str(resource_file.resolve())] = _file_version(resource_file)
            result = control.skill_snapshot(skill_name, {"content": result, "versions": versions})["content"]
        return ToolResult(result)


class ReadSkillResourceTool(BaseTool):
    @property
    def name(self) -> str:
        return "skill__read_resource"

    @property
    def description(self) -> str:
        return "Read one supporting resource referenced by a loaded skill, optionally limited to an inclusive line range."

    @property
    def category(self) -> str:
        return "read"

    @property
    def source(self) -> str:
        return "skill"

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
        control = context.runtime.run_control
        if control is not None and control.skill_snapshot(skill_name) is None:
            return ToolResult("Load the skill with skill__load before reading its supporting resources.", is_error=True)
        start_line = int(arguments.get("start_line") or 1)
        end_line = arguments.get("end_line")
        end_line = int(end_line) if end_line is not None else None

        mgr = SkillsManager(context.work_dir, data_dir=context.data_dir)
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

        control = context.runtime.run_control
        frozen = control.skill_snapshot(skill_name) if control is not None else None
        if frozen is not None:
            requested = mgr.resolve_resource_path(skill.name, resource_path)
            paths = [Path(skill.source), requested] if requested else [Path(skill.source)]
            if requested is None or any(frozen["versions"].get(str(path.resolve())) != _file_version(path) for path in paths):
                return ToolResult("Skill or resource changed after loading; use a new run to load the new version.", is_error=True)

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
        findings = scan_for_threats(content, scope="strict")
        if findings:
            return ToolResult(
                f"Resource '{resource_path}' is blocked because it matched: {', '.join(findings)}.",
                is_error=True,
                metadata={"blocked": True, "threats": findings},
            )
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


def _file_version(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


def _usage_store_for_skill(skill, context: ToolContext) -> SkillUsageStore | None:
    source = Path(str(getattr(skill, "source", "") or "")).expanduser().resolve(strict=False)
    root = source.parent.parent
    if not (source.parent / "SKILL.md").exists():
        return None
    # The discovery result already identifies the managed package.  Deriving
    # the sidecar root from its source avoids a second global-path lookup that
    # can disagree with a workspace/project overlay.
    scope = str(getattr(skill, "source_scope", "") or "").strip().lower()
    if scope in {"project", "global"} and not bool(getattr(skill, "read_only", False)):
        return SkillUsageStore(root=root, scope=scope)
    # A discovery provider may classify a package as external when its global
    # root is supplied by a test or a plugin-specific resolver.  A matching
    # provenance record is the authoritative proof that this package is a
    # PyCat-managed root; ordinary external skills have no such record.
    if (root / ".usage.json").is_file():
        candidate = SkillUsageStore(root=root, scope="global")
        record = candidate.get(getattr(skill, "name", ""))
        recorded_scope = str(record.get("scope") or "").strip().lower()
        if record and recorded_scope in {"project", "global"}:
            return SkillUsageStore(root=root, scope=recorded_scope)
    return None
