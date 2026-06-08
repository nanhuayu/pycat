from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.content.markdown import extract_markdown_links, extract_title_and_preview, strip_frontmatter, with_frontmatter
from models.state import MEMORY_CATEGORIES, MEMORY_CONTENT_LIMIT, MemoryRecord, SessionState


@dataclass(frozen=True)
class MemorySnippet:
    key: str
    value: str
    score: int
    source: str = "memory"
    mtime: float = 0.0

    @property
    def freshness_label(self) -> str:
        if self.mtime <= 0:
            return ""
        try:
            dt = datetime.fromtimestamp(self.mtime).astimezone()
            return dt.isoformat(timespec="seconds")
        except Exception:
            return ""


class MemoryService:
    ENTRYPOINT_NAME = "MEMORY.md"
    GLOBAL_ENTRYPOINT_NAME = "SOUL.md"
    MEMORY_FILE_PREFIX = "memory__"
    TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
    SOURCE_OPTIONS = ("session", "workspace", "global")
    SESSION_MEMORY_VALUE_CAP = 520
    FILE_MEMORY_VALUE_CAP = 900
    REJECTED_MEMORY_CANDIDATES_KEY = "_rejected_memory_candidates"

    @classmethod
    def list_memory_entries(
        cls,
        state: SessionState,
        *,
        scope: str = "session",
        work_dir: str | None = None,
    ) -> list[dict[str, str]]:
        scope = cls._normalize_scope(scope)
        if scope == "session":
            return [
                {"key": str(key), "path": "", "updated": ""}
                for key in sorted((state.memory or {}).keys(), key=str.lower)
            ]

        root = cls.ensure_memory_dir(scope, work_dir=work_dir)
        if root is None:
            return []
        entrypoint = cls._entrypoint_name(scope)
        try:
            files = [
                path for path in root.iterdir()
                if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}
            ]
        except Exception:
            return []
        entries: list[dict[str, str]] = []
        for path in sorted(files, key=lambda item: (item.name != entrypoint, item.name.lower())):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            except Exception:
                mtime = ""
            entries.append({"key": cls._key_from_memory_file(path), "path": path.name, "updated": mtime})
        return entries

    @classmethod
    def read_memory_entry(
        cls,
        state: SessionState,
        *,
        scope: str = "session",
        key: str,
        work_dir: str | None = None,
    ) -> str | None:
        scope = cls._normalize_scope(scope)
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None
        if scope == "session":
            if normalized_key not in (state.memory or {}):
                return None
            item = state.memory.get(normalized_key)
            record = item if isinstance(item, MemoryRecord) else MemoryRecord.from_dict(normalized_key, item)
            return record.content

        path = cls._resolve_memory_file(scope, normalized_key, work_dir=work_dir, create_dir=False)
        if path is None or not path.exists() or not path.is_file():
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
        scope: str = "session",
        key: str,
        content: str,
        work_dir: str | None = None,
        current_seq: int = 0,
        reason: str = "",
        tags: list[Any] | None = None,
        category: str = "fact",
        evidence_refs: list[Any] | None = None,
        confidence: float = 1.0,
    ) -> str:
        scope = cls._normalize_scope(scope)
        normalized_key = str(key or "").strip()
        body = str(content or "").strip()
        if not normalized_key:
            return "Memory key is required."
        if not body:
            return "Memory content is required."
        if len(body) > MEMORY_CONTENT_LIMIT:
            return f"Memory content is too long ({len(body)} chars). Keep memory facts within {MEMORY_CONTENT_LIMIT} chars; use state__artifact for long notes."
        if scope == "session":
            feedback = cls.handle_updates(
                state,
                {
                    normalized_key: MemoryRecord(
                        key=normalized_key,
                        content=body,
                        scope="session",
                        category=cls._normalize_category(category),
                        evidence_refs=[str(item).strip() for item in (evidence_refs or []) if str(item).strip()],
                        confidence=float(confidence or 1.0),
                        updated_seq=int(current_seq or 0),
                    )
                },
                current_seq,
            )
            return "\n".join(feedback) if feedback else f"Remembered: {normalized_key}"

        path = cls._resolve_memory_file(scope, normalized_key, work_dir=work_dir, create_dir=True)
        if path is None:
            return f"Failed to resolve {scope} memory path."
        metadata = {
            "created": cls._timestamp(),
            "updated": cls._timestamp(),
            "scope": scope,
            "reason": reason,
            "tags": [str(item).strip() for item in (tags or []) if str(item).strip()],
        }
        title = normalized_key if normalized_key.lower().endswith((".md", ".markdown", ".txt")) else cls._display_title_from_key(normalized_key)
        markdown = with_frontmatter(f"# {title}\n\n{body}\n", metadata)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            cls._ensure_index_link(scope, path, title, work_dir=work_dir)
        except Exception as exc:
            return f"Failed to write {scope} memory {normalized_key}: {exc}"
        return f"Wrote {scope} memory: {normalized_key}"

    @classmethod
    def delete_memory_entry(
        cls,
        state: SessionState,
        *,
        scope: str = "session",
        key: str,
        work_dir: str | None = None,
    ) -> str:
        scope = cls._normalize_scope(scope)
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return "Memory key is required."
        if scope == "session":
            if normalized_key in (state.memory or {}):
                del state.memory[normalized_key]
                return f"Forgot: {normalized_key}"
            return f"No session memory to delete: {normalized_key}"

        path = cls._resolve_memory_file(scope, normalized_key, work_dir=work_dir, create_dir=False)
        if path is None or not path.exists():
            return f"No {scope} memory to delete: {normalized_key}"
        try:
            path.unlink()
            cls._remove_index_link(scope, path, work_dir=work_dir)
        except Exception as exc:
            return f"Failed to delete {scope} memory {normalized_key}: {exc}"
        return f"Deleted {scope} memory: {normalized_key}"

    @classmethod
    def list_memory_candidates(cls, state: SessionState, *, limit: int = 20) -> list[dict[str, Any]]:
        rejected = set(cls._rejected_candidate_ids(state))
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in (state.archive_index or {}).values():
            metadata = getattr(record, "metadata", {}) or {}
            for idx, item in enumerate(metadata.get("memory_candidates") or []):
                candidate = cls._normalize_candidate(item)
                content = candidate.get("content", "")
                if not content:
                    continue
                candidate_id = cls.memory_candidate_id(record.id, content, idx)
                if candidate_id in rejected or candidate_id in seen:
                    continue
                seen.add(candidate_id)
                candidate.update(
                    {
                        "id": candidate_id,
                        "content_id": record.id,
                        "source": record.source,
                        "title": record.title,
                    }
                )
                evidence = list(candidate.get("evidence_refs") or [])
                if record.id not in evidence:
                    evidence.insert(0, record.id)
                candidate["evidence_refs"] = evidence[:8]
                candidates.append(candidate)
                if len(candidates) >= max(1, int(limit or 20)):
                    return candidates
        return candidates

    @classmethod
    def promote_memory_candidate(
        cls,
        state: SessionState,
        *,
        candidate_id: str,
        key: str = "",
        scope: str = "session",
        work_dir: str | None = None,
        current_seq: int = 0,
    ) -> str:
        target_id = str(candidate_id or "").strip()
        if not target_id:
            return "candidate_id is required."
        candidate = next((item for item in cls.list_memory_candidates(state, limit=200) if item.get("id") == target_id), None)
        if not candidate:
            return f"Memory candidate not found: {target_id}"
        memory_key = str(key or "").strip() or cls._key_from_candidate(candidate)
        message = cls.write_memory_entry(
            state,
            scope=scope,
            key=memory_key,
            content=str(candidate.get("content") or ""),
            work_dir=work_dir,
            current_seq=current_seq,
            reason=f"promoted memory candidate {target_id}",
            category=str(candidate.get("category") or "fact"),
            evidence_refs=list(candidate.get("evidence_refs") or []),
            confidence=float(candidate.get("confidence") or 0.5),
        )
        cls.reject_memory_candidate(state, candidate_id=target_id)
        return message

    @classmethod
    def reject_memory_candidate(cls, state: SessionState, *, candidate_id: str) -> str:
        target_id = str(candidate_id or "").strip()
        if not target_id:
            return "candidate_id is required."
        rejected = cls._rejected_candidate_ids(state)
        if target_id not in rejected:
            rejected.append(target_id)
        state.archived_summaries = [
            item for item in (state.archived_summaries or [])
            if not str(item).startswith(cls.REJECTED_MEMORY_CANDIDATES_KEY + ":")
        ]
        state.archived_summaries.append(cls.REJECTED_MEMORY_CANDIDATES_KEY + ":" + ",".join(rejected[-200:]))
        return f"Rejected memory candidate: {target_id}"

    @staticmethod
    def memory_candidate_id(content_id: str, content: str, index: int = 0) -> str:
        import hashlib

        digest = hashlib.sha1(f"{content_id}:{index}:{content}".encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"memcand-{digest}"

    @classmethod
    def ensure_memory_dir(cls, scope: str, *, work_dir: str | None = None) -> Path | None:
        scope = cls._normalize_scope(scope)
        if scope == "workspace":
            raw = str(work_dir or "").strip()
            if not raw:
                return None
            try:
                root = Path(raw).expanduser().resolve()
            except Exception:
                return None
            if not root.exists() or not root.is_dir():
                return None
            memory_dir = root / ".pycat" / "memory"
        elif scope == "global":
            memory_dir = Path.home() / ".PyCat" / "memory"
        else:
            return None
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        return memory_dir

    @staticmethod
    def handle_updates(state: SessionState, updates: Dict[str, Any], current_seq: int) -> List[str]:
        feedback: list[str] = []
        for key, value in updates.items():
            if value is None:
                if key in state.memory:
                    del state.memory[key]
                    feedback.append(f"Forgot: {key}")
            else:
                state.memory[key] = value if isinstance(value, MemoryRecord) else MemoryRecord.from_dict(str(key), value)
                state.memory[key].updated_seq = int(current_seq or state.memory[key].updated_seq or 0)
                feedback.append(f"Remembered: {key}")
        return feedback

    @classmethod
    def _rejected_candidate_ids(cls, state: SessionState) -> list[str]:
        for item in reversed(state.archived_summaries or []):
            text = str(item or "")
            prefix = cls.REJECTED_MEMORY_CANDIDATES_KEY + ":"
            if text.startswith(prefix):
                return [part.strip() for part in text[len(prefix):].split(",") if part.strip()]
        return []

    @staticmethod
    def _normalize_candidate(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            content = str(item.get("content") or item.get("fact") or item.get("text") or "").strip()[:MEMORY_CONTENT_LIMIT]
            category = str(item.get("category") or "fact").strip().lower()
            if category not in MEMORY_CATEGORIES:
                category = "fact"
            evidence_refs = [str(ref).strip() for ref in (item.get("evidence_refs") or []) if str(ref).strip()]
            try:
                confidence = float(item.get("confidence") or 0.5)
            except Exception:
                confidence = 0.5
            return {
                "content": content,
                "category": category,
                "confidence": max(0.0, min(confidence, 1.0)),
                "evidence_refs": evidence_refs,
            }
        return {
            "content": str(item or "").strip()[:MEMORY_CONTENT_LIMIT],
            "category": "fact",
            "confidence": 0.5,
            "evidence_refs": [],
        }

    @staticmethod
    def _key_from_candidate(candidate: dict[str, Any]) -> str:
        import re

        content = str(candidate.get("content") or "memory").strip().lower()
        words = re.findall(r"[\w\u4e00-\u9fff]+", content, flags=re.UNICODE)
        key = ".".join(words[:6]) or "memory"
        return key[:80]

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
        """Return memory facts and memory-file snippets most relevant to the query.

        This deterministic scorer keeps prompt assembly fast, testable, and
        available without an extra model call.
        """

        query_tokens = cls._tokens(query)
        snippets: list[MemorySnippet] = []
        selected_sources = cls._normalize_sources(sources)

        include_session = "session" in selected_sources
        include_workspace = "workspace" in selected_sources
        include_global = "global" in selected_sources

        if include_session:
            for key, value in (state.memory or {}).items():
                record = value if isinstance(value, MemoryRecord) else MemoryRecord.from_dict(str(key), value)
                text = f"{key} {record.content} {record.category}"
                score = cls._score(text, query_tokens)
                if score <= 0 and not query_tokens:
                    score = 1
                if score > 0:
                    snippets.append(
                        MemorySnippet(
                            key=str(key),
                            value=cls._memory_value_for_prompt(
                                key=str(key),
                                value=f"[{record.category}] {record.content}",
                                cap=cls.SESSION_MEMORY_VALUE_CAP,
                            ),
                            score=score,
                            source=record.category or "fact",
                        )
                    )

        if include_workspace:
            for item in cls.load_workspace_memory(work_dir):
                text = f"{item.key} {item.value}"
                score = cls._score(text, query_tokens) + 1
                if score <= 1 and not query_tokens:
                    score = 1
                if score > 0:
                    snippets.append(
                        MemorySnippet(
                            key=item.key,
                            value=cls._memory_value_for_prompt(
                                key=item.key,
                                value=item.value,
                                cap=cls.FILE_MEMORY_VALUE_CAP,
                                source=item.source,
                            ),
                            score=score,
                            source=item.source,
                            mtime=item.mtime,
                        )
                    )

        if include_global:
            for item in cls.load_global_memory():
                text = f"{item.key} {item.value}"
                score = cls._score(text, query_tokens) + 1
                if score <= 1 and not query_tokens:
                    score = 1
                if score > 0:
                    snippets.append(
                        MemorySnippet(
                            key=item.key,
                            value=cls._memory_value_for_prompt(
                                key=item.key,
                                value=item.value,
                                cap=cls.FILE_MEMORY_VALUE_CAP,
                                source=item.source,
                            ),
                            score=score,
                            source=item.source,
                            mtime=item.mtime,
                        )
                    )

        snippets.sort(key=lambda item: (-item.score, item.source, item.key.lower()))
        return snippets[: max(0, int(limit or 0))]

    @classmethod
    def _memory_value_for_prompt(cls, *, key: str, value: str, cap: int, source: str = "session") -> str:
        raw = str(value or "").strip()
        if len(raw) <= max(80, int(cap or 0)):
            return raw
        digest = re.sub(r"\s+", " ", raw[:160]).strip()
        hint = f"... [memory entry trimmed; key={key}; source={source}; chars={len(raw)}; use state__memory read/list for full content if needed]"
        return cls._trim(f"{digest} {hint}", max(160, int(cap or 0)))

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
        selected_sources = cls._normalize_sources(sources)
        if not selected_sources:
            return ""

        snippets = cls.select_relevant(
            state,
            query,
            limit=limit,
            work_dir=work_dir,
            sources=selected_sources,
        )
        if not snippets:
            return ""

        lines = [f"<relevant_memory sources=\"{', '.join(selected_sources)}\">"]
        for item in snippets:
            suffix = f" (updated: {item.freshness_label})" if item.freshness_label else ""
            lines.append(f"- [{item.source}] {item.key}{suffix}: {item.value}")
        lines.append("</relevant_memory>")
        return cls._trim("\n".join(lines), max_chars)

    @classmethod
    def _normalize_sources(cls, sources: Any) -> tuple[str, ...]:
        if sources is None:
            candidates = list(cls.SOURCE_OPTIONS)
        elif isinstance(sources, str):
            candidates = [part.strip().lower() for part in sources.split(",")]
        elif isinstance(sources, (list, tuple, set)):
            candidates = [str(item).strip().lower() for item in sources]
        else:
            candidates = []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item not in cls.SOURCE_OPTIONS or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return tuple(normalized)

    @staticmethod
    def _normalize_category(category: Any) -> str:
        raw = str(category or "fact").strip().lower()
        return raw if raw in MEMORY_CATEGORIES else "fact"

    @classmethod
    def load_workspace_memory(
        cls,
        work_dir: str | None,
        *,
        limit: int = 20,
        max_file_chars: int = 12_000,
    ) -> list[MemorySnippet]:
        """Load Markdown memory notes from ``<work_dir>/.pycat/memory``.

        This keeps PyCat's workspace memory loading deterministic and local.
        Only markdown/text notes are scanned, newest files win ties, and huge
        notes are previewed.
        """

        root = cls._workspace_memory_dir(work_dir)
        if root is None:
            return []

        indexed = cls._load_workspace_memory_index(
            root,
            limit=max(0, int(limit or 0)),
            max_file_chars=max_file_chars,
        )
        if indexed:
            return indexed[: max(0, int(limit or 0))]

        try:
            candidates = [
                path
                for path in root.iterdir()
                if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}
            ]
        except Exception:
            return []

        try:
            candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        except Exception:
            candidates.sort(key=lambda path: path.name.lower())

        snippets: list[MemorySnippet] = []
        for path in candidates[: max(0, int(limit or 0))]:
            snippet = cls._load_workspace_memory_file(path, max_file_chars=max_file_chars)
            if snippet is not None:
                snippets.append(snippet)
        return snippets

    @classmethod
    def _load_workspace_memory_index(
        cls,
        root: Path,
        *,
        limit: int,
        max_file_chars: int,
    ) -> list[MemorySnippet]:
        return cls._load_memory_index(
            root,
            entrypoint_name=cls.ENTRYPOINT_NAME,
            index_source="workspace-index",
            topic_source="workspace-topic",
            limit=limit,
            max_file_chars=max_file_chars,
        )

    @classmethod
    def _load_memory_index(
        cls,
        root: Path,
        *,
        entrypoint_name: str,
        index_source: str,
        topic_source: str,
        limit: int,
        max_file_chars: int,
    ) -> list[MemorySnippet]:
        if limit <= 0:
            return []

        entrypoint = root / entrypoint_name
        if not entrypoint.exists() or not entrypoint.is_file():
            return []

        snippets: list[MemorySnippet] = []
        index_snippet = cls._load_workspace_memory_file(
            entrypoint,
            max_file_chars=max_file_chars,
            source=index_source,
        )
        if index_snippet is not None:
            snippets.append(index_snippet)

        for path in cls._iter_workspace_memory_links(entrypoint, root, max_file_chars=max_file_chars):
            if len(snippets) >= limit:
                break
            snippet = cls._load_workspace_memory_file(
                path,
                max_file_chars=max_file_chars,
                source=topic_source,
            )
            if snippet is not None:
                snippets.append(snippet)
        return snippets

    @classmethod
    def _workspace_memory_dir(cls, work_dir: str | None) -> Path | None:
        raw = str(work_dir or "").strip()
        if not raw:
            return None
        try:
            root = Path(raw).expanduser().resolve()
        except Exception:
            return None
        if not root.exists() or not root.is_dir():
            return None
        memory_dir = root / ".pycat" / "memory"
        if not memory_dir.exists() or not memory_dir.is_dir():
            return None
        return memory_dir

    # ------------------------------------------------------------------
    # Global memory (~/.PyCat/memory)
    # ------------------------------------------------------------------

    @classmethod
    def load_global_memory(
        cls,
        *,
        limit: int = 20,
        max_file_chars: int = 12_000,
    ) -> list[MemorySnippet]:
        """Load Markdown memory notes from ``~/.PyCat/memory``.

        Global memory survives across all workspaces. Only markdown/text notes
        are scanned, and the SOUL.md entrypoint is read first when available.
        """
        root = cls._global_memory_dir()
        if root is None:
            return []

        indexed = cls._load_memory_index(
            root,
            entrypoint_name=cls.GLOBAL_ENTRYPOINT_NAME,
            index_source="global-index",
            topic_source="global-topic",
            limit=max(0, int(limit or 0)),
            max_file_chars=max_file_chars,
        )
        if indexed:
            return indexed[: max(0, int(limit or 0))]

        try:
            candidates = [
                path
                for path in root.iterdir()
                if path.is_file()
                and path.name.startswith(cls.MEMORY_FILE_PREFIX)
                and path.suffix.lower() in {".md", ".markdown", ".txt"}
            ]
        except Exception:
            return []

        try:
            candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        except Exception:
            candidates.sort(key=lambda path: path.name.lower())

        snippets: list[MemorySnippet] = []
        for path in candidates[: max(0, int(limit or 0))]:
            snippet = cls._load_workspace_memory_file(
                path,
                max_file_chars=max_file_chars,
                source="global",
            )
            if snippet is not None:
                snippets.append(snippet)
        return snippets

    @classmethod
    def _global_memory_dir(cls) -> Path | None:
        try:
            memory_dir = Path.home() / ".PyCat" / "memory"
        except Exception:
            return None
        if not memory_dir.exists() or not memory_dir.is_dir():
            return None
        return memory_dir

    @classmethod
    def _normalize_scope(cls, scope: str) -> str:
        normalized = str(scope or "session").strip().lower()
        return normalized if normalized in cls.SOURCE_OPTIONS else "session"

    @classmethod
    def _entrypoint_name(cls, scope: str) -> str:
        return cls.GLOBAL_ENTRYPOINT_NAME if cls._normalize_scope(scope) == "global" else cls.ENTRYPOINT_NAME

    @classmethod
    def _resolve_memory_file(
        cls,
        scope: str,
        key: str,
        *,
        work_dir: str | None = None,
        create_dir: bool = False,
    ) -> Path | None:
        scope = cls._normalize_scope(scope)
        root = cls.ensure_memory_dir(scope, work_dir=work_dir) if create_dir else (
            cls._workspace_memory_dir(work_dir) if scope == "workspace" else cls._global_memory_dir()
        )
        if root is None:
            return None
        raw_key = str(key or "").strip().replace("\\", "/")
        if not raw_key:
            return None
        if raw_key in {cls.ENTRYPOINT_NAME, cls.GLOBAL_ENTRYPOINT_NAME} or raw_key.endswith((".md", ".markdown", ".txt")):
            candidate_name = raw_key
        else:
            candidate_name = f"{cls.MEMORY_FILE_PREFIX}{cls._sanitize_memory_key(raw_key)}.md"
        try:
            candidate = (root / candidate_name).resolve()
            resolved_root = root.resolve()
            if candidate != resolved_root and resolved_root not in candidate.parents:
                return None
            return candidate
        except Exception:
            return None

    @classmethod
    def _ensure_index_link(cls, scope: str, memory_file: Path, title: str, *, work_dir: str | None = None) -> None:
        scope = cls._normalize_scope(scope)
        root = cls.ensure_memory_dir(scope, work_dir=work_dir)
        if root is None:
            return
        entrypoint = root / cls._entrypoint_name(scope)
        header = "# PyCat Global Memory\n\n" if scope == "global" else "# PyCat Workspace Memory\n\n"
        if entrypoint.exists():
            content = entrypoint.read_text(encoding="utf-8", errors="replace")
        else:
            content = header + "## Memories\n"
        try:
            rel = memory_file.resolve().relative_to(root.resolve()).as_posix()
        except Exception:
            rel = memory_file.name
        link = f"- [{title}]({rel})"
        if f"]({rel})" not in content:
            content = content.rstrip() + "\n" + link + "\n"
        entrypoint.write_text(content, encoding="utf-8")

    @classmethod
    def _remove_index_link(cls, scope: str, memory_file: Path, *, work_dir: str | None = None) -> None:
        scope = cls._normalize_scope(scope)
        root = cls.ensure_memory_dir(scope, work_dir=work_dir)
        if root is None:
            return
        entrypoint = root / cls._entrypoint_name(scope)
        if not entrypoint.exists():
            return
        try:
            rel = memory_file.resolve().relative_to(root.resolve()).as_posix()
            content = entrypoint.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        lines = [line for line in content.splitlines() if f"]({rel})" not in line]
        entrypoint.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    @classmethod
    def _key_from_memory_file(cls, path: Path) -> str:
        name = path.name
        stem = path.stem
        if stem.startswith(cls.MEMORY_FILE_PREFIX):
            return stem[len(cls.MEMORY_FILE_PREFIX):]
        return name

    @classmethod
    def _display_title_from_key(cls, key: str) -> str:
        return str(key or "memory").strip().replace("_", " ").replace("-", " ").title()

    @classmethod
    def _sanitize_memory_key(cls, key: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(key or "").strip())
        return safe.strip("._ ") or "memory"

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%Y%m%dT%H%M%S")

    @classmethod
    def _read_workspace_memory_raw(cls, path: Path, *, max_file_chars: int) -> tuple[str, float] | None:
        try:
            stat = path.stat()
            if stat.st_size <= 0:
                return None
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read(max_file_chars + 1)
        except Exception:
            return None

        return raw, float(getattr(stat, "st_mtime", 0.0) or 0.0)

    @classmethod
    def _load_workspace_memory_file(
        cls,
        path: Path,
        *,
        max_file_chars: int,
        source: str = "workspace",
    ) -> MemorySnippet | None:
        loaded = cls._read_workspace_memory_raw(path, max_file_chars=max_file_chars)
        if loaded is None:
            return None

        raw, mtime = loaded

        content = cls._strip_frontmatter(raw)
        title, preview = cls._extract_title_and_preview(path, content)
        if not preview:
            return None
        if len(raw) > max_file_chars:
            preview = cls._trim(preview, max(0, max_file_chars // 2))
        return MemorySnippet(
            key=title,
            value=preview,
            score=1,
            source=str(source or "workspace"),
            mtime=mtime,
        )

    @classmethod
    def _iter_workspace_memory_links(
        cls,
        entrypoint: Path,
        root: Path,
        *,
        max_file_chars: int,
    ) -> list[Path]:
        loaded = cls._read_workspace_memory_raw(entrypoint, max_file_chars=max_file_chars)
        if loaded is None:
            return []

        raw, _mtime = loaded
        content = cls._strip_frontmatter(raw)
        resolved_root = root.resolve()
        seen: set[Path] = {entrypoint.resolve()}
        targets: list[Path] = []

        for target in extract_markdown_links(content):
            try:
                candidate = (resolved_root / target).resolve()
            except Exception:
                continue

            if candidate in seen:
                continue
            if resolved_root != candidate and resolved_root not in candidate.parents:
                continue
            if not candidate.exists() or not candidate.is_file():
                continue
            if candidate.suffix.lower() not in {".md", ".markdown", ".txt"}:
                continue

            seen.add(candidate)
            targets.append(candidate)

        return targets

    @classmethod
    def _strip_frontmatter(cls, text: str) -> str:
        return strip_frontmatter(text)

    @classmethod
    def _extract_title_and_preview(cls, path: Path, content: str) -> tuple[str, str]:
        return extract_title_and_preview(path, content)

    @classmethod
    def _tokens(cls, text: str) -> set[str]:
        return {token.lower() for token in cls.TOKEN_RE.findall(str(text or "")) if len(token.strip()) >= 2}

    @classmethod
    def _score(cls, text: str, query_tokens: set[str]) -> int:
        if not query_tokens:
            return 0
        text_tokens = cls._tokens(text)
        if not text_tokens:
            return 0
        overlap = query_tokens & text_tokens
        score = len(overlap) * 4
        lowered = str(text or "").lower()
        for token in query_tokens:
            if token in lowered:
                score += 1
        return score

    @staticmethod
    def _trim(text: str, max_chars: int) -> str:
        raw = str(text or "").strip()
        if len(raw) <= max_chars:
            return raw
        return raw[: max(0, max_chars - 3)].rstrip() + "..."
