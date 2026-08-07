from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.content.markdown import extract_markdown_links, extract_title_and_preview, strip_frontmatter, with_frontmatter
from core.config import get_global_subdir
from models.contracts.session_state import (
    MEMORY_CATEGORIES,
    MEMORY_CONTENT_LIMIT,
    MEMORY_TOPIC_CONTENT_LIMIT,
    MEMORY_SCOPES,
    MemoryCandidate,
    MemoryRecord,
    SessionState,
)


@dataclass(frozen=True)
class MemorySnippet:
    key: str
    value: str
    score: int
    source: str = "memory"
    mtime: float = 0.0
    scope: str = "session"
    read_key: str = ""

    @property
    def freshness_label(self) -> str:
        if self.mtime <= 0:
            return ""
        try:
            return datetime.fromtimestamp(self.mtime).astimezone().isoformat(timespec="seconds")
        except Exception:
            return ""


class MemoryService:
    ENTRYPOINT_NAME = "MEMORY.md"
    GLOBAL_ENTRYPOINT_NAME = "SOUL.md"
    MEMORY_FILE_PREFIX = "memory__"
    SOURCE_OPTIONS = ("session", "workspace", "global")
    TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", re.UNICODE)
    SESSION_MEMORY_VALUE_CAP = 520
    FILE_MEMORY_VALUE_CAP = 900

    @classmethod
    def list_memory_entries(
        cls,
        state: SessionState,
        *,
        scope: str,
        work_dir: str | None = None,
    ) -> list[dict[str, str]]:
        scope = cls._normalize_scope(scope)
        if scope == "session":
            return [{"key": key, "path": "", "updated": ""} for key in sorted(state.memory, key=str.lower)]
        root = cls.ensure_memory_dir(scope, work_dir=work_dir)
        if root is None:
            return []
        try:
            files = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}]
        except Exception:
            return []
        entries = []
        for path in sorted(files, key=lambda item: item.as_posix().lower()):
            try:
                updated = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                relative = path.relative_to(root).as_posix()
            except Exception:
                updated = ""
                relative = path.name
            entries.append({"key": cls._key_from_memory_file(path), "path": relative, "updated": updated})
        return entries

    @classmethod
    def read_memory_entry(
        cls,
        state: SessionState,
        *,
        scope: str,
        key: str,
        work_dir: str | None = None,
    ) -> str | None:
        scope = cls._normalize_scope(scope)
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None
        if scope == "session":
            value = state.memory.get(normalized_key)
            if value is None:
                return None
            return (value if isinstance(value, MemoryRecord) else MemoryRecord.from_dict(normalized_key, value)).content
        path = cls._resolve_memory_file(scope, normalized_key, work_dir=work_dir)
        if path is None or not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    @classmethod
    def write_memory_entry(
        cls,
        state: SessionState,
        *,
        scope: str,
        key: str,
        content: str,
        work_dir: str | None = None,
        current_seq: int = 0,
        category: str = "fact",
        refs: list[Any] | None = None,
    ) -> str:
        scope = cls._normalize_scope(scope)
        key = str(key or "").strip()
        content = str(content or "").strip()
        if not key:
            return "Memory key is required."
        if not content:
            return "Memory content is required."
        limit = MEMORY_CONTENT_LIMIT if scope == "session" else MEMORY_TOPIC_CONTENT_LIMIT
        if len(content) > limit:
            return f"Memory content is too long ({len(content)} chars; limit={limit}). Use state__artifact for long material."
        category = cls._normalize_category(category)
        if scope == "session":
            state.memory[key] = MemoryRecord(
                key=key,
                content=content,
                scope=scope,
                category=category,
                refs=cls._merge_values(refs or [], limit=12),
                updated_seq=int(current_seq or 0),
            )
            return f"Saved session memory: {key}"

        path = cls._resolve_memory_file(scope, key, work_dir=work_dir, create_dir=True)
        if path is None:
            return f"Failed to resolve {scope} memory path."
        title = key if key.lower().endswith((".md", ".markdown", ".txt")) else cls._display_title_from_key(key)
        markdown = with_frontmatter(
            f"# {title}\n\n{content}\n",
            {
                "updated": cls._timestamp(),
                "scope": scope,
                "category": category,
                "refs": cls._merge_values(refs or [], limit=12),
            },
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            cls._ensure_index_link(scope, path, title, work_dir=work_dir)
            return f"Wrote {scope} memory: {key}"
        except Exception as exc:
            return f"Failed to write {scope} memory {key}: {exc}"

    @classmethod
    def delete_memory_entry(
        cls,
        state: SessionState,
        *,
        scope: str,
        key: str,
        work_dir: str | None = None,
    ) -> str:
        scope = cls._normalize_scope(scope)
        key = str(key or "").strip()
        if not key:
            return "Memory key is required."
        if scope == "session":
            if key not in state.memory:
                return f"No session memory to delete: {key}"
            del state.memory[key]
            return f"Deleted session memory: {key}"
        path = cls._resolve_memory_file(scope, key, work_dir=work_dir)
        if path is None or not path.exists():
            return f"No {scope} memory to delete: {key}"
        try:
            path.unlink()
            cls._remove_index_link(scope, path, work_dir=work_dir)
            return f"Deleted {scope} memory: {key}"
        except Exception as exc:
            return f"Failed to delete {scope} memory {key}: {exc}"

    @classmethod
    def list_memory_candidates(cls, state: SessionState) -> list[dict[str, Any]]:
        candidates = [
            value if isinstance(value, MemoryCandidate) else MemoryCandidate.from_dict(str(key), value)
            for key, value in (state.memory_candidates or {}).items()
        ]
        pending = [item for item in candidates if item.status == "pending" and item.content]
        pending.sort(key=lambda item: (int(item.created_seq or 0), item.id))
        return [item.to_dict() for item in pending[:20]]

    @classmethod
    def add_memory_candidates(
        cls,
        state: SessionState,
        candidates: list[Any],
        *,
        current_seq: int = 0,
    ) -> int:
        seq = int(current_seq or 0)
        known_contents = {
            cls._normalize_candidate_content(record.content)
            for key, value in (state.memory or {}).items()
            for record in [value if isinstance(value, MemoryRecord) else MemoryRecord.from_dict(str(key), value)]
            if record.content
        }
        added = 0
        changed = False
        for raw in candidates or []:
            candidate = cls._normalize_candidate(raw)
            content = str(candidate.get("content") or "").strip()
            normalized = cls._normalize_candidate_content(content)
            if not normalized or normalized in known_contents:
                continue
            candidate_id = cls.memory_candidate_id(content)
            existing_raw = (state.memory_candidates or {}).get(candidate_id)
            if existing_raw is not None:
                existing = (
                    existing_raw
                    if isinstance(existing_raw, MemoryCandidate)
                    else MemoryCandidate.from_dict(candidate_id, existing_raw)
                )
                if existing.status != "pending":
                    continue
                refs = cls._merge_values(existing.refs, candidate.get("refs") or [], limit=12)
                reason = existing.reason or str(candidate.get("reason") or "")
                if refs != existing.refs or reason != existing.reason:
                    existing.refs = refs
                    existing.reason = reason
                    existing.updated_seq = seq
                    state.memory_candidates[candidate_id] = existing
                    changed = True
                continue
            state.memory_candidates[candidate_id] = MemoryCandidate(
                id=candidate_id,
                content=content,
                scope=str(candidate.get("scope") or "session"),
                category=str(candidate.get("category") or "fact"),
                reason=str(candidate.get("reason") or ""),
                refs=list(candidate.get("refs") or []),
                created_seq=seq,
                updated_seq=seq,
            )
            known_contents.add(normalized)
            added += 1
            changed = True
        if changed:
            state.last_updated_seq = max(int(state.last_updated_seq or 0), seq)
            state.state_version += 1
        return added

    @classmethod
    def promote_memory_candidate(
        cls,
        state: SessionState,
        *,
        candidate_id: str,
        key: str = "",
        scope: str = "",
        work_dir: str | None = None,
        current_seq: int = 0,
    ) -> str:
        target_id = str(candidate_id or "").strip()
        raw = (state.memory_candidates or {}).get(target_id)
        candidate = raw if isinstance(raw, MemoryCandidate) else (
            MemoryCandidate.from_dict(target_id, raw) if isinstance(raw, dict) else None
        )
        if candidate is None or candidate.status != "pending":
            return f"Memory candidate not found: {target_id or '<empty>'}"
        memory_key = str(key or "").strip() or cls._key_from_candidate(candidate.to_dict())
        target_scope = cls._normalize_scope(scope or candidate.scope)
        message = cls.write_memory_entry(
            state,
            scope=target_scope,
            key=memory_key,
            content=candidate.content,
            work_dir=work_dir,
            current_seq=current_seq,
            category=candidate.category,
            refs=list(candidate.refs),
        )
        if message.startswith("Saved session memory:") or message.startswith("Wrote "):
            candidate.status = "promoted"
            candidate.updated_seq = int(current_seq or 0)
            state.memory_candidates[target_id] = candidate
            state.last_updated_seq = max(int(state.last_updated_seq or 0), int(current_seq or 0))
            state.state_version += 1
        return message

    @classmethod
    def reject_memory_candidate(
        cls,
        state: SessionState,
        *,
        candidate_id: str,
        current_seq: int = 0,
    ) -> str:
        target_id = str(candidate_id or "").strip()
        raw = (state.memory_candidates or {}).get(target_id)
        candidate = raw if isinstance(raw, MemoryCandidate) else (
            MemoryCandidate.from_dict(target_id, raw) if isinstance(raw, dict) else None
        )
        if candidate is None:
            return f"Memory candidate not found: {target_id or '<empty>'}"
        candidate.status = "rejected"
        candidate.updated_seq = int(current_seq or 0)
        state.memory_candidates[target_id] = candidate
        state.last_updated_seq = max(int(state.last_updated_seq or 0), int(current_seq or 0))
        state.state_version += 1
        return f"Rejected memory candidate: {target_id}"

    @staticmethod
    def memory_candidate_id(content: str) -> str:
        import hashlib

        normalized = MemoryService._normalize_candidate_content(content)
        digest = hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"memcand-{digest}"

    @staticmethod
    def handle_updates(state: SessionState, updates: Dict[str, Any], current_seq: int) -> List[str]:
        feedback: list[str] = []
        for key, value in updates.items():
            if value is None:
                if key in state.memory:
                    del state.memory[key]
                    feedback.append(f"Deleted session memory: {key}")
                continue
            record = value if isinstance(value, MemoryRecord) else MemoryRecord.from_dict(str(key), value)
            record.updated_seq = int(current_seq or record.updated_seq or 0)
            state.memory[str(key)] = record
            feedback.append(f"Saved session memory: {key}")
        return feedback

    @classmethod
    def select_relevant(
        cls,
        state: SessionState,
        query: str,
        *,
        limit: int = 6,
        work_dir: str | None = None,
        sources: Any = None,
    ) -> List[MemorySnippet]:
        tokens = cls._tokens(query)
        snippets: list[MemorySnippet] = []
        selected = cls._normalize_sources(sources)
        if "session" in selected:
            for key, value in state.memory.items():
                record = value if isinstance(value, MemoryRecord) else MemoryRecord.from_dict(str(key), value)
                score = cls._score(f"{key} {record.content} {record.category}", tokens)
                if not tokens:
                    score = 1
                if score > 0:
                    snippets.append(MemorySnippet(
                        key=str(key),
                        value=cls._memory_value_for_prompt(
                            key=str(key), value=f"[{record.category}] {record.content}", cap=cls.SESSION_MEMORY_VALUE_CAP,
                            scope="session", read_key=str(key),
                        ),
                        score=score,
                        source=record.category,
                        scope="session",
                        read_key=str(key),
                    ))
        if "workspace" in selected:
            snippets.extend(cls._relevant_files(cls.load_workspace_memory(work_dir), tokens, "workspace"))
        if "global" in selected:
            snippets.extend(cls._relevant_files(cls.load_global_memory(), tokens, "global"))
        snippets.sort(key=lambda item: (-item.score, item.source, item.key.lower()))
        return snippets[: max(0, int(limit or 0))]

    @classmethod
    def _relevant_files(cls, items: list[MemorySnippet], tokens: set[str], scope: str) -> list[MemorySnippet]:
        result = []
        for item in items:
            score = cls._score(f"{item.key} {item.value}", tokens)
            if tokens and score <= 0:
                continue
            result.append(MemorySnippet(
                key=item.key,
                value=cls._memory_value_for_prompt(
                    key=item.key, value=item.value, cap=cls.FILE_MEMORY_VALUE_CAP,
                    source=item.source, scope=scope, read_key=item.read_key or item.key,
                ),
                score=score or 1,
                source=item.source,
                mtime=item.mtime,
                scope=scope,
                read_key=item.read_key,
            ))
        return result

    @classmethod
    def build_prompt_section(
        cls,
        state: SessionState,
        query: str,
        *,
        limit: int = 6,
        max_chars: int = 2400,
        work_dir: str | None = None,
        sources: Any = None,
    ) -> str:
        selected = cls._normalize_sources(sources)
        snippets = cls.select_relevant(state, query, limit=limit, work_dir=work_dir, sources=selected)
        if not snippets:
            return ""
        lines = [f"<relevant_memory sources=\"{', '.join(selected)}\">"]
        for item in snippets:
            updated = f" updated={item.freshness_label}" if item.freshness_label else ""
            lines.append(f"- [{item.source}] {item.key}{updated}: {item.value}")
        lines.append("</relevant_memory>")
        return cls._trim("\n".join(lines), max_chars)

    @classmethod
    def load_workspace_memory(cls, work_dir: str | None, *, limit: int = 20, max_file_chars: int = 12_000) -> list[MemorySnippet]:
        root = cls._workspace_memory_dir(work_dir)
        return cls._load_from_root(root, cls.ENTRYPOINT_NAME, limit=limit, max_file_chars=max_file_chars, prefix_only=False)

    @classmethod
    def load_global_memory(cls, *, limit: int = 20, max_file_chars: int = 12_000) -> list[MemorySnippet]:
        root = cls._global_memory_dir()
        return cls._load_from_root(root, cls.GLOBAL_ENTRYPOINT_NAME, limit=limit, max_file_chars=max_file_chars, prefix_only=True)

    @classmethod
    def _load_from_root(
        cls,
        root: Path | None,
        entrypoint_name: str,
        *,
        limit: int,
        max_file_chars: int,
        prefix_only: bool,
    ) -> list[MemorySnippet]:
        if root is None or limit <= 0:
            return []
        entrypoint = root / entrypoint_name
        if entrypoint.is_file():
            paths = [entrypoint] + cls._linked_files(entrypoint, root, max_file_chars=max_file_chars)
        else:
            try:
                paths = [
                    path for path in root.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in {".md", ".markdown", ".txt"}
                    and (not prefix_only or path.name.startswith(cls.MEMORY_FILE_PREFIX))
                ]
            except Exception:
                paths = []
            paths.sort(key=lambda path: path.name.lower())
        snippets = []
        for path in paths[:limit]:
            snippet = cls._load_file(path, root=root, max_file_chars=max_file_chars)
            if snippet is not None:
                snippets.append(snippet)
        return snippets

    @classmethod
    def _load_file(cls, path: Path, *, root: Path, max_file_chars: int) -> MemorySnippet | None:
        try:
            stat = path.stat()
            raw = path.read_text(encoding="utf-8", errors="replace")[: max_file_chars + 1]
            relative = path.relative_to(root).as_posix()
        except Exception:
            return None
        content = strip_frontmatter(raw)
        title, preview = extract_title_and_preview(path, content)
        if not preview:
            return None
        return MemorySnippet(
            key=f"{relative} / {title}",
            value=cls._trim(preview, max_file_chars),
            score=1,
            source="memory-file",
            mtime=float(stat.st_mtime or 0.0),
            read_key=relative,
        )

    @classmethod
    def _linked_files(cls, entrypoint: Path, root: Path, *, max_file_chars: int) -> list[Path]:
        try:
            content = strip_frontmatter(entrypoint.read_text(encoding="utf-8", errors="replace")[:max_file_chars])
            resolved_root = root.resolve()
        except Exception:
            return []
        result: list[Path] = []
        seen = {entrypoint.resolve()}
        for target in extract_markdown_links(content):
            try:
                path = (resolved_root / target).resolve()
            except Exception:
                continue
            if path in seen or (path != resolved_root and resolved_root not in path.parents):
                continue
            if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}:
                seen.add(path)
                result.append(path)
        return result

    @classmethod
    def ensure_memory_dir(cls, scope: str, *, work_dir: str | None = None) -> Path | None:
        scope = cls._normalize_scope(scope)
        if scope == "workspace":
            try:
                root = Path(str(work_dir or "")).expanduser().resolve()
            except Exception:
                return None
            if not root.is_dir():
                return None
            target = root / ".pycat" / "memory"
        elif scope == "global":
            target = get_global_subdir("memory")
        else:
            return None
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target
        except Exception:
            return None

    @classmethod
    def _workspace_memory_dir(cls, work_dir: str | None) -> Path | None:
        try:
            path = Path(str(work_dir or "")).expanduser().resolve() / ".pycat" / "memory"
        except Exception:
            return None
        return path if path.is_dir() else None

    @classmethod
    def _global_memory_dir(cls) -> Path | None:
        path = get_global_subdir("memory")
        return path if path.is_dir() else None

    @classmethod
    def _resolve_memory_file(
        cls, scope: str, key: str, *, work_dir: str | None = None, create_dir: bool = False,
    ) -> Path | None:
        root = cls.ensure_memory_dir(scope, work_dir=work_dir) if create_dir else (
            cls._workspace_memory_dir(work_dir) if cls._normalize_scope(scope) == "workspace" else cls._global_memory_dir()
        )
        if root is None:
            return None
        raw = str(key or "").strip().replace("\\", "/")
        if not raw:
            return None
        name = raw if raw.endswith((".md", ".markdown", ".txt")) else f"{cls.MEMORY_FILE_PREFIX}{cls._sanitize_memory_key(raw)}.md"
        try:
            path = (root / name).resolve()
            resolved_root = root.resolve()
            return path if path == resolved_root or resolved_root in path.parents else None
        except Exception:
            return None

    @classmethod
    def _ensure_index_link(cls, scope: str, path: Path, title: str, *, work_dir: str | None = None) -> None:
        root = cls.ensure_memory_dir(scope, work_dir=work_dir)
        if root is None:
            return
        index = root / (cls.GLOBAL_ENTRYPOINT_NAME if cls._normalize_scope(scope) == "global" else cls.ENTRYPOINT_NAME)
        content = index.read_text(encoding="utf-8", errors="replace") if index.exists() else "# PyCat Memory\n"
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        if f"]({relative})" not in content:
            index.write_text(content.rstrip() + f"\n- [{title}]({relative})\n", encoding="utf-8")

    @classmethod
    def _remove_index_link(cls, scope: str, path: Path, *, work_dir: str | None = None) -> None:
        root = cls.ensure_memory_dir(scope, work_dir=work_dir)
        if root is None:
            return
        index = root / (cls.GLOBAL_ENTRYPOINT_NAME if cls._normalize_scope(scope) == "global" else cls.ENTRYPOINT_NAME)
        if not index.exists():
            return
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        lines = [line for line in index.read_text(encoding="utf-8", errors="replace").splitlines() if f"]({relative})" not in line]
        index.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    @classmethod
    def _memory_value_for_prompt(
        cls, *, key: str, value: str, cap: int, source: str = "session", scope: str = "session", read_key: str = "",
    ) -> str:
        raw = str(value or "").strip()
        if len(raw) <= max(80, int(cap or 0)):
            return raw
        target = str(read_key or key or "").strip()
        hint = (
            f"... [trimmed; source={source}; chars={len(raw)}; read with "
            f"state__memory(action=\"view\", scope={json.dumps(scope)}, key={json.dumps(target)})]"
        )
        digest = re.sub(r"\s+", " ", raw[: max(40, int(cap) - len(hint) - 1)]).strip()
        return cls._trim(f"{digest} {hint}", max(160, int(cap)))

    @classmethod
    def _normalize_sources(cls, sources: Any) -> tuple[str, ...]:
        if sources is None:
            values = cls.SOURCE_OPTIONS
        elif isinstance(sources, str):
            values = tuple(part.strip().lower() for part in sources.split(","))
        elif isinstance(sources, (list, tuple, set)):
            values = tuple(str(item).strip().lower() for item in sources)
        else:
            values = ()
        return tuple(dict.fromkeys(item for item in values if item in cls.SOURCE_OPTIONS))

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        value = str(scope or "session").strip().lower()
        return value if value in MemoryService.SOURCE_OPTIONS else "session"

    @staticmethod
    def _normalize_category(category: Any) -> str:
        value = str(category or "fact").strip().lower()
        return value if value in MEMORY_CATEGORIES else "fact"

    @staticmethod
    def _normalize_candidate(item: Any) -> dict[str, Any]:
        if isinstance(item, MemoryCandidate):
            item = item.to_dict()
        payload = item if isinstance(item, dict) else {"content": item}
        content = str(payload.get("content") or "").strip()
        scope = str(payload.get("scope") or "session").strip().lower()
        category = str(payload.get("category") or "fact").strip().lower()
        return {
            "content": content[:MEMORY_CONTENT_LIMIT],
            "scope": scope if scope in MEMORY_SCOPES else "session",
            "category": category if category in MEMORY_CATEGORIES else "fact",
            "reason": str(payload.get("reason") or "").strip()[:MEMORY_CONTENT_LIMIT],
            "refs": MemoryService._merge_values(
                payload.get("refs") or [],
                limit=12,
            ),
        }

    @staticmethod
    def _normalize_candidate_content(content: Any) -> str:
        return " ".join(str(content or "").casefold().split())

    @staticmethod
    def _merge_values(*groups: Any, limit: int = 12) -> list[str]:
        merged: list[str] = []
        for group in groups:
            for item in group or []:
                value = str(item or "").strip()
                if value and value not in merged:
                    merged.append(value)
                if len(merged) >= limit:
                    return merged
        return merged

    @staticmethod
    def _key_from_candidate(candidate: dict[str, Any]) -> str:
        words = re.findall(r"[\w\u4e00-\u9fff]+", str(candidate.get("content") or "memory").lower(), flags=re.UNICODE)
        return (".".join(words[:6]) or "memory")[:80]

    @classmethod
    def _tokens(cls, text: str) -> set[str]:
        result: set[str] = set()
        for token in cls.TOKEN_RE.findall(str(text or "")):
            token = token.strip().lower()
            if len(token) < 2:
                continue
            result.add(token)
            if all("\u4e00" <= char <= "\u9fff" for char in token):
                result.update(token[index:index + 2] for index in range(len(token) - 1))
        return result

    @classmethod
    def _score(cls, text: str, query_tokens: set[str]) -> int:
        if not query_tokens:
            return 0
        tokens = cls._tokens(text)
        lowered = str(text or "").lower()
        return len(tokens & query_tokens) * 4 + sum(1 for token in query_tokens if token in lowered)

    @staticmethod
    def _key_from_memory_file(path: Path) -> str:
        stem = path.stem
        return stem[len(MemoryService.MEMORY_FILE_PREFIX):] if stem.startswith(MemoryService.MEMORY_FILE_PREFIX) else path.name

    @staticmethod
    def _display_title_from_key(key: str) -> str:
        return str(key or "memory").strip().replace("_", " ").replace("-", " ").title()

    @staticmethod
    def _sanitize_memory_key(key: str) -> str:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(key or ""))
        return safe.strip("._ ") or "memory"

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _trim(text: str, max_chars: int) -> str:
        raw = str(text or "").strip()
        return raw if len(raw) <= max_chars else raw[: max(0, max_chars - 3)].rstrip() + "..."
