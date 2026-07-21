"""Skill prompt section construction."""
from __future__ import annotations

from html import escape
from typing import Any, Dict, Iterable

from .discovery import SkillsManager
from .routing import check_skill_execution_availability, resolve_skill_invocation_spec


def build_skill_prompt_section(conversation: Any, tools: Iterable[Dict[str, Any]]) -> str:
    """Build skill catalog/invocation prompt text for the current request."""
    work_dir = getattr(conversation, "work_dir", ".") or "."
    skill_manager = SkillsManager(work_dir)

    latest_skill_run: dict[str, Any] = {}
    for msg in reversed(getattr(conversation, "messages", []) or []):
        if getattr(msg, "role", "") != "user":
            continue
        metadata = getattr(msg, "metadata", {}) or {}
        skill_run = metadata.get("skill_run") if isinstance(metadata, dict) else None
        if isinstance(skill_run, dict):
            latest_skill_run = skill_run
        break

    available_skills = []
    for skill in skill_manager.list_skills():
        spec = resolve_skill_invocation_spec(skill)
        if spec.user_invocable or not spec.disable_model_invocation:
            available_skills.append(skill)

    parts: list[str] = []
    if available_skills:
        catalog_lines = ["<available_skills>"]
        for skill in available_skills:
            spec = resolve_skill_invocation_spec(skill)
            attrs = [f'name="{skill.name}"']
            description = truncate_skill_catalog_value(str(skill.description or "").strip())
            if description:
                attrs.append(f'description="{xml_attr(description)}"')
            attrs.append(f'model_invocable="{str(not spec.disable_model_invocation).lower()}"')
            catalog_lines.append(f"<skill {' '.join(attrs)} />")
        catalog_lines.append("</available_skills>")
        catalog_lines.append(
            "Load a relevant model-invocable skill once with `skill__load`; skills with model_invocable=\"false\" require explicit user invocation."
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
        user_input = str(latest_skill_run.get("user_input") or "").strip()
        if user_input:
            runtime_lines.append(f"user_input: {user_input}")
        runtime_lines.append(f"status: {'executable' if execution.executable else 'unavailable'}")
        if execution.concrete_tools:
            runtime_lines.append(f"concrete_tools: {', '.join(execution.concrete_tools)}")
        if execution.reason:
            runtime_lines.append(f"reason: {execution.reason}")
        if execution.missing_tools:
            runtime_lines.append(f"missing_tools: {', '.join(execution.missing_tools)}")
        runtime_lines.append("rule: Before taking action for an explicitly invoked skill, call `skill__load` to read its SKILL.md entrypoint.")
        if resource_paths:
            runtime_lines.append("rule: Read only referenced supporting files with `skill__read_resource`.")
        if not execution.executable:
            runtime_lines.append("rule: Explain the unavailable dependency; do not invent tools.")
        runtime_lines.append("</invoked_skill>")
        parts.append("\n".join(runtime_lines))

    return "\n\n".join(part for part in parts if part.strip())


def truncate_skill_catalog_value(value: str, *, limit: int = 250) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def xml_attr(value: Any) -> str:
    return escape(str(value or ""), quote=True)
