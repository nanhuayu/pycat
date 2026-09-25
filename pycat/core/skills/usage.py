"""Root-aware, corruption-safe Skill usage and provenance accounting."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from pycat.core.config import get_global_subdir
from pycat.core.persistence import atomic_write_text, exclusive_file_lock

USAGE_FILE_NAME = ".usage.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def usage_path(root: Path | str | None = None) -> Path:
    """Return the sidecar path for one managed Skill root."""
    base = Path(root) if root is not None else get_global_subdir("skills")
    return base / USAGE_FILE_NAME


class SkillUsageStore:
    """A separate usage ledger per project/global managed root."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        root: Path | str | None = None,
        scope: str = "global",
    ) -> None:
        if path is not None:
            self._path = Path(path)
            self._root = self._path.parent
        else:
            self._root = Path(root) if root is not None else get_global_subdir("skills")
            self._path = usage_path(self._root)
        self._scope = str(scope or "global").strip().lower() or "global"
        self._root_display = str(self._root.expanduser().resolve(strict=False))

    # ------------------------------------------------------------------ read

    def all(self) -> dict[str, dict[str, Any]]:
        try:
            with exclusive_file_lock(self._path):
                data, error = self._load_locked()
        except OSError:
            return {}
        return data if not error and data is not None else {}

    def get(self, name: str) -> dict[str, Any]:
        key = str(name or "").strip().lower()
        return dict(self.all().get(key) or {})

    def is_agent_created(self, name: str) -> bool:
        return self.get(name).get("created_by") == "agent"

    def can_mutate(self) -> tuple[bool, str]:
        """Check readability without creating or repairing a corrupt ledger."""
        try:
            with exclusive_file_lock(self._path):
                _data, error = self._load_locked()
        except OSError as exc:
            return False, f"usage ledger unavailable: {exc}"
        if error:
            return False, error
        return True, ""

    # ----------------------------------------------------------------- write

    def record_created(self, name: str, *, created_by: str = "user") -> bool:
        key = self._key(name)
        if not key:
            return False
        now = _now()

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            record = dict(data.get(key) or {})
            record.setdefault("created_at", now)
            record["created_by"] = str(created_by or "user")
            record.setdefault("state", "active")
            record["scope"] = self._scope
            record["root"] = self._root_display
            record["last_activity_at"] = now
            data[key] = record

        return self._mutate(mutate)

    def record_patched(self, name: str, *, by: str = "user") -> bool:
        key = self._key(name)
        if not key:
            return False
        now = _now()

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            record = dict(data.get(key) or {})
            record.setdefault("created_at", now)
            record.setdefault("created_by", str(by or "user"))
            record["scope"] = self._scope
            record["root"] = self._root_display
            record["patch_count"] = self._positive_int(record.get("patch_count")) + 1
            record["last_patched_at"] = now
            record["last_activity_at"] = now
            record["state"] = "active"
            data[key] = record

        return self._mutate(mutate)

    def record_loaded(self, name: str) -> bool:
        """Record one successful ``skill__load`` as view and use together."""
        key = self._key(name)
        if not key:
            return False
        now = _now()

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            record = dict(data.get(key) or {})
            record["scope"] = self._scope
            record["root"] = self._root_display
            record["load_count"] = self._positive_int(record.get("load_count")) + 1
            record["view_count"] = self._positive_int(record.get("view_count")) + 1
            record["use_count"] = self._positive_int(record.get("use_count")) + 1
            record["last_view_at"] = now
            record["last_used_at"] = now
            record["last_activity_at"] = now
            record["state"] = "active"
            data[key] = record

        return self._mutate(mutate)

    # ------------------------------------------------------------- internals

    @staticmethod
    def _positive_int(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _key(name: str) -> str:
        return str(name or "").strip().lower()

    def _load_locked(self) -> tuple[dict[str, dict[str, Any]] | None, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, ""
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"usage ledger cannot be read: {exc}"
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return None, f"usage ledger contains invalid JSON: {exc}"
        if not isinstance(payload, Mapping):
            return None, "usage ledger root must be an object"
        records: dict[str, dict[str, Any]] = {}
        for name, record in payload.items():
            if isinstance(record, Mapping):
                records[str(name).strip().lower()] = dict(record)
        return records, ""

    def _mutate(self, callback: Callable[[dict[str, dict[str, Any]]], None]) -> bool:
        try:
            with exclusive_file_lock(self._path):
                data, error = self._load_locked()
                if error or data is None:
                    return False
                callback(data)
                self._write_locked(data)
                return True
        except (OSError, TypeError, ValueError):
            return False

    def _write_locked(self, data: Mapping[str, Mapping[str, Any]]) -> None:
        atomic_write_text(
            self._path,
            json.dumps(dict(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


__all__ = ["SkillUsageStore", "USAGE_FILE_NAME", "usage_path"]
