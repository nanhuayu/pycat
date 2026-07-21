"""Skill invocation routing and execution availability."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.tools.mcp.naming import build_mcp_tool_name, tool_names_match
from models.contracts.skill import Skill, SkillExecutionCheck, SkillInvocationSpec
from models.contracts.tooling import ToolSelectionPolicy, normalize_tool_category

COMMAND_TOOL_NAMES: Tuple[str, ...] = (
    "shell__run",
    "shell__start",
    "shell__read",
    "shell__kill",
)


def resolve_skill_invocation_spec(
    skill: Skill,
    *,
    fallback_mode: str = "agent",
) -> SkillInvocationSpec:
    """Resolve the declared invocation contract for an explicit skill run."""
    metadata = dict(getattr(skill, "metadata", {}) or {})
    mode = (
        str(first_non_empty(metadata.get("mode"), fallback_mode or "agent") or "agent")
        .strip()
        .lower()
        or "agent"
    )

    raw_tools = coerce_list(
        first_non_empty(
            metadata.get("tools"),
            metadata.get("allowed-tools"),
            metadata.get("allowed_tools"),
            metadata.get("tool"),
        )
    )
    preferred_cli = tuple(extract_cli_hints(raw_tools))

    executor = normalize_executor(
        first_non_empty(
            metadata.get("executor"),
            metadata.get("executor-type"),
            metadata.get("executor_type"),
        )
    )
    if not executor:
        if preferred_cli:
            executor = "cli"
        elif any(looks_like_mcp_ref(item) for item in raw_tools):
            executor = "mcp"
        else:
            executor = "instruction"

    execution_mode = (
        "fork"
        if str(
            first_non_empty(
                metadata.get("context"),
                metadata.get("execution-mode"),
                metadata.get("execution_mode"),
            )
        )
        .strip()
        .lower()
        == "fork"
        else "inline"
    )

    raw_categories = coerce_list(
        first_non_empty(
            metadata.get("allowed-tool-categories"),
            metadata.get("allowed_tool_categories"),
            metadata.get("allowed-categories"),
            metadata.get("allowed_categories"),
        )
    )
    categories = {normalize_tool_category(item) for item in raw_categories if str(item or "").strip()}
    if executor == "cli":
        categories.add("execute")
    if executor == "mcp":
        categories.add("mcp")
    tool_selection = ToolSelectionPolicy.from_categories(categories or None)

    return SkillInvocationSpec(
        mode=mode,
        executor=executor,
        execution_mode=execution_mode,
        user_invocable=coerce_bool(metadata.get("user-invocable"), default=True),
        disable_model_invocation=coerce_bool(
            metadata.get("disable-model-invocation"),
            default=False,
        ),
        tool_selection=tool_selection,
        declared_tools=tuple(extract_declared_tool_names(raw_tools, executor=executor)),
        preferred_cli=preferred_cli,
    )


def check_skill_execution_availability(
    skill: Skill,
    tools: Iterable[Dict[str, Any]],
    *,
    fallback_mode: str = "agent",
) -> SkillExecutionCheck:
    """Check whether a skill can execute with the concrete tools in this request."""
    spec = resolve_skill_invocation_spec(skill, fallback_mode=fallback_mode)
    available_tools = extract_available_tool_names(tools)

    if spec.executor == "instruction":
        return SkillExecutionCheck(
            executable=True,
            reason="This skill injects instructions only and does not require a dedicated executor.",
        )

    if spec.executor == "cli":
        concrete_tools = tuple(
            tool_name for tool_name in COMMAND_TOOL_NAMES if tool_name in available_tools
        )
        if concrete_tools:
            return SkillExecutionCheck(
                executable=True,
                concrete_tools=concrete_tools,
                reason="Shell command tools are available for this CLI-oriented skill.",
            )
        return SkillExecutionCheck(
            executable=False,
            reason="This skill requires shell command tools, but none are available in the current request.",
        )

    if spec.executor == "mcp":
        if not spec.declared_tools:
            return SkillExecutionCheck(
                executable=False,
                reason="This skill declares MCP execution but does not list any concrete MCP tools.",
            )

        concrete_tools: List[str] = []
        missing_tools: List[str] = []
        for declared_tool in spec.declared_tools:
            match = find_matching_tool_name(declared_tool, available_tools)
            if match:
                concrete_tools.append(match)
            else:
                missing_tools.append(declared_tool)

        if missing_tools:
            return SkillExecutionCheck(
                executable=False,
                concrete_tools=tuple(dedupe_preserve_order(concrete_tools)),
                missing_tools=tuple(dedupe_preserve_order(missing_tools)),
                reason=f"Missing declared MCP tools: {', '.join(dedupe_preserve_order(missing_tools))}",
            )

        return SkillExecutionCheck(
            executable=True,
            concrete_tools=tuple(dedupe_preserve_order(concrete_tools)),
            reason="All declared MCP tools are available in the current request.",
        )

    return SkillExecutionCheck(
        executable=False,
        reason=f"Unsupported skill executor: {spec.executor}",
    )


def extract_cli_hints(values: Iterable[str]) -> List[str]:
    hints: List[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        lowered = value.lower()
        if lowered.startswith("bash(") and value.endswith(")"):
            inner = value[5:-1].strip()
            inner = inner.replace(":*", "").rstrip("*").strip()
            if inner.endswith(":"):
                inner = inner[:-1].strip()
            if inner:
                hints.append(inner)
    return dedupe_preserve_order(hints)


def extract_declared_tool_names(values: Iterable[str], *, executor: str) -> List[str]:
    result: List[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered.startswith("bash(") and value.endswith(")"):
            continue
        if executor != "mcp":
            continue

        if lowered.startswith("mcp:"):
            reference = value[4:].strip()
            if ":" in reference:
                server_name, tool_name = reference.split(":", 1)
                result.append(build_mcp_tool_name(server_name, tool_name))
                continue
            result.append(reference)
            continue

        if value.count(":") == 1 and not lowered.startswith("http"):
            server_name, tool_name = value.split(":", 1)
            result.append(build_mcp_tool_name(server_name, tool_name))
            continue

        result.append(value)
    return dedupe_preserve_order(result)


def extract_available_tool_names(tools: Iterable[Dict[str, Any]]) -> Tuple[str, ...]:
    names: List[str] = []
    for tool in tools:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = str(fn.get("name") or "").strip()
        if name:
            names.append(name)
    return tuple(dedupe_preserve_order(names))


def find_matching_tool_name(declared_tool: str, available_tools: Iterable[str]) -> str:
    target = str(declared_tool or "").strip()
    if not target:
        return ""
    for available_tool in available_tools:
        if tool_names_match(target, available_tool):
            return available_tool
    return ""


def normalize_executor(value: Any) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"", "none", "instruction", "instructions", "instruction-only", "instruction_only"}:
        return "instruction" if lowered else ""
    if lowered in {"cli", "shell", "command", "commands", "bash"}:
        return "cli"
    if lowered == "mcp":
        return "mcp"
    return ""


def looks_like_mcp_ref(value: Any) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered.startswith("mcp__") or lowered.startswith("mcp:")


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        if isinstance(value, list):
            if value:
                return value
            continue
        return value
    return ""


def coerce_bool(value: Any, *, default: bool) -> bool:
    coerced = coerce_optional_bool(value)
    if coerced is None:
        return bool(default)
    return coerced


def coerce_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    return None


def coerce_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result
