"""Mode and delegated-agent profile loading."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from core.config.io import get_user_modes_json_path, load_project_config, load_user_modes_dict
from core.config.migrations import migrate_mode_payload, migrate_modes_payload, migrate_category
from core.modes.defaults import get_default_modes, get_primary_mode_slugs
from models.contracts.mode import ModeConfig, normalize_mode_slug
from models.contracts.model_target import ModelTarget


logger = logging.getLogger(__name__)


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _model_target(raw: object) -> ModelTarget:
    return ModelTarget.from_dict(raw if isinstance(raw, (dict, str)) else {})


def resolve_mode_config(
    mode_slug: str,
    *,
    work_dir: str | None = None,
    mode_manager: Optional["ModeManager"] = None,
) -> ModeConfig:
    return (mode_manager or ModeManager(work_dir)).get(normalize_mode_slug(mode_slug))


class ModeManager:
    def __init__(self, work_dir: str | None = None, *, include_project: bool = True):
        self.work_dir = str(work_dir or ".")
        self.include_project = bool(include_project)
        self._cache: Optional[Dict[str, ModeConfig]] = None

    def list_modes(self) -> List[ModeConfig]:
        self._ensure_loaded()
        return list((self._cache or {}).values())

    def list_ui_modes(self) -> List[ModeConfig]:
        self._ensure_loaded()
        modes = self._cache or {}
        ordered: list[ModeConfig] = []
        seen: set[str] = set()
        for slug in get_primary_mode_slugs():
            mode = modes.get(slug)
            if mode is not None:
                ordered.append(mode)
                seen.add(slug)
        for mode in modes.values():
            if mode.slug == "channel":
                continue
            if mode.slug not in seen and mode.is_primary_mode():
                ordered.append(mode)
                seen.add(mode.slug)
        return ordered

    def list_subagent_profiles(self) -> List[ModeConfig]:
        self._ensure_loaded()
        return [mode for mode in (self._cache or {}).values() if mode.is_subagent_profile()]

    def get(self, slug: str) -> ModeConfig:
        self._ensure_loaded()
        key = normalize_mode_slug(slug)
        found = (self._cache or {}).get(key)
        if found is not None:
            return found
        fallback = (self._cache or {}).get("chat") or get_default_modes()[0]
        return replace(fallback, slug=key, name=key, source=None)

    def find(self, slug: str) -> ModeConfig | None:
        self._ensure_loaded()
        return (self._cache or {}).get(normalize_mode_slug(slug))

    def _ensure_loaded(self) -> None:
        if self._cache is not None:
            return

        builtin = {item.slug: item for item in get_default_modes()}
        modes = dict(builtin)

        try:
            global_payload = load_user_modes_dict()
            for item in self._parse_modes(global_payload, source="global"):
                modes[item.slug] = self._merge_with_builtin(item, builtin.get(item.slug))
        except Exception as exc:
            logger.debug("Failed to load global modes: %s", exc)

        if self.include_project:
            try:
                project = load_project_config(self.work_dir)
                for item in self._parse_modes({"modes": project.modes}, source="project"):
                    modes[item.slug] = self._merge_with_builtin(item, builtin.get(item.slug))
            except Exception as exc:
                logger.debug("Failed to load project modes: %s", exc)

        modes.setdefault("chat", builtin["chat"])
        self._cache = modes

    @staticmethod
    def _merge_with_builtin(mode: ModeConfig, builtin: ModeConfig | None) -> ModeConfig:
        if builtin is None:
            return mode
        target = mode.model_target if mode.model_target.model_ref else builtin.model_target
        return replace(
            builtin,
            name=mode.name or builtin.name,
            purpose=mode.purpose or builtin.purpose,
            prompt=mode.prompt or builtin.prompt,
            allowed_tool_categories=mode.allowed_tool_categories or builtin.allowed_tool_categories,
            profile_kind=mode.profile_kind or builtin.profile_kind,
            completion_policy=mode.completion_policy or builtin.completion_policy,
            model_target=target,
            max_turns=mode.max_turns if mode.max_turns is not None else builtin.max_turns,
            shared_context_policy=mode.shared_context_policy or builtin.shared_context_policy,
            source=mode.source or builtin.source,
        )

    @staticmethod
    def _parse_modes(payload: object, *, source: str) -> list[ModeConfig]:
        normalized = migrate_modes_payload(payload)
        result: list[ModeConfig] = []
        for raw in normalized.get("modes") or []:
            if not isinstance(raw, dict):
                continue
            item = migrate_mode_payload(raw)
            slug = normalize_mode_slug(str(item.get("slug") or ""))
            if not slug:
                continue
            categories = []
            for category in item.get("allowed_tool_categories") or []:
                normalized_category = migrate_category(category)
                if normalized_category not in categories:
                    categories.append(normalized_category)
            profile_kind = str(item.get("profile_kind") or "primary").strip().lower()
            if profile_kind not in {"primary", "subagent", "both"}:
                profile_kind = "primary"
            result.append(
                ModeConfig(
                    slug=slug,
                    name=str(item.get("name") or slug).strip() or slug,
                    purpose=str(item.get("purpose") or "").strip(),
                    prompt=str(item.get("prompt") or "").strip(),
                    allowed_tool_categories=tuple(categories),
                    profile_kind=profile_kind,
                    completion_policy=str(item.get("completion_policy") or "text"),
                    model_target=_model_target(item.get("model_target")),
                    max_turns=_positive_int(item.get("max_turns")),
                    shared_context_policy=str(item.get("shared_context_policy") or "indexes_only"),
                    source=source,
                )
            )
        return result
