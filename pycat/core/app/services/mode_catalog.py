"""Persistence boundary for user-wide mode and sub-agent profiles."""

from __future__ import annotations

from pycat.core.config import save_user_modes_dict
from pycat.core.modes.defaults import get_required_mode_slugs
from pycat.core.modes.manager import ModeManager
from pycat.models.contracts.mode import ModeConfig


class ModeCatalogService:
    def __init__(self, *, data_dir: str | None = None):
        self.data_dir = data_dir

    def load(self) -> list[ModeConfig]:
        return list(ModeManager(None, include_project=False, data_dir=self.data_dir).list_modes())

    def list(self, work_dir: str = "") -> list[ModeConfig]:
        return list(ModeManager(work_dir, data_dir=self.data_dir).list_modes())

    def save(self, modes: list[ModeConfig] | tuple[ModeConfig, ...]) -> bool:
        items = list(modes or ())
        slugs = {str(mode.slug or "").strip().lower() for mode in items}
        if not set(get_required_mode_slugs()).issubset(slugs):
            return False
        return save_user_modes_dict({"modes": [self._to_dict(mode) for mode in items]}, data_dir=self.data_dir)

    @staticmethod
    def _to_dict(mode: ModeConfig) -> dict:
        payload = {
            "slug": mode.slug,
            "name": mode.name,
            "purpose": mode.purpose,
            "prompt": mode.prompt,
            "allowed_tool_categories": list(mode.allowed_tool_categories or ()),
            "profile_kind": mode.profile_kind,
            "completion_policy": mode.completion_policy,
            "source": mode.source,
        }
        if mode.is_subagent_profile():
            payload.update(
                {
                    "model_target": mode.model_target.to_dict(),
                    "max_turns": mode.max_turns,
                    "shared_context_policy": mode.shared_context_policy,
                }
            )
        return payload
