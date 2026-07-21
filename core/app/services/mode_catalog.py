"""Persistence boundary for user-wide mode and sub-agent profiles."""
from __future__ import annotations

from core.config import save_user_modes_dict
from core.modes.defaults import get_required_mode_slugs
from core.modes.manager import ModeManager
from models.contracts.mode import ModeConfig


class ModeCatalogService:
    def load(self) -> list[ModeConfig]:
        return list(ModeManager(None, include_project=False).list_modes())

    def save(self, modes: list[ModeConfig] | tuple[ModeConfig, ...]) -> bool:
        items = list(modes or ())
        slugs = {str(mode.slug or "").strip().lower() for mode in items}
        if not set(get_required_mode_slugs()).issubset(slugs):
            return False
        return save_user_modes_dict({"modes": [self._to_dict(mode) for mode in items]})

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
