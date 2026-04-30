from __future__ import annotations

from typing import Any, Dict

from core.config.io import (
    load_settings_dict,
    load_app_config,
    set_cached_settings_dict,
)


def load_settings_from_disk() -> Dict[str, Any]:
    return load_settings_dict() or {}


def get_app_settings(*, refresh: bool = False) -> Dict[str, Any]:
    # Keep legacy dict API stable.
    return load_app_config(refresh=refresh).to_dict()


def set_cached_settings(settings: Dict[str, Any] | None) -> None:
    # Keep legacy setter stable.
    set_cached_settings_dict(settings or {})


def _get_nested(settings: Dict[str, Any], key_path: str, default: Any = None) -> Any:
    cur: Any = settings
    for part in (key_path or "").split("."):
        if not part:
            continue
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def get_default_max_context_messages(settings: Dict[str, Any] | None = None) -> int:
    s = settings or get_app_settings()
    v = _get_nested(s, "context.default_max_context_messages", 0)
    try:
        v = int(v)
    except Exception:
        v = 0
    return v if v > 0 else 0


def get_prompt_optimizer_system_prompt(settings: Dict[str, Any] | None = None) -> str | None:
    s = settings or get_app_settings()
    po = s.get("prompt_optimizer") if isinstance(s, dict) else None
    if not isinstance(po, dict):
        return None

    templates = po.get("templates")
    if not isinstance(templates, dict):
        templates = {}

    sel = str(po.get("selected_template") or "default")
    text = templates.get(sel)
    text = (text or "").strip()
    return text or None


def is_agent_auto_compress_enabled(settings: Dict[str, Any] | None = None) -> bool:
    s = settings or get_app_settings()
    v = _get_nested(s, "context.agent_auto_compress_enabled", True)
    try:
        return bool(v)
    except Exception:
        return True


def get_compression_policy_overrides(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    s = settings or get_app_settings()
    pol = _get_nested(s, "context.compression_policy", {})
    return pol if isinstance(pol, dict) else {}
