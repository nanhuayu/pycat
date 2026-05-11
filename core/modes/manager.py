from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from core.config.io import get_user_modes_json_path, load_project_config
from core.modes.defaults import get_default_modes, get_primary_mode_slugs
from core.modes.types import ToolCategoryOptions, ModeConfig, normalize_mode_slug


logger = logging.getLogger(__name__)


def _parse_tool_name_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return tuple()
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name:
            items.append(name)
    return tuple(items)


def _parse_optional_int(raw: object) -> int | None:
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    return value if value > 0 else None


def _parse_optional_bool(raw: object) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        return None
    return bool(raw)


def resolve_mode_config(
    mode_slug: str,
    *,
    work_dir: str | None = None,
    mode_manager: Optional["ModeManager"] = None,
) -> ModeConfig:
    """Resolve a ModeConfig for a slug.

    Convenience helper so call sites don't need to instantiate ModeManager manually.
    """
    slug = normalize_mode_slug(str(mode_slug or "chat"))
    mm = mode_manager or ModeManager(work_dir)
    return mm.get(slug)


class ModeManager:
    """Loads and provides mode configs.

    - Built-in defaults first
    - Optional user-level modes.json in APPDATA/PyCat
    - Optional project-level modes.json under work_dir
    """

    def __init__(self, work_dir: str | None = None):
        self.work_dir = str(work_dir or ".")
        self._cache: Optional[Dict[str, ModeConfig]] = None

    def list_modes(self) -> List[ModeConfig]:
        self._ensure_loaded()
        assert self._cache is not None
        return list(self._cache.values())

    def list_ui_modes(self) -> List[ModeConfig]:
        """Return a compact, user-facing mode list.

        Built-in modes are reduced to the commonly used subset while custom/project
        modes remain visible so advanced workflows are still reachable.
        """

        self._ensure_loaded()
        assert self._cache is not None

        primary = set(get_primary_mode_slugs())
        ordered: list[ModeConfig] = []
        seen: set[str] = set()

        for slug in get_primary_mode_slugs():
            mode = self._cache.get(slug)
            if mode is None:
                continue
            ordered.append(mode)
            seen.add(mode.slug)

        for mode in self._cache.values():
            if mode.slug in seen:
                continue
            if mode.source == "builtin" and mode.slug not in primary:
                continue
            ordered.append(mode)
            seen.add(mode.slug)

        return ordered

    def get(self, slug: str) -> ModeConfig:
        self._ensure_loaded()
        assert self._cache is not None
        key = normalize_mode_slug(slug)
        found = self._cache.get(key)
        if found is not None:
            return found
        fallback = self._cache.get("chat") or get_default_modes()[0]
        return replace(
            fallback,
            slug=key,
            name=key,
            source=None,
        )

    def _ensure_loaded(self) -> None:
        if self._cache is not None:
            return

        builtin_modes = {m.slug: m for m in get_default_modes()}
        modes: Dict[str, ModeConfig] = dict(builtin_modes)

        try:
            user_path = get_user_modes_json_path()
            if user_path.exists() and user_path.is_file():
                loaded = self._load_modes_json(user_path, source="global")
                for m in loaded:
                    modes[m.slug] = m
        except Exception as exc:
            logger.debug("Failed to load global modes configuration: %s", exc)

        try:
            project = load_project_config(self.work_dir)
            if project.modes:
                for item in project.modes:
                    loaded = self._load_modes_json(Path("<project>"), source="project", raw_items=[item])
                    for m in loaded:
                        modes[m.slug] = m
        except Exception as exc:
            logger.debug("Failed to load project modes configuration: %s", exc)

        if "chat" not in modes:
            modes["chat"] = get_default_modes()[0]

        normalized: Dict[str, ModeConfig] = {}
        for slug, mode in modes.items():
            normalized[slug] = self._normalize_loaded_mode(mode, builtin_modes.get(slug))

        self._cache = normalized

    def _normalize_loaded_mode(self, mode: ModeConfig, builtin_mode: Optional[ModeConfig]) -> ModeConfig:
        allowed_tool_categories = list(mode.allowed_tool_categories or ())
        if not allowed_tool_categories and builtin_mode is not None:
            allowed_tool_categories = list(builtin_mode.allowed_tool_categories or ())
        if builtin_mode is None:
            return replace(mode, allowed_tool_categories=tuple(allowed_tool_categories))

        role_definition = (mode.role_definition or "").strip() or builtin_mode.role_definition
        when_to_use = (mode.when_to_use or "").strip() or builtin_mode.when_to_use
        description = (mode.description or "").strip() or builtin_mode.description
        name = (mode.name or "").strip() or builtin_mode.name
        custom_instructions = (mode.custom_instructions or "").strip() or builtin_mode.custom_instructions
        tool_allowlist = tuple(mode.tool_allowlist or builtin_mode.tool_allowlist or ())
        tool_denylist = tuple(mode.tool_denylist or builtin_mode.tool_denylist or ())
        max_turns = mode.max_turns if mode.max_turns is not None else builtin_mode.max_turns
        context_window_limit = (
            mode.context_window_limit if mode.context_window_limit is not None else builtin_mode.context_window_limit
        )
        auto_compress_enabled = (
            mode.auto_compress_enabled
            if mode.auto_compress_enabled is not None
            else builtin_mode.auto_compress_enabled
        )

        return replace(
            mode,
            name=name,
            role_definition=role_definition,
            when_to_use=when_to_use,
            description=description,
            custom_instructions=custom_instructions,
            allowed_tool_categories=tuple(allowed_tool_categories),
            tool_allowlist=tool_allowlist,
            tool_denylist=tool_denylist,
            max_turns=max_turns,
            context_window_limit=context_window_limit,
            auto_compress_enabled=auto_compress_enabled,
        )

    def _load_modes_json(self, path: Path, *, source: str, raw_items: Optional[List[dict]] = None) -> List[ModeConfig]:
        if raw_items is None:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("modes") if isinstance(raw, dict) else raw
        else:
            items = raw_items
        if not isinstance(items, list):
            return []

        out: List[ModeConfig] = []
        for it in items:
            if not isinstance(it, dict):
                continue

            slug = normalize_mode_slug(str(it.get("slug", "")))
            name = str(it.get("name", slug))
            role_definition = str(it.get("roleDefinition") or it.get("role_definition") or "").strip()
            if not slug:
                continue

            categories = it.get("allowed_tool_categories") or []
            parsed_categories: list = []
            if isinstance(categories, list):
                for category in categories:
                    if isinstance(category, str):
                        parsed_categories.append(category)
                    elif isinstance(category, list) and len(category) == 2 and isinstance(category[0], str) and isinstance(category[1], dict):
                        parsed_categories.append((category[0], ToolCategoryOptions(
                            file_regex=str(category[1].get("fileRegex") or category[1].get("file_regex") or "") or None,
                            description=str(category[1].get("description") or "") or None,
                        )))

            out.append(
                ModeConfig(
                    slug=slug,
                    name=name,
                    role_definition=role_definition,
                    when_to_use=(it.get("whenToUse") or it.get("when_to_use")),
                    description=(it.get("description")),
                    custom_instructions=(it.get("customInstructions") or it.get("custom_instructions")),
                    allowed_tool_categories=tuple(parsed_categories),
                    tool_allowlist=_parse_tool_name_list(it.get("toolAllowlist") or it.get("tool_allowlist")),
                    tool_denylist=_parse_tool_name_list(it.get("toolDenylist") or it.get("tool_denylist")),
                    max_turns=_parse_optional_int(it.get("maxTurns") or it.get("max_turns")),
                    context_window_limit=_parse_optional_int(it.get("contextWindowLimit") or it.get("context_window_limit")),
                    auto_compress_enabled=_parse_optional_bool(it.get("autoCompressEnabled") if "autoCompressEnabled" in it else it.get("auto_compress_enabled")),
                    source=source,
                )
            )

        dedup: Dict[str, ModeConfig] = {}
        for m in out:
            dedup[m.slug] = m
        return list(dedup.values())
