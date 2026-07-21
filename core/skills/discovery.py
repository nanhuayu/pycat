"""Skill discovery and loading."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_global_subdir
from models.contracts.skill import Skill

from .manifest import (
    external_user_skill_dirs,
    load_skill_file,
    parse_skill_frontmatter,
    parse_skill_frontmatter_value,
    skill_dir_scope,
)
from .resources import (
    list_skill_resource_paths,
    read_skill_resource,
    resolve_skill_resource_path,
    resolve_skill_root,
)

logger = logging.getLogger(__name__)


class SkillsManager:
    """Discover and load skills from configured directories."""

    def __init__(self, work_dir: str = ".", *, include_disabled: bool = False) -> None:
        self._work_dir = work_dir
        self._include_disabled = bool(include_disabled)
        self._skills: Dict[str, Skill] = {}
        self._loaded = False

    @property
    def skills(self) -> Dict[str, Skill]:
        if not self._loaded:
            self.reload()
        return self._skills

    def reload(self) -> None:
        self._skills.clear()
        for directory in self._skill_dirs():
            self._load_from_dir(directory)
        self._loaded = True
        logger.debug("Loaded %d skills", len(self._skills))

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(str(name or "").strip().lower())

    def list_skills(self) -> List[Skill]:
        return list(self.skills.values())

    def get_content(self, name: str) -> Optional[str]:
        skill = self.get(name)
        return skill.content if skill else None

    def get_entrypoint(self, name: str) -> Optional[Path]:
        skill = self.get(name)
        if skill is None:
            return None
        return Path(skill.source)

    def get_root_dir(self, name: str) -> Optional[Path]:
        skill = self.get(name)
        if skill is None:
            return None
        return resolve_skill_root(Path(skill.source))

    def list_resources(self, name: str) -> List[str]:
        skill = self.get(name)
        if skill is None:
            return []
        return list_skill_resource_paths(skill)

    def resolve_resource_path(self, name: str, relative_path: str) -> Optional[Path]:
        skill = self.get(name)
        if skill is None:
            return None
        return resolve_skill_resource_path(skill, relative_path)

    def read_resource(
        self,
        name: str,
        relative_path: str,
        *,
        start_line: int = 1,
        end_line: Optional[int] = None,
    ) -> Optional[Tuple[str, int, int, int]]:
        skill = self.get(name)
        if skill is None:
            return None
        return read_skill_resource(
            skill,
            relative_path,
            start_line=start_line,
            end_line=end_line,
        )

    def _skill_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        root = Path(self._work_dir).resolve()

        project_dir = root / ".pycat" / "skills"
        if project_dir.is_dir():
            dirs.append(project_dir)

        global_dir = get_global_subdir("skills")
        if global_dir.is_dir():
            dirs.append(global_dir)

        for external_dir in external_user_skill_dirs():
            if external_dir.is_dir():
                dirs.append(external_dir)

        return dirs

    def _load_from_dir(self, directory: Path) -> None:
        source_scope, read_only = skill_dir_scope(directory, self._work_dir)
        try:
            for entry in sorted(directory.iterdir()):
                skill = None
                try:
                    skill = self._load_skill_entry(
                        entry,
                        source_scope=source_scope,
                        read_only=read_only,
                    )
                except Exception as exc:
                    logger.warning("Failed to load skill %s: %s", entry, exc)
                if not skill or skill.name in self._skills:
                    continue
                self._skills[skill.name] = skill
        except Exception as exc:
            logger.warning("Failed to scan skill directory %s: %s", directory, exc)

    def _load_skill_entry(self, entry: Path, *, source_scope: str, read_only: bool) -> Optional[Skill]:
        if not entry.is_dir():
            return None
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            return None
        enabled = not (entry / ".disabled").exists()
        if not enabled and not self._include_disabled:
            return None
        return self._load_skill_file(
            skill_file,
            default_name=entry.name,
            enabled=enabled,
            source_scope=source_scope,
            read_only=read_only,
        )

    def _load_skill_file(
        self,
        path: Path,
        *,
        default_name: str,
        enabled: bool = True,
        source_scope: str = "project",
        read_only: bool = False,
    ) -> Optional[Skill]:
        return load_skill_file(
            path,
            default_name=default_name,
            enabled=enabled,
            source_scope=source_scope,
            read_only=read_only,
        )

    @staticmethod
    def _parse_frontmatter(content: str):
        return parse_skill_frontmatter(content)

    @staticmethod
    def _parse_frontmatter_value(value: str):
        return parse_skill_frontmatter_value(value)
