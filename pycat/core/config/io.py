from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from pycat.core.config.migrations import (
    SCHEMA_VERSION,
    migrate_modes_payload,
    migrate_settings_payload,
    restore_migrated_capabilities_from_modes,
)
from pycat.core.persistence import atomic_write_text
from pycat.models.contracts.config import AppConfig, ProjectConfig

_APP_CACHE: AppConfig | None = None
logger = logging.getLogger(__name__)
_GLOBAL_DATA_DIR_CACHE: Path | None = None


def get_global_data_dir() -> Path:
    global _GLOBAL_DATA_DIR_CACHE
    if _GLOBAL_DATA_DIR_CACHE is not None:
        return _GLOBAL_DATA_DIR_CACHE

    target_dir = Path.home() / ".pycat"
    target_dir.mkdir(parents=True, exist_ok=True)
    _GLOBAL_DATA_DIR_CACHE = target_dir
    return target_dir


def get_global_subdir(name: str, *, data_dir: str | Path | None = None) -> Path:
    return (Path(data_dir) if data_dir else get_global_data_dir()) / str(name or "").strip()


def _get_app_data_dir(*, data_dir: str | Path | None = None) -> Path:
    return Path(data_dir) if data_dir is not None else get_global_data_dir()


def get_settings_path(*, data_dir: str | Path | None = None) -> Path:
    return _get_app_data_dir(data_dir=data_dir) / "settings.json"


def get_user_modes_json_path(*, data_dir: str | Path | None = None) -> Path:
    """User-level modes config path.

    Stored next to settings.json so modes are global across all projects.
    """
    return _get_app_data_dir(data_dir=data_dir) / "modes.json"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> bool:
    """Write JSON beside the target and atomically replace it."""
    try:
        atomic_write_text(path, json.dumps(data or {}, ensure_ascii=False, indent=2) + "\n")
        return True
    except Exception as exc:
        logger.debug("Failed to atomically write %s: %s", path, exc)
        return False


def load_user_modes_dict(*, data_dir: str | Path | None = None) -> Dict[str, Any]:
    path = get_user_modes_json_path(data_dir=data_dir)
    try:
        if path.exists() and path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            migrated = migrate_modes_payload(raw)
            if raw != migrated:
                _atomic_write_json(path, migrated)
            return migrated
    except Exception:
        return {}
    return {"schema_version": SCHEMA_VERSION, "modes": []}


def save_user_modes_dict(data: Dict[str, Any], *, data_dir: str | Path | None = None) -> bool:
    path = get_user_modes_json_path(data_dir=data_dir)
    return _atomic_write_json(path, migrate_modes_payload(data))


def load_settings_dict(*, data_dir: str | Path | None = None) -> Dict[str, Any]:
    path = get_settings_path(data_dir=data_dir)
    try:
        if path.exists() and path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            migrated, profiles = migrate_settings_payload(raw)
            modes = load_user_modes_dict(data_dir=data_dir)
            migrated, migrated_modes = restore_migrated_capabilities_from_modes(migrated, modes)
            if modes != migrated_modes:
                _atomic_write_json(get_user_modes_json_path(data_dir=data_dir), migrated_modes)
            if raw != migrated:
                _atomic_write_json(path, migrated)
            return migrated
    except Exception:
        return {}
    return {"schema_version": SCHEMA_VERSION}


def save_settings_dict(settings: Dict[str, Any], *, data_dir: str | Path | None = None) -> bool:
    path = get_settings_path(data_dir=data_dir)
    payload, profiles = migrate_settings_payload(settings)
    modes = load_user_modes_dict(data_dir=data_dir)
    payload, migrated_modes = restore_migrated_capabilities_from_modes(payload, modes)
    if modes != migrated_modes:
        _atomic_write_json(get_user_modes_json_path(data_dir=data_dir), migrated_modes)
    return _atomic_write_json(path, payload)


def load_app_config(*, refresh: bool = False) -> AppConfig:
    global _APP_CACHE
    if refresh or _APP_CACHE is None:
        _APP_CACHE = AppConfig.from_dict(load_settings_dict())
    return _APP_CACHE


def set_cached_app_config(app_config: AppConfig | None) -> None:
    global _APP_CACHE
    _APP_CACHE = app_config


def set_cached_settings_dict(settings: Dict[str, Any] | None) -> None:
    set_cached_app_config(AppConfig.from_dict(settings or {}))


def save_app_config(app_config: AppConfig, *, refresh_cache: bool = True) -> bool:
    ok = save_settings_dict(app_config.to_dict())
    if refresh_cache and ok:
        set_cached_app_config(app_config)
    return ok


def get_modes_json_path(work_dir: str) -> Optional[Path]:
    wd = (work_dir or "").strip()
    if not wd or wd.startswith("ssh://"):
        return None
    try:
        p = Path(wd)
    except Exception:
        return None
    if not p.exists() or not p.is_dir():
        return None
    return p / "modes.json"


def load_project_config(work_dir: str) -> ProjectConfig:
    p = get_modes_json_path(work_dir)
    if not p or not p.exists() or not p.is_file():
        return ProjectConfig(work_dir=str(work_dir or ""), modes=[])

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        raw = None
    migrated = migrate_modes_payload(raw)
    if raw != migrated:
        _atomic_write_json(p, migrated)
    return ProjectConfig.from_modes_json(str(work_dir or ""), migrated)


def save_project_config(project: ProjectConfig) -> bool:
    p = get_modes_json_path(project.work_dir)
    if not p:
        return False

    try:
        return _atomic_write_json(p, migrate_modes_payload(project.to_modes_json()))
    except Exception:
        return False
