"""Workspace-aware skill discovery and declared invocation service."""
from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from core.config import get_global_subdir
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

    _NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

    def list_for_workdir(self, work_dir: str | None, *, include_disabled: bool = False) -> list:
        return SkillsManager(work_dir or ".", include_disabled=include_disabled).list_skills()

    def get(
        self,
        skill_name: str,
        *,
        work_dir: str | None,
        include_disabled: bool = False,
    ) -> Skill | None:
        return SkillsManager(work_dir or ".", include_disabled=include_disabled).get(skill_name)

    def exists(self, skill_name: str, *, work_dir: str | None) -> bool:
        return SkillsManager(work_dir or ".").get(skill_name) is not None

    def create_managed(
        self,
        name: str,
        *,
        description: str = "",
        scope: str = "global",
        work_dir: str | None,
    ) -> Path:
        skill_name = str(name or "").strip().lower()
        if not self._NAME_RE.fullmatch(skill_name):
            raise ValueError("技能名称只能包含小写字母、数字、点、下划线和连字符，长度不超过 64。")
        root = self._scope_root(scope, work_dir)
        root.mkdir(parents=True, exist_ok=True)
        target = root / skill_name
        if target.exists():
            raise ValueError(f"技能已存在：{skill_name}")

        staging = root / f".{skill_name}.creating-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            text = (
                "---\n"
                f"name: {skill_name}\n"
                f"description: {self._yaml_scalar(description or '说明这个技能适合处理什么。')}\n"
                "mode: agent\n"
                "---\n\n"
                "# 使用说明\n\n"
                "写下触发场景、处理步骤和必要约束。\n"
            )
            (staging / "SKILL.md").write_text(text, encoding="utf-8")
            os.replace(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return target / "SKILL.md"

    def set_managed_enabled(self, skill: Skill, enabled: bool, *, work_dir: str | None) -> None:
        root = self._managed_root(skill, work_dir)
        marker = root / ".disabled"
        if enabled:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            return
        marker.write_text("disabled by PyCat settings\n", encoding="utf-8")

    def delete_managed(self, skill: Skill, *, work_dir: str | None) -> None:
        root = self._managed_root(skill, work_dir)
        staged = root.parent / f".{root.name}.deleting-{uuid.uuid4().hex}"
        os.replace(root, staged)
        try:
            shutil.rmtree(staged)
        except Exception:
            try:
                os.replace(staged, root)
            except Exception:
                pass
            raise

    def _scope_root(self, scope: str, work_dir: str | None) -> Path:
        value = str(scope or "global").strip().lower()
        if value == "global":
            return get_global_subdir("skills").resolve()
        if value == "project":
            clean_work_dir = str(work_dir or "").strip()
            if not clean_work_dir:
                raise ValueError("没有工作区时不能创建项目技能。")
            return (Path(clean_work_dir).expanduser().resolve() / ".pycat" / "skills").resolve()
        raise ValueError("技能范围只能是 global 或 project。")

    def _managed_root(self, skill: Skill, work_dir: str | None) -> Path:
        if skill is None or bool(getattr(skill, "read_only", False)):
            raise ValueError("外部技能为只读，不能修改。")
        source = Path(str(getattr(skill, "source", "") or "")).expanduser().resolve()
        root = source.parent
        if not (root / "SKILL.md").is_file():
            raise ValueError("技能入口文件不存在。")
        allowed = [get_global_subdir("skills").resolve()]
        if str(work_dir or "").strip():
            allowed.append((Path(work_dir).expanduser().resolve() / ".pycat" / "skills").resolve())
        for base in allowed:
            try:
                root.relative_to(base)
            except ValueError:
                continue
            return root
        raise ValueError("技能路径不在 PyCat 管理目录内。")

    @staticmethod
    def _yaml_scalar(value: str) -> str:
        text = " ".join(str(value or "").split())
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

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
