from __future__ import annotations

import os
import platform
from typing import Any, Dict, List, Optional

from models.conversation import Conversation
from models.provider import Provider

from core.context.file_context import get_file_tree
from core.channel import build_channel_prompt_section
from core.config.schema import AppConfig
from core.modes.manager import resolve_mode_config
from core.modes.types import normalize_mode_slug
from core.prompts.project_instructions import ProjectInstructionService
from core.skills import (
    SkillsManager,
    check_skill_execution_availability,
    resolve_skill_invocation_spec,
)
from core.tools.catalog import TOOL_CATEGORY_LABELS, normalize_tool_category


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful and precise assistant. Follow the user's instructions carefully and ask clarifying questions when needed."
)

DEFAULT_AGENT_TOOL_GUIDELINES = (
    "## Tool Usage\n"
    "- Use the provided tools to interact with the system.\n"
    "- Always check command outputs and handle errors.\n"
    "- If a tool fails, analyze the error and try a different approach.\n"
    "- Use `execute_command` for short bounded commands. Use `shell_start` plus `shell_status`, `shell_logs`, `shell_wait`, or `shell_kill` for long-running commands.\n"
    "- Use `manage_todo` for explicit current-task status, `manage_artifact` for plans/explorations/reports/notes, `manage_memory` for session/workspace/global memory, and `manage_state` for summary/archive.\n"
    "- Complex Task Protocol: for multi-step work, web/search/browser research, multi-source reading, code edits, debugging, planning, or requested timeline/report/document output, the first state-maintenance call should be `manage_todo(action=\"set\", items=[...])` with concrete visible milestones and exactly one `in_progress` item. Do not create ceremonial todos for one-step work.\n"
    "- Good todos are user-visible milestones and acceptance checkpoints, not implementation noise. Good: `审查调用链并定位状态边界`, `移除运行时 nudge 并更新测试`, `生成最终时间线报告`. Bad: `search web`, `read file`, `run formatter`, `grep code`.\n"
    "- Keep todos current when they exist: update completed milestones and the current in-progress item with `manage_todo(action=\"update\", items=[...])`. Completed/cancelled todos are compacted into recent history; do not recreate equivalent completed todos unless the user asks for new work or scope changes.\n"
    "- The todo list is rendered live to the user. Do not repeat the full todo list after a `manage_todo` call; acknowledge the state change briefly and continue with concrete work.\n"
    "- State priority: artifacts are the source of truth for plans/reports/documents; todos are only live progress; memory is only durable reusable facts. If a final report or approved plan already satisfies the request, read/update the artifact or finish instead of rebuilding the same todo list.\n"
    "- Use canonical artifacts: `plan` (kind=plan, status=draft/approved/final), `exploration` (kind=exploration), and `report` (kind=report). Create/update a `plan` artifact for non-trivial execution plans, an `exploration` artifact for multi-source findings, and a final `report` artifact before completion when the user requested a report, timeline, document, or substantial summary.\n"
    "- Artifact indexes/abstracts may be injected without full content. If an existing artifact appears relevant to the current request, read it with `manage_artifact(action=\"read\")` before new broad search or duplication, then update or append as appropriate. Put file paths, URLs, or symbol locations in `references`/`related`, and use `frontmatter` for stable Markdown metadata such as created, tags, source, and status.\n"
    "- When a tool result says the full output was stored in a file, read that exact absolute path before retrying equivalent extraction. Do not guess relative paths for MCP/browser-created files; use returned normalized paths or session `tool-results` paths.\n"
    "- Memory is only for durable, reusable facts and preferences. Before writing workspace/global memory, inspect existing memory with `manage_memory(action=\"list\"|\"view\")` to avoid duplicates. Store stable decisions, verified commands, or repo conventions with `manage_memory`; never save long plans, tool dumps, transient todos, secrets, or temporary reports as memory facts.\n"
    "- `attempt_completion` is a built-in tool for finishing work; do not treat it as a skill or document name.\n"
    "- If another mode is a better fit, use `switch_mode`; if focused work should continue independently, use `subagent__custom`.\n"
    "- Use `capability__summarize_text` for one file or one long text, `subagent__read_analyze` for multi-file/cross-source analysis, and `subagent__search` for research."
)

DEFAULT_PLAN_WORKFLOW = (
    "## Workflow: Plan\n"
    "- Discover context first using read/search/delegated read-only analysis; do not edit files or run implementation commands in plan mode.\n"
    "- Maintain `manage_artifact(name=\"plan\", kind=\"plan\", status=\"draft\")` as the primary deliverable.\n"
    "- Write plans with these sections in order: Summary, Scope, Phases, Steps, Relevant Files, Verification, Decisions, Risks/Open Questions.\n"
    "- Keep each phase small and actionable; include specific files, symbols, and expected checks.\n"
    "- Ask clarifying questions when requirements or trade-offs are unresolved.\n"
    "- Mark the plan `status=\"approved\"` only after user alignment; implementation should read the approved plan before editing."
)

DEFAULT_EXPLORE_WORKFLOW = (
    "## Workflow: Explore\n"
    "- Stay read-only: search broadly, inspect narrowly, and return evidence-backed findings.\n"
    "- Use `manage_artifact(name=\"exploration\", kind=\"exploration\", status=\"draft\")` for reusable findings when exploration spans multiple files.\n"
    "- Report concrete file paths, symbols, patterns, existing design conventions, risks, and open questions.\n"
    "- Summaries should end with Suggested Next Steps, but not implementation details or edits.\n"
    "- Do not create implementation plans unless requested; hand off to Plan or Agent when changes are needed."
)

DEFAULT_IMPLEMENT_WORKFLOW = (
    "## Workflow: Implement\n"
    "- If a `plan` artifact exists, read it first and treat an approved/final plan as the execution source of truth.\n"
    "- Maintain todo for concrete visible milestones when the task spans multiple substantial steps; keep one `in_progress` item and mark items complete after finishing them.\n"
    "- Before any non-trivial edit, confirm the target files and the acceptance criteria from the plan or exploration notes.\n"
    "- After edits, verify with targeted tests or diagnostics.\n"
    "- Save verification notes or final summaries in `manage_artifact(name=\"report\", kind=\"report\", status=\"final\")` when the result is substantial.\n"
    "- Use `manage_memory` only for durable facts; do not store transient progress, drafts, or large tool outputs there."
)


def _normalize_string_tuple(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        candidates = [part.strip() for part in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        candidates = [str(item).strip() for item in values]
    else:
        candidates = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return tuple(normalized)


def _enabled_channel_sources(config: AppConfig) -> tuple[str, ...]:
    seen: set[str] = set()
    sources: list[str] = []
    for channel in getattr(config, "channels", []) or []:
        if not bool(getattr(channel, "enabled", False)):
            continue
        source = str(getattr(channel, "source", "") or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        sources.append(source)
    return tuple(sources)
def build_mode_profile_section(mode_slug: str, mode_cfg: Optional[Any]) -> str:
    lines = ["<mode_profile>", f"slug: {mode_slug}"]

    if mode_cfg is not None:
        if (mode_cfg.name or "").strip():
            lines.append(f"name: {mode_cfg.name}")
        if (mode_cfg.description or "").strip():
            lines.append(f"description: {mode_cfg.description}")
        if (mode_cfg.when_to_use or "").strip():
            lines.append(f"when_to_use: {mode_cfg.when_to_use}")
        allowed_tool_categories = sorted(mode_cfg.tool_category_names())
        if allowed_tool_categories:
            lines.append(f"allowed_tool_categories: {', '.join(allowed_tool_categories)}")

    lines.append("</mode_profile>")
    return "\n".join(lines)


def build_mode_workflow_guidance(mode_slug: str) -> str:
    slug = normalize_mode_slug(mode_slug)
    guidance: dict[str, list[str]] = {
        "agent": [
            DEFAULT_IMPLEMENT_WORKFLOW,
            "Maintain the current todo list with `manage_todo` when scope changes or steps complete.",
            "Create or update a short working plan with `manage_artifact(name=\"plan\", kind=\"plan\", status=\"draft\")` for multi-step execution.",
            "Store durable facts such as important paths, commands, or decisions with `manage_memory` instead of repeating them in chat.",
            "Use `switch_mode` if the request clearly belongs to another mode, or `subagent__custom` if a separate delegated run is better.",
            "Use `attempt_completion` only when the task is actually complete and you can summarize the result clearly.",
        ],
        "plan": [
            DEFAULT_PLAN_WORKFLOW,
            "Create and maintain a plan artifact as the primary artifact for architecture work.",
            "Use `manage_todo` to track open design questions and decision checkpoints.",
            "Persist only confirmed constraints or decisions into memory.",
            "Switch to a more appropriate mode if the task stops being architecture work, and call `attempt_completion` once the design output is ready.",
        ],
        "explore": [
            DEFAULT_EXPLORE_WORKFLOW,
            "Prefer broad-to-narrow workspace search, then read the smallest necessary file ranges.",
            "Use `related` and `references` when saving exploration artifacts so later implementation can recover evidence quickly.",
            "Switch to plan or agent mode rather than editing directly.",
        ],
    }
    items = guidance.get(slug)
    if not items:
        return ""
    lines = ["## State Workflow"]
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if text.startswith("## "):
            lines.append(text)
        else:
            lines.append(f"- {text}")
    return "\n".join(lines)


def build_environment_section(work_dir: str, max_depth: int = 2) -> str:
    os_info = platform.system() + " " + platform.release()
    file_tree = get_file_tree(work_dir, max_depth=max_depth)
    parts = [
        "<environment_info>",
        f"OS: {os_info}",
        f"WorkDir: {os.path.abspath(work_dir)}",
        "</environment_info>",
        "",
        "<workspace_info>",
        file_tree or "(empty)",
        "</workspace_info>",
    ]
    return "\n".join(parts).strip()


def resolve_base_system_prompt_text(
    *,
    conversation: Conversation,
    app_config: AppConfig,
    default_work_dir: str = ".",
    include_conversation_override: bool = True,
) -> str:
    settings = conversation.settings or {}
    mode_slug = normalize_mode_slug(str(getattr(conversation, "mode", "chat") or "chat"))
    work_dir = getattr(conversation, "work_dir", None) or default_work_dir

    try:
        mode_cfg = resolve_mode_config(mode_slug, work_dir=str(work_dir))
    except Exception:
        mode_cfg = None

    if include_conversation_override:
        conv_custom = ((settings.get("system_prompt") or "").strip() or (settings.get("custom_instructions") or "").strip())
        if conv_custom:
            return conv_custom

    prompt_cfg = app_config.prompts
    if mode_cfg is not None and (mode_cfg.role_definition or "").strip():
        return mode_cfg.role_definition.strip()
    if (prompt_cfg.default_system_prompt or "").strip():
        return prompt_cfg.default_system_prompt.strip()
    if (prompt_cfg.base_role_definition or "").strip():
        return prompt_cfg.base_role_definition.strip()
    return DEFAULT_SYSTEM_PROMPT


def build_state_section(conversation: Conversation) -> str:
    try:
        state = conversation.get_state()
        return state.to_prompt_view(include_artifacts=False, include_memory_facts=False) or ""
    except Exception:
        return ""


def build_available_tools_section(tools: List[Dict[str, Any]], *, max_description_chars: int = 180) -> str:
    """Build a compact, catalog-aligned summary of request-time tools.

    The authoritative tool schemas are still sent through the API request body;
    this section is only a short navigation aid for the model. It avoids dumping
    full JSON schemas or long MCP descriptions into the system prompt.
    """
    if not tools:
        return ""

    grouped: dict[str, list[tuple[str, str]]] = {}
    for tool in tools:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        description = " ".join(str(fn.get("description") or "").split())
        if len(description) > max_description_chars:
            description = description[: max_description_chars - 1].rstrip() + "…"
        category = normalize_tool_category(fn.get("x_pycat_category"))
        grouped.setdefault(category, []).append((name, description))

    if not grouped:
        return ""
    lines = ["<available_tools>", "Tool schemas are available in the request body; use only names listed here."]
    category_order = {name: index for index, name in enumerate(TOOL_CATEGORY_LABELS.keys())}
    for category in sorted(grouped.keys(), key=lambda c: category_order.get(c, 999)):
        label = TOOL_CATEGORY_LABELS.get(category, category)
        items = sorted(grouped[category], key=lambda item: item[0])
        lines.append(f"[{category}] {label}")
        for name, description in items:
            suffix = f": {description}" if description else ""
            lines.append(f"- {name}{suffix}")
    lines.append("</available_tools>")
    return "\n".join(lines)


def build_system_prompt(
    *,
    conversation: Conversation,
    tools: List[Dict[str, Any]],
    provider: Provider,
    app_config: AppConfig,
    default_work_dir: str = ".",
) -> str:
    settings = conversation.settings or {}

    prompt_cfg = app_config.prompts
    mode_slug = normalize_mode_slug(str(getattr(conversation, "mode", "chat") or "chat"))

    work_dir = getattr(conversation, "work_dir", None) or default_work_dir

    try:
        mode_cfg = resolve_mode_config(mode_slug, work_dir=str(work_dir))
    except Exception:
        mode_cfg = None

    conv_custom = ((settings.get("system_prompt") or "").strip() or (settings.get("custom_instructions") or "").strip())

    parts: list[str] = []

    role_def: Optional[str] = None
    mode_custom: Optional[str] = None

    if mode_cfg is not None:
        role_def = (mode_cfg.role_definition or "").strip() or None
        mode_custom = (mode_cfg.custom_instructions or "").strip() or None

    parts.append(build_mode_profile_section(mode_slug, mode_cfg))

    # System prompt precedence:
    # 1) mode.roleDefinition
    # 2) app.prompts.default_system_prompt
    # 3) app.prompts.base_role_definition (legacy)
    # 4) built-in
    if role_def:
        parts.append(role_def)
    elif (prompt_cfg.default_system_prompt or "").strip():
        parts.append(prompt_cfg.default_system_prompt.strip())
    elif (prompt_cfg.base_role_definition or "").strip():
        parts.append(prompt_cfg.base_role_definition.strip())
    else:
        parts.append(DEFAULT_SYSTEM_PROMPT)

    if (prompt_cfg.agent_tool_guidelines or "").strip():
        parts.append(prompt_cfg.agent_tool_guidelines.strip())
    else:
        parts.append(DEFAULT_AGENT_TOOL_GUIDELINES)

    available_tools_section = build_available_tools_section(tools)
    if available_tools_section:
        parts.append(available_tools_section)

    project_instructions = ProjectInstructionService.build_prompt_section(str(work_dir or "."))
    if project_instructions:
        parts.append(project_instructions)

    if bool(prompt_cfg.include_state):
        state_section = build_state_section(conversation)
        if state_section:
            parts.append(state_section)

    enabled_channel_sources = _enabled_channel_sources(app_config)
    allowed_channel_sources = _normalize_string_tuple(settings.get("allowed_channel_sources")) or enabled_channel_sources
    trusted_channel_sources = tuple(
        source for source in _normalize_string_tuple(settings.get("trusted_channel_sources"))
        if source in allowed_channel_sources
    )
    channel_notice_policy = str(settings.get("channel_notice_policy", "notice") or "notice").strip().lower() or "notice"
    channel_section = build_channel_prompt_section(
        getattr(conversation, "messages", []) or [],
        configured_sources=enabled_channel_sources,
        allowed_sources=allowed_channel_sources,
        trusted_sources=trusted_channel_sources,
        notice_policy=channel_notice_policy,
    )
    if channel_section:
        parts.append(channel_section)

    workflow_guidance = build_mode_workflow_guidance(mode_slug)
    if workflow_guidance:
        parts.append(workflow_guidance)

    combined_custom = "\n\n".join(
        [x for x in [mode_custom, conv_custom] if isinstance(x, str) and x.strip()]
    ).strip()
    if combined_custom:
        parts.append(f"## Custom Instructions\n{combined_custom}")

    latest_skill_run: dict[str, Any] = {}
    for msg in reversed(getattr(conversation, "messages", []) or []):
        if getattr(msg, "role", "") != "user":
            continue
        metadata = getattr(msg, "metadata", {}) or {}
        skill_run = metadata.get("skill_run") if isinstance(metadata, dict) else None
        if isinstance(skill_run, dict):
            latest_skill_run = skill_run
        break

    skill_manager = SkillsManager(getattr(conversation, "work_dir", ".") or ".")
    available_skills = []
    for skill in skill_manager.list_skills():
        spec = resolve_skill_invocation_spec(skill)
        if spec.user_invocable:
            available_skills.append(skill)
    if available_skills:
        catalog_lines = ["<available_skills>"]
        for skill in available_skills:
            spec = resolve_skill_invocation_spec(skill)
            attrs = [f'name="{skill.name}"']
            description = str(skill.description or "").strip()
            if description:
                attrs.append(f'description="{description}"')
            attrs.append(f'executor="{spec.executor}"')
            arg_hint = str(skill.metadata.get("argument-hint") or "").strip()
            if arg_hint:
                attrs.append(f'argument_hint="{arg_hint}"')
            if skill.tags:
                attrs.append(f'tags="{", ".join(skill.tags)}"')
            catalog_lines.append(f"<skill {' '.join(attrs)} />")
        catalog_lines.append("</available_skills>")
        catalog_lines.append(
            "The catalog above is for progressive skill discovery. Do not assume a skill is active or callable unless the user explicitly invoked `/{skill-name}` in this turn. Skill names are not tool names."
        )
        parts.append("\n".join(catalog_lines))

    latest_skill_name = str(latest_skill_run.get("name") or "").strip().lower()
    loaded_skill = skill_manager.get(latest_skill_name) if latest_skill_name else None
    if loaded_skill is not None:
        spec = resolve_skill_invocation_spec(loaded_skill)
        execution = check_skill_execution_availability(loaded_skill, tools)
        resource_paths = skill_manager.list_resources(loaded_skill.name)
        runtime_lines = ["<invoked_skill>"]
        runtime_lines.append(f"name: {loaded_skill.name}")
        runtime_lines.append(f"entrypoint: {loaded_skill.source}")
        runtime_lines.append(f"mode: {spec.mode}")
        runtime_lines.append(f"executor: {spec.executor}")
        runtime_lines.append(f"execution_mode: {spec.execution_mode}")
        runtime_lines.append(f"disable_model_invocation: {spec.disable_model_invocation}")
        user_input = str(latest_skill_run.get("user_input") or "").strip()
        if user_input:
            runtime_lines.append(f"user_input: {user_input}")
        if spec.preferred_cli:
            runtime_lines.append(f"preferred_cli: {', '.join(spec.preferred_cli)}")
        if spec.declared_tools:
            runtime_lines.append(f"declared_tools: {', '.join(spec.declared_tools)}")
        runtime_lines.append(f"status: {'executable' if execution.executable else 'unavailable'}")
        if execution.concrete_tools:
            runtime_lines.append(f"concrete_tools: {', '.join(execution.concrete_tools)}")
        if execution.reason:
            runtime_lines.append(f"reason: {execution.reason}")
        if execution.missing_tools:
            runtime_lines.append(f"missing_tools: {', '.join(execution.missing_tools)}")
        if resource_paths:
            runtime_lines.append(f"resource_paths: {', '.join(resource_paths[:20])}")
        runtime_lines.append("rule: Before taking action for an explicitly invoked skill, call `skill__load` to read its SKILL.md entrypoint.")
        runtime_lines.append("rule: If the loaded skill references supporting files, call `skill__read_resource` only for the specific files you need.")
        if execution.executable:
            runtime_lines.append("rule: Use only concrete tool names that appear in <available_tools> or concrete_tools. Skill names are not tool names.")
        else:
            runtime_lines.append("rule: Do not invent missing tools. If execution is unavailable, explain the missing capability and stop instead of probing repeatedly.")
        runtime_lines.append("</invoked_skill>")
        parts.append("\n".join(runtime_lines))

    return "\n\n".join([p for p in parts if isinstance(p, str) and p.strip()]).strip()
