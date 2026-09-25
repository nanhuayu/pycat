"""Skill discovery and loading."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pycat.core.config import get_global_subdir
from pycat.models.contracts.skill import Skill
from pycat.models.session_paths import resolve_project_data_root

from .manifest import (
    bundled_skill_dir,
    external_user_skill_dirs,
    load_skill_file,
    skill_dir_scope,
)
from .resources import (
    list_skill_resource_paths,
    read_skill_resource,
    resolve_skill_resource_path,
)

logger = logging.getLogger(__name__)


class SkillsManager:
    """Discover and load skills from configured directories."""

    def __init__(self, work_dir: str = ".", *, include_disabled: bool = False, data_dir: str | None = None) -> None:
        self.data_dir = data_dir
        self._work_dir = str(work_dir or "").strip()
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
        if not self._include_disabled:
            self._skills = {name: skill for name, skill in self._skills.items() if skill.enabled}
        self._loaded = True
        logger.debug("Loaded %d skills", len(self._skills))

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(str(name or "").strip().lower())

    def list_skills(self) -> List[Skill]:
        return list(self.skills.values())

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
        if self._work_dir:
            project_dir = resolve_project_data_root(self._work_dir, data_dir=self.data_dir) / "skills"
            if project_dir.is_dir():
                dirs.append(project_dir)

        global_dir = get_global_subdir("skills", data_dir=self.data_dir)
        if global_dir.is_dir():
            dirs.append(global_dir)

        for external_dir in (external_user_skill_dirs() if not self.data_dir or Path(self.data_dir).resolve() == (Path.home() / ".pycat").resolve() else []):
            if external_dir.is_dir():
                dirs.append(external_dir)

        dirs.append(bundled_skill_dir())
        return dirs

    def _load_from_dir(self, directory: Path) -> None:
        source_scope, read_only = skill_dir_scope(directory, self._work_dir, data_dir=self.data_dir)
        try:
            for entry in sorted(directory.iterdir()):
                if entry.name.startswith("."):
                    continue  # hidden entries: .archive, .curator_backups, .usage.json, ...
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
        if source_scope == "bundled":
            enabled = not (get_global_subdir("skills", data_dir=self.data_dir) / ".bundled-disabled" / entry.name).exists()
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
