"""Workspace-aware skill discovery and declared invocation service."""
from __future__ import annotations

from core.skills import (
    Skill,
    SkillExecutionCheck,
    SkillInvocationSpec,
    SkillsManager,
    check_skill_execution_availability,
    resolve_skill_invocation_spec,
)


class SkillService:
    """Workspace-aware skill operations for command and UI layers."""

    def list_for_workdir(self, work_dir: str | None) -> list:
        return SkillsManager(work_dir or ".").list_skills()

    def get(self, skill_name: str, *, work_dir: str | None) -> Skill | None:
        return SkillsManager(work_dir or ".").get(skill_name)

    def exists(self, skill_name: str, *, work_dir: str | None) -> bool:
        return SkillsManager(work_dir or ".").get(skill_name) is not None

    def get_invocation_spec(
        self,
        skill_name: str,
        *,
        work_dir: str | None,
        fallback_mode: str = "agent",
    ) -> SkillInvocationSpec | None:
        skill = self.get(skill_name, work_dir=work_dir)
        if skill is None:
            return None
        return resolve_skill_invocation_spec(skill, fallback_mode=fallback_mode)

    def check_execution(
        self,
        skill_name: str,
        *,
        work_dir: str | None,
        tools,
        fallback_mode: str = "agent",
    ) -> SkillExecutionCheck | None:
        skill = self.get(skill_name, work_dir=work_dir)
        if skill is None:
            return None
        return check_skill_execution_availability(
            skill,
            tools,
            fallback_mode=fallback_mode,
        )