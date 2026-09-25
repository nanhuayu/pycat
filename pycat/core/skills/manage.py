"""Validated writes for project/global managed Skills."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from pycat.core.config import get_global_subdir
from pycat.core.persistence import atomic_write_bytes, atomic_write_text, exclusive_file_lock
from pycat.core.security.threats import first_threat_message
from pycat.core.skills.usage import SkillUsageStore
from pycat.models.session_paths import resolve_project_data_root

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
MAX_CONTENT_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 200
MANAGED_SCOPES = ("project", "global")

DEFAULT_TEMPLATE = """---
name: {name}
description: {description}
---

# {name}

{content}
"""


def managed_root(scope: str, work_dir: str = "", *, data_dir: str | Path | None = None) -> Path:
    """Resolve one and only one PyCat-managed Skill root."""
    normalized = str(scope or "").strip().lower()
    if normalized == "global":
        return get_global_subdir("skills", data_dir=data_dir)
    if normalized == "project":
        clean_work_dir = str(work_dir or "").strip()
        if not clean_work_dir:
            raise ValueError("project scope requires an active workspace")
        return resolve_project_data_root(clean_work_dir, data_dir=data_dir) / "skills"
    raise ValueError("skill scope must be project or global")


def validate_skill_payload(name: str, description: str, content: str) -> str | None:
    """Return an error message, or None when the payload can be persisted."""
    normalized = str(name or "").strip().lower()
    if not SKILL_NAME_RE.fullmatch(normalized):
        return f"invalid skill name {name!r} (lowercase letters/digits/hyphens, 3-64 chars)"
    if not str(description or "").strip():
        return "description is required"
    if len(str(description)) > MAX_DESCRIPTION_CHARS:
        return f"description exceeds {MAX_DESCRIPTION_CHARS} chars"
    if not str(content or "").strip():
        return "content is required"
    if len(str(content)) > MAX_CONTENT_CHARS:
        return f"content exceeds {MAX_CONTENT_CHARS} chars"
    threat = first_threat_message(f"{description}\n{content}", scope="strict")
    if threat:
        return threat
    return None


def build_skill_markdown(name: str, description: str, content: str) -> str:
    return DEFAULT_TEMPLATE.format(
        name=str(name).strip().lower(),
        description=str(description).strip().replace("\n", " "),
        content=str(content).strip(),
    )


def upsert_skill(
    name: str,
    *,
    description: str,
    content: str,
    work_dir: str = "",
    scope: str = "global",
    create_only: bool = False,
    created_by: str = "user",
    data_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """Create or replace a managed ``SKILL.md`` with provenance accounting."""
    normalized = str(name or "").strip().lower()
    error = validate_skill_payload(normalized, description, content)
    if error:
        return False, error
    try:
        root = managed_root(scope, work_dir, data_dir=data_dir)
    except ValueError as exc:
        return False, str(exc)
    normalized_scope = str(scope or "").strip().lower()
    skill_file = root / normalized / "SKILL.md"
    skill_dir = root / normalized
    if is_link_or_reparse(skill_dir) or is_link_or_reparse(skill_file):
        return False, "skill directory cannot be a symbolic link or junction"
    if create_only and skill_file.exists():
        return False, f"skill {normalized!r} already exists"
    usage = SkillUsageStore(root=root, scope=normalized_scope)
    can_mutate, reason = usage.can_mutate()
    if not can_mutate:
        return False, reason
    try:
        old_bytes = skill_file.read_bytes() if skill_file.exists() else None
    except OSError as exc:
        return False, f"skill source cannot be read: {exc}"
    existed = old_bytes is not None
    payload = build_skill_markdown(normalized, description, content)
    with exclusive_file_lock(skill_file):
        try:
            atomic_write_text(skill_file, payload)
            accounted = (
                usage.record_patched(normalized, by=created_by)
                if existed
                else usage.record_created(normalized, created_by=created_by)
            )
            if not accounted:
                raise OSError("usage ledger rejected the provenance update")
        except (OSError, ValueError, TypeError) as exc:
            _restore_file(skill_file, old_bytes)
            return False, f"skill write rolled back: {exc}"
    return True, f"skill {normalized!r} {'updated' if existed else 'created'}"


def write_skill_resource(
    name: str,
    relative_path: str,
    content: str,
    *,
    work_dir: str = "",
    scope: str = "global",
    remove: bool = False,
    data_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """Write or remove a resource inside one existing managed Skill."""
    normalized = str(name or "").strip().lower()
    if not SKILL_NAME_RE.fullmatch(normalized):
        return False, f"invalid skill name {name!r}"
    raw_relative = str(relative_path or "").strip().replace("\\", "/")
    if not raw_relative or raw_relative.startswith(("/", ".")) or ".." in raw_relative.split("/"):
        return False, "relative path must stay inside the skill directory"
    if raw_relative.lower() == "skill.md":
        return False, "use patch to update SKILL.md"
    if len(str(content or "")) > MAX_CONTENT_CHARS:
        return False, f"content exceeds {MAX_CONTENT_CHARS} chars"
    if not remove:
        threat = first_threat_message(str(content or ""), scope="strict")
        if threat:
            return False, threat
    try:
        root = managed_root(scope, work_dir, data_dir=data_dir)
    except ValueError as exc:
        return False, str(exc)
    normalized_scope = str(scope or "").strip().lower()
    skill_dir = root / normalized
    skill_file = skill_dir / "SKILL.md"
    if is_link_or_reparse(skill_dir) or is_link_or_reparse(skill_file):
        return False, "skill directory cannot be a symbolic link or junction"
    if not skill_file.exists():
        return False, f"skill {normalized!r} not found"
    target = skill_dir / raw_relative
    if is_link_or_reparse(target):
        return False, "resource path cannot be a symbolic link or junction"
    try:
        target.resolve(strict=False).relative_to(skill_dir.resolve(strict=False))
    except ValueError:
        return False, "relative path must stay inside the skill directory"
    usage = SkillUsageStore(root=root, scope=normalized_scope)
    can_mutate, reason = usage.can_mutate()
    if not can_mutate:
        return False, reason
    try:
        old_bytes = target.read_bytes() if target.exists() else None
    except OSError as exc:
        return False, f"resource cannot be read: {exc}"
    with exclusive_file_lock(target):
        try:
            if remove:
                if target.exists():
                    target.unlink()
                accounted = usage.record_patched(normalized, by="user")
                note = f"removed {raw_relative}"
            else:
                atomic_write_text(target, str(content))
                accounted = usage.record_patched(normalized, by="user")
                note = f"wrote {raw_relative}"
            if not accounted:
                raise OSError("usage ledger rejected the resource update")
        except (OSError, ValueError, TypeError) as exc:
            _restore_file(target, old_bytes)
            return False, f"resource write rolled back: {exc}"
    return True, note


def is_agent_created(name: str, *, work_dir: str = "", scope: str = "global", data_dir: str | Path | None = None) -> bool:
    try:
        root = managed_root(scope, work_dir, data_dir=data_dir)
    except ValueError:
        return False
    return SkillUsageStore(root=root, scope=str(scope or "").strip().lower()).is_agent_created(name)


def is_link_or_reparse(path: Path) -> bool:
    """Detect symlinks and Windows junction/reparse points without following them."""
    target = Path(path)
    try:
        if target.is_symlink():
            return True
        is_junction = getattr(target, "is_junction", None)
        if callable(is_junction) and bool(is_junction()):
            return True
        attributes = int(
            getattr(target.stat(follow_symlinks=False), "st_file_attributes", 0) or 0
        )
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        return bool(reparse_flag and attributes & reparse_flag)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _restore_file(path: Path, old_bytes: bytes | None) -> None:
    try:
        if old_bytes is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(path, old_bytes)
    except OSError:
        pass


__all__ = [
    "MANAGED_SCOPES",
    "MAX_CONTENT_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "SKILL_NAME_RE",
    "build_skill_markdown",
    "is_agent_created",
    "is_link_or_reparse",
    "managed_root",
    "upsert_skill",
    "validate_skill_payload",
    "write_skill_resource",
]
