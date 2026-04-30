"""Skill system — load reusable skill directories plus declared invocation metadata.

Skills are discovered from:
- ``~/.PyCat/skills/``
- ``.pycat/skills/``

Each skill must live in its own directory with ``SKILL.md`` as the entrypoint.
Explicit ``/{skill}`` invocation is the only skill execution entrypoint.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.config import get_global_subdir
from core.tools.mcp.naming import build_mcp_tool_name, tool_names_match

logger = logging.getLogger(__name__)


COMMAND_TOOL_NAMES: Tuple[str, ...] = (
    "execute_command",
    "shell_start",
    "shell_status",
    "shell_logs",
    "shell_wait",
    "shell_kill",
)


@dataclass
class Skill:
    """A loaded skill definition."""

    name: str
    content: str
    source: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillInvocationSpec:
    """Declared runtime contract for an explicit skill run."""

    mode: str = "agent"
    executor: str = "instruction"
    execution_mode: str = "inline"
    user_invocable: bool = True
    disable_model_invocation: bool = False
    enable_mcp: bool = False
    enable_search: bool = False
    declared_tools: Tuple[str, ...] = ()
    preferred_cli: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillExecutionCheck:
    """Whether a skill can execute with the currently exposed concrete tools."""

    executable: bool
    concrete_tools: Tuple[str, ...] = ()
    reason: str = ""
    missing_tools: Tuple[str, ...] = ()


class SkillsManager:
    """Discover and load skills from configured directories."""

    def __init__(self, work_dir: str = ".") -> None:
        self._work_dir = work_dir
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
        return _resolve_skill_root(Path(skill.source))

    def list_resources(self, name: str) -> List[str]:
        skill = self.get(name)
        if skill is None:
            return []
        return _list_skill_resource_paths(skill)

    def resolve_resource_path(self, name: str, relative_path: str) -> Optional[Path]:
        skill = self.get(name)
        if skill is None:
            return None
        return _resolve_skill_resource_path(skill, relative_path)

    def read_resource(
        self,
        name: str,
        relative_path: str,
        *,
        start_line: int = 1,
        end_line: Optional[int] = None,
    ) -> Optional[Tuple[str, int, int, int]]:
        path = self.resolve_resource_path(name, relative_path)
        if path is None or not path.is_file():
            return None

        text = _read_text_with_fallback(path)
        lines = text.splitlines()
        total_lines = len(lines)
        if total_lines == 0:
            return ("", 0, 0, 0)

        safe_start = max(1, int(start_line or 1))
        safe_end = total_lines if end_line is None else min(total_lines, max(safe_start, int(end_line or safe_start)))
        snippet = "\n".join(lines[safe_start - 1 : safe_end])
        return snippet, total_lines, safe_start, safe_end

    def _skill_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        root = Path(self._work_dir).resolve()

        project_dir = root / ".pycat" / "skills"
        if project_dir.is_dir():
            dirs.append(project_dir)

        global_dir = get_global_subdir("skills")
        if global_dir.is_dir():
            dirs.append(global_dir)

        return dirs

    def _load_from_dir(self, directory: Path) -> None:
        try:
            for entry in sorted(directory.iterdir()):
                skill = None
                try:
                    skill = self._load_skill_entry(entry)
                except Exception as exc:
                    logger.warning("Failed to load skill %s: %s", entry, exc)
                if not skill or skill.name in self._skills:
                    continue
                self._skills[skill.name] = skill
        except Exception as exc:
            logger.warning("Failed to scan skill directory %s: %s", directory, exc)

    def _load_skill_entry(self, entry: Path) -> Optional[Skill]:
        if entry.is_dir():
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                return None
            return self._load_skill_file(skill_file, default_name=entry.name)
        return None

    def _load_skill_file(self, path: Path, *, default_name: str) -> Optional[Skill]:
        content = path.read_text(encoding="utf-8")
        metadata, body = self._parse_frontmatter(content)
        raw_name = str(metadata.get("name") or default_name or "").strip().lower()
        if not raw_name:
            return None
        tags = self._extract_tags(body)
        frontmatter_tags = metadata.get("tags")
        if isinstance(frontmatter_tags, list):
            tags = [str(tag).strip() for tag in frontmatter_tags if str(tag).strip()] or tags
        return Skill(
            name=raw_name,
            content=body.strip() or content.strip(),
            source=str(path),
            description=str(metadata.get("description") or "").strip(),
            tags=tags,
            metadata=metadata,
        )

    @staticmethod
    def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
        stripped = content.lstrip()
        if not stripped.startswith("---"):
            return {}, content

        lines = stripped.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, content

        closing_index = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                closing_index = index
                break
        if closing_index is None:
            return {}, content

        raw_meta = lines[1:closing_index]
        body = "\n".join(lines[closing_index + 1 :])
        metadata: Dict[str, Any] = {}
        current_list_key: Optional[str] = None

        for raw_line in raw_meta:
            line = raw_line.rstrip()
            stripped_line = line.strip()
            if current_list_key and stripped_line.startswith("- "):
                metadata.setdefault(current_list_key, []).append(
                    SkillsManager._parse_frontmatter_value(stripped_line[2:].strip())
                )
                continue

            current_list_key = None
            line = stripped_line
            if not line or line.startswith("#") or ":" not in line:
                continue

            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if not value:
                metadata[key] = []
                current_list_key = key
                continue
            metadata[key] = SkillsManager._parse_frontmatter_value(value)

        return metadata, body

    @staticmethod
    def _parse_frontmatter_value(value: str) -> Any:
        raw = str(value or "").strip()
        if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
            raw = raw[1:-1]
        if raw.startswith("[") and raw.endswith("]"):
            return [
                item.strip().strip("'\"")
                for item in raw[1:-1].split(",")
                if item.strip()
            ]
        lowered = raw.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        return raw

    @staticmethod
    def _extract_tags(content: str) -> List[str]:
        for line in content.splitlines()[:10]:
            stripped = line.strip()
            if stripped.lower().startswith("tags:"):
                raw = stripped[5:].strip().strip("[]")
                return [tag.strip() for tag in raw.split(",") if tag.strip()]
        return []


def resolve_skill_invocation_spec(
    skill: Skill,
    *,
    fallback_mode: str = "agent",
) -> SkillInvocationSpec:
    """Resolve the declared invocation contract for an explicit skill run."""
    metadata = dict(getattr(skill, "metadata", {}) or {})
    mode = (
        str(_first_non_empty(metadata.get("mode"), fallback_mode or "agent") or "agent")
        .strip()
        .lower()
        or "agent"
    )

    raw_tools = _coerce_list(
        _first_non_empty(
            metadata.get("tools"),
            metadata.get("allowed-tools"),
            metadata.get("allowed_tools"),
            metadata.get("tool"),
        )
    )
    preferred_cli = tuple(_extract_cli_hints(raw_tools))

    executor = _normalize_executor(
        _first_non_empty(
            metadata.get("executor"),
            metadata.get("executor-type"),
            metadata.get("executor_type"),
        )
    )
    if not executor:
        if preferred_cli:
            executor = "cli"
        elif any(_looks_like_mcp_ref(item) for item in raw_tools):
            executor = "mcp"
        else:
            executor = "instruction"

    execution_mode = (
        "fork"
        if str(
            _first_non_empty(
                metadata.get("context"),
                metadata.get("execution-mode"),
                metadata.get("execution_mode"),
            )
        )
        .strip()
        .lower()
        == "fork"
        else "inline"
    )

    enable_mcp = _coerce_optional_bool(
        _first_non_empty(
            metadata.get("enable-mcp"),
            metadata.get("enable_mcp"),
            metadata.get("mcp"),
        )
    )
    if enable_mcp is None:
        enable_mcp = executor == "mcp"

    enable_search = _coerce_optional_bool(
        _first_non_empty(
            metadata.get("enable-search"),
            metadata.get("enable_search"),
            metadata.get("search"),
        )
    )
    if enable_search is None:
        enable_search = False

    return SkillInvocationSpec(
        mode=mode,
        executor=executor,
        execution_mode=execution_mode,
        user_invocable=_coerce_bool(metadata.get("user-invocable"), default=True),
        disable_model_invocation=_coerce_bool(
            metadata.get("disable-model-invocation"),
            default=False,
        ),
        enable_mcp=bool(enable_mcp),
        enable_search=bool(enable_search),
        declared_tools=tuple(_extract_declared_tool_names(raw_tools, executor=executor)),
        preferred_cli=preferred_cli,
    )


def check_skill_execution_availability(
    skill: Skill,
    tools: Iterable[Dict[str, Any]],
    *,
    fallback_mode: str = "agent",
) -> SkillExecutionCheck:
    """Check whether a skill can execute with the concrete tools in this request."""
    spec = resolve_skill_invocation_spec(skill, fallback_mode=fallback_mode)
    available_tools = _extract_available_tool_names(tools)

    if spec.executor == "instruction":
        return SkillExecutionCheck(
            executable=True,
            reason="This skill injects instructions only and does not require a dedicated executor.",
        )

    if spec.executor == "cli":
        concrete_tools = tuple(
            tool_name for tool_name in COMMAND_TOOL_NAMES if tool_name in available_tools
        )
        if concrete_tools:
            return SkillExecutionCheck(
                executable=True,
                concrete_tools=concrete_tools,
                reason="Shell command tools are available for this CLI-oriented skill.",
            )
        return SkillExecutionCheck(
            executable=False,
            reason="This skill requires shell command tools, but none are available in the current request.",
        )

    if spec.executor == "mcp":
        if not spec.declared_tools:
            return SkillExecutionCheck(
                executable=False,
                reason="This skill declares MCP execution but does not list any concrete MCP tools.",
            )

        concrete_tools: List[str] = []
        missing_tools: List[str] = []
        for declared_tool in spec.declared_tools:
            match = _find_matching_tool_name(declared_tool, available_tools)
            if match:
                concrete_tools.append(match)
            else:
                missing_tools.append(declared_tool)

        if missing_tools:
            return SkillExecutionCheck(
                executable=False,
                concrete_tools=tuple(_dedupe_preserve_order(concrete_tools)),
                missing_tools=tuple(_dedupe_preserve_order(missing_tools)),
                reason=f"Missing declared MCP tools: {', '.join(_dedupe_preserve_order(missing_tools))}",
            )

        return SkillExecutionCheck(
            executable=True,
            concrete_tools=tuple(_dedupe_preserve_order(concrete_tools)),
            reason="All declared MCP tools are available in the current request.",
        )

    return SkillExecutionCheck(
        executable=False,
        reason=f"Unsupported skill executor: {spec.executor}",
    )


def _resolve_skill_root(path: Path) -> Path:
    return path.parent if path.is_file() else path


def _extract_markdown_resource_links(content: str) -> List[str]:
    result: List[str] = []
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", str(content or "")):
        candidate = str(match.group(1) or "").strip()
        if not candidate or "://" in candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("./"):
            candidate = candidate[2:]
        if candidate.startswith("/"):
            continue
        result.append(candidate.replace("\\", "/"))
    return _dedupe_preserve_order(result)


def _list_skill_resource_paths(skill: Skill) -> List[str]:
    root = _resolve_skill_root(Path(skill.source))
    results: List[str] = []

    for relative_path in _extract_markdown_resource_links(skill.content):
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except Exception:
            continue
        if candidate.exists() and candidate.is_file():
            results.append(candidate.relative_to(root).as_posix())

    for directory_name in ("references", "templates", "scripts"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            results.append(path.relative_to(root).as_posix())

    return _dedupe_preserve_order(results)


def _resolve_skill_resource_path(skill: Skill, relative_path: str) -> Optional[Path]:
    root = _resolve_skill_root(Path(skill.source)).resolve()
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return None
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except Exception:
        return None
    return candidate


def _read_text_with_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "mbcs"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_cli_hints(values: Iterable[str]) -> List[str]:
    hints: List[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        lowered = value.lower()
        if lowered.startswith("bash(") and value.endswith(")"):
            inner = value[5:-1].strip()
            inner = inner.replace(":*", "").rstrip("*").strip()
            if inner.endswith(":"):
                inner = inner[:-1].strip()
            if inner:
                hints.append(inner)
    return _dedupe_preserve_order(hints)


def _extract_declared_tool_names(values: Iterable[str], *, executor: str) -> List[str]:
    result: List[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered.startswith("bash(") and value.endswith(")"):
            continue
        if executor != "mcp":
            continue

        if lowered.startswith("mcp:"):
            reference = value[4:].strip()
            if ":" in reference:
                server_name, tool_name = reference.split(":", 1)
                result.append(build_mcp_tool_name(server_name, tool_name))
                continue
            result.append(reference)
            continue

        if value.count(":") == 1 and not lowered.startswith("http"):
            server_name, tool_name = value.split(":", 1)
            result.append(build_mcp_tool_name(server_name, tool_name))
            continue

        result.append(value)
    return _dedupe_preserve_order(result)


def _extract_available_tool_names(tools: Iterable[Dict[str, Any]]) -> Tuple[str, ...]:
    names: List[str] = []
    for tool in tools:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = str(fn.get("name") or "").strip()
        if name:
            names.append(name)
    return tuple(_dedupe_preserve_order(names))


def _find_matching_tool_name(declared_tool: str, available_tools: Iterable[str]) -> str:
    target = str(declared_tool or "").strip()
    if not target:
        return ""
    for available_tool in available_tools:
        if tool_names_match(target, available_tool):
            return available_tool
    return ""


def _normalize_executor(value: Any) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"", "none", "instruction", "instructions", "instruction-only", "instruction_only"}:
        return "instruction" if lowered else ""
    if lowered in {"cli", "shell", "command", "commands", "bash"}:
        return "cli"
    if lowered == "mcp":
        return "mcp"
    return ""


def _looks_like_mcp_ref(value: Any) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered.startswith("mcp__") or lowered.startswith("mcp:")


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        if isinstance(value, list):
            if value:
                return value
            continue
        return value
    return ""


def _coerce_bool(value: Any, *, default: bool) -> bool:
    coerced = _coerce_optional_bool(value)
    if coerced is None:
        return bool(default)
    return coerced


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    return None


def _coerce_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result
