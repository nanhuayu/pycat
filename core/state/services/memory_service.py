from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from models.state import SessionState


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
    TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
    FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
    INDEX_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    SOURCE_OPTIONS = ("session", "workspace", "global")

    @staticmethod
    def handle_updates(state: SessionState, updates: Dict[str, Any], current_seq: int) -> List[str]:
        feedback: list[str] = []
        for key, value in updates.items():
            if value is None:
                if key in state.memory:
                    del state.memory[key]
                    feedback.append(f"Forgot: {key}")
            else:
                state.memory[key] = str(value)
                feedback.append(f"Remembered: {key}")
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
        """Return memory facts and document previews most relevant to the query.

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
                text = f"{key} {value}"
                score = cls._score(text, query_tokens)
                if score <= 0 and not query_tokens:
                    score = 1
                if score > 0:
                    snippets.append(
                        MemorySnippet(
                            key=str(key),
                            value=cls._trim(str(value or ""), 320),
                            score=score,
                            source="fact",
                        )
                    )

            for name, doc in (state.documents or {}).items():
                normalized = str(name or "").strip().lower()
                if normalized == "plan":
                    continue
                preview_source = str(getattr(doc, "abstract", "") or getattr(doc, "content", "") or "")
                if not preview_source.strip():
                    continue
                text = f"{name} {getattr(doc, 'kind', '')} {preview_source}"
                score = cls._score(text, query_tokens)
                if normalized == "memory":
                    score += 2
                if score <= 0 and not query_tokens:
                    score = 1
                if score > 0:
                    snippets.append(
                        MemorySnippet(
                            key=str(name),
                            value=cls._trim(preview_source, 420),
                            score=score,
                            source="document",
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
                            value=cls._trim(item.value, 520),
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
                            value=cls._trim(item.value, 520),
                            score=score,
                            source=item.source,
                            mtime=item.mtime,
                        )
                    )

        snippets.sort(key=lambda item: (-item.score, item.source, item.key.lower()))
        return snippets[: max(0, int(limit or 0))]

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
        if limit <= 0:
            return []

        entrypoint = root / cls.ENTRYPOINT_NAME
        if not entrypoint.exists() or not entrypoint.is_file():
            return []

        snippets: list[MemorySnippet] = []
        index_snippet = cls._load_workspace_memory_file(
            entrypoint,
            max_file_chars=max_file_chars,
            source="workspace-index",
        )
        if index_snippet is not None:
            snippets.append(index_snippet)

        for path in cls._iter_workspace_memory_links(entrypoint, root, max_file_chars=max_file_chars):
            if len(snippets) >= limit:
                break
            snippet = cls._load_workspace_memory_file(
                path,
                max_file_chars=max_file_chars,
                source="workspace-topic",
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

        indexed = cls._load_workspace_memory_index(
            root,
            limit=max(0, int(limit or 0)),
            max_file_chars=max_file_chars,
        )
        if indexed:
            return [
                MemorySnippet(
                    key=s.key,
                    value=s.value,
                    score=s.score,
                    source=s.source.replace("workspace", "global"),
                    mtime=s.mtime,
                )
                for s in indexed[: max(0, int(limit or 0))]
            ]

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

        for match in cls.INDEX_LINK_RE.finditer(content):
            raw_target = str(match.group(1) or "").strip()
            if not raw_target:
                continue
            target = raw_target.split("#", 1)[0].split("?", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue

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
        return cls.FRONTMATTER_RE.sub("", str(text or ""), count=1).strip()

    @classmethod
    def _extract_title_and_preview(cls, path: Path, content: str) -> tuple[str, str]:
        lines = [line.strip() for line in str(content or "").splitlines()]
        title = ""
        body_lines: list[str] = []
        for line in lines:
            if not line:
                if body_lines:
                    break
                continue
            if not title and line.startswith("#"):
                title = line.lstrip("#").strip()
                continue
            body_lines.append(line)
            if len(" ".join(body_lines)) >= 700:
                break
        title = title or path.stem
        preview = " ".join(body_lines).strip() or str(content or "").strip()
        return f"{path.name} / {title}", cls._trim(preview, 900)

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
