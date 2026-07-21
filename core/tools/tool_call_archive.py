"""Archive-first tool call result handling."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.content.archive_store import ArchivedContentRecord, SessionArchiveStore, estimate_tokens, stringify_content
from core.content.archive_view_service import ArchiveViewService
from core.content.view_protocol import (
    ContentExactness,
    ContentViewLabel,
    exact_view_from_text,
)


@dataclass
class ContentView:
    label: ContentViewLabel
    body: str
    content_id: str = ""
    source: str = ""
    chars: int = 0
    exactness: ContentExactness = ContentExactness.EXACT
    preview: str = ""
    record: ArchivedContentRecord | None = None

    def render(self) -> str:
        body = str(self.body or "")
        if self.content_id:
            header = f"{self.label.bracketed}\ncontent_id={self.content_id}"
            if self.exactness == ContentExactness.EXACT:
                header += "\nexact=true"
            return f"{header}\n{body}" if body else header
        return body


@dataclass
class ToolCallArchiveResult:
    display: str
    full_path: str | None = None
    total_chars: int = 0
    is_processed: bool = False
    strategy: str = "inline"
    hint: str | None = None
    summary: str = ""
    token_estimate: int = 0
    archive: ArchivedContentRecord | None = None
    view_label: str = "inline"
    view_kind: str = "inline"
    view_desc: str = ""
    view_exactness: str = "exact"
    content_id: str = ""
    image_count: int = 0
    images_restorable: bool = True

    def to_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "tool_result_strategy": self.strategy,
            "tool_result_chars": self.total_chars,
            "tool_result_tokens_estimate": self.token_estimate,
            "tool_result_view": self.view_label,
            "tool_result_view_kind": self.view_kind,
            "tool_result_view_desc": self.view_desc,
            "tool_result_exactness": self.view_exactness,
        }
        if self.hint:
            metadata["tool_result_hint"] = self.hint
        if self.summary:
            metadata["tool_result_summary"] = self.summary
        if self.archive is not None:
            metadata["content_id"] = self.archive.id
            metadata["archive_kind"] = self.archive.kind
            metadata["archive_ref"] = self.archive.original_ref
            metadata["archive_size"] = int(self.archive.size or 0)
            metadata["archive_updated_seq"] = int(self.archive.updated_seq or self.archive.created_seq or 0)
        elif self.content_id:
            metadata["content_id"] = self.content_id
        if self.image_count:
            metadata["archive_image_count"] = int(self.image_count)
            metadata["archive_images_restorable"] = bool(self.images_restorable)
        return metadata


class ToolCallArchiveService:
    """Archive important tool outputs before model-visible display shaping."""

    ARCHIVE_THRESHOLD_CHARS = 32_000
    DATA_CATEGORIES = {"read", "web", "execute", "delegate", "capability", "mcp"}
    NON_ARCHIVE_PREFIXES = ("archive__", "state__", "user__")
    NON_ARCHIVE_TOOLS = {
        "agent__complete",
        "file__write",
        "file__edit",
        "file__patch",
        "file__delete",
        "skill__load",
    }
    def __init__(self, work_dir: str, conversation_id: object = None):
        self.archive_store = SessionArchiveStore(work_dir, conversation_id=conversation_id)

    def process(
        self,
        *,
        tool_name: str,
        raw_text: Any,
        tool_category: str = "",
        tool_call_id: str | None = None,
        tool_args: dict[str, Any] | None = None,
        seq_id: int = 0,
        images: list[str] | None = None,
    ) -> ToolCallArchiveResult:
        text = stringify_content(raw_text)
        if self._should_archive(tool_name, tool_category, text):
            return self._archive(tool_name, text, tool_call_id, tool_args or {}, seq_id, images or [])
        return self._inline(tool_name, text)

    def _should_archive(self, tool_name: str, tool_category: str, text: str) -> bool:
        name = str(tool_name or "").strip()
        if not text or name in self.NON_ARCHIVE_TOOLS or name.startswith(self.NON_ARCHIVE_PREFIXES):
            return False
        if name == "skill__read_resource":
            return True
        category = str(tool_category or "").strip().lower()
        if category:
            return category in self.DATA_CATEGORIES
        if name.startswith(("web__", "shell__", "python__", "mcp__", "capability__")):
            return True
        if name == "agent__run":
            return True
        if name.startswith("file__"):
            return name in {"file__read", "file__list", "file__search"}
        return len(text) > self.ARCHIVE_THRESHOLD_CHARS

    def _inline(self, tool_name: str, text: str) -> ToolCallArchiveResult:
        label = self._leading_view_label(text)
        exactness = self._exactness_for_label(label)
        content_id = self._parse_content_id(text) if label and label.kind in {"content", "full", "line", "char"} else ""
        return ToolCallArchiveResult(
            display=text,
            total_chars=len(text),
            strategy="inline",
            summary=self._inline_summary(tool_name, text),
            token_estimate=estimate_tokens(text),
            view_label=label.value if label else "inline",
            view_kind=label.kind if label else "inline",
            view_desc=label.semantic_desc if label else "",
            view_exactness=exactness.value,
            content_id=content_id,
        )

    def _archive(
        self,
        tool_name: str,
        text: str,
        tool_call_id: str | None,
        tool_args: dict[str, Any],
        seq_id: int,
        images: list[str],
    ) -> ToolCallArchiveResult:
        call_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tool_call_id or "call")).strip("_")[:40] or "call"
        content_kind = self._detect_content_kind(tool_name, text)
        record = self.archive_store.write_original(
            kind="tool_call",
            title=f"{tool_name or 'tool'}_{call_id}",
            content=text,
            source=str(tool_name or ""),
            seq_id=seq_id,
            metadata={
                "tool_call_id": str(tool_call_id or ""),
                "content_kind": content_kind,
                "references": self._extract_references(text),
            },
            input_payload={
                "tool_name": str(tool_name or ""),
                "tool_call_id": str(tool_call_id or ""),
                "arguments": dict(tool_args or {}),
            },
            images=images,
        )
        full_path = str(self.archive_store.resolve_ref(record.original_ref))
        display = f"{ContentViewLabel.build('archive', 'ready')}\ncontent_id={record.id}"
        hint = f"Use archive__read with content_id={record.id} to access archived output."
        return ToolCallArchiveResult(
            display=display,
            full_path=full_path,
            total_chars=len(text),
            is_processed=True,
            strategy="archive",
            hint=hint,
            summary=record.summary,
            token_estimate=record.token_estimate,
            archive=record,
            view_label="archive:ready",
            view_kind="archive",
            view_desc="ready",
            view_exactness="exact",
            content_id=record.id,
            image_count=len(images),
            images_restorable=self.archive_store.images_are_restorable(record, expected_count=len(images)),
        )

    @staticmethod
    def _detect_content_kind(tool_name: str, text: str) -> str:
        raw = str(text or "").lstrip()
        name = str(tool_name or "")
        if name.startswith("web__"):
            return "web"
        if re.search(r"data:image/|\[Image:|\"type\"\s*:\s*\"image\"|\bmimeType\b", raw[:4000], re.I):
            return "image"
        if raw[:1] in {"{", "["}:
            return "json"
        if "<html" in raw[:2000].lower() or "<!doctype html" in raw[:2000].lower():
            return "html"
        return "text"

    @staticmethod
    def _extract_references(text: str) -> list[str]:
        refs: list[str] = []
        for match in re.findall(r"https?://[^\s\]\)\}\>'\"]+", str(text or "")):
            ref = match.rstrip(".,;:")
            if ref and ref not in refs:
                refs.append(ref)
            if len(refs) >= 20:
                break
        return refs

    @staticmethod
    def _inline_summary(tool_name: str, text: str) -> str:
        first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
        return (first or f"{tool_name or 'tool'} completed.")[:220]

    @staticmethod
    def _leading_view_label(text: str) -> ContentViewLabel | None:
        raw = str(text or "").lstrip()
        if not raw.startswith("[") or "]" not in raw[:80]:
            return None
        label = ContentViewLabel.parse(raw)
        if label.kind in {"content", "full", "line", "char", "summary", "archive"}:
            return label
        return None

    @staticmethod
    def _exactness_for_label(label: ContentViewLabel | None) -> ContentExactness:
        if label is None:
            return ContentExactness.EXACT
        if label.kind in {"content", "full", "line", "char"}:
            return ContentExactness.EXACT
        if label.kind == "summary" and label.desc in {"pending", "failed"}:
            return ContentExactness.PENDING
        if label.kind == "archive":
            return ContentExactness.PENDING
        if label.kind == "summary":
            return ContentExactness.DERIVED
        return ContentExactness.EXACT

    @staticmethod
    def _parse_content_id(text: str) -> str:
        for line in str(text or "").splitlines()[:8]:
            clean = line.strip()
            if clean.startswith("content_id="):
                return clean.split("=", 1)[1].strip()
        return ""


class ToolResultViewService:
    """Build the first recoverable model view for a completed tool result."""

    FULL_LIMIT = 8_000
    SUMMARY_THRESHOLD = 2_000
    SUMMARY_EXACT_PREFIX = 4_000

    def __init__(
        self,
        *,
        work_dir: str = "",
        conversation_id: object = None,
        conversation: Any = None,
        compressor: Any = None,
    ) -> None:
        self.work_dir = str(work_dir or "")
        self.conversation_id = conversation_id
        self.conversation = conversation
        self.compressor = compressor

    async def build_display(
        self,
        *,
        tool_name: str,
        text: str,
        archive_result: ToolCallArchiveResult,
    ) -> ToolCallArchiveResult:
        if archive_result.archive is None:
            archive_result.view_label = "inline"
            archive_result.view_kind = "inline"
            archive_result.view_desc = ""
            archive_result.view_exactness = "exact"
            return archive_result

        record = archive_result.archive
        should_summarize = self.needs_summary(text=text, archive_result=archive_result)
        if should_summarize and self.compressor is not None and self.work_dir:
            service = ArchiveViewService(
                work_dir=self.work_dir,
                conversation_id=self.conversation_id,
                conversation=self.conversation,
                compressor=self.compressor,
            )
            result = await service.get_or_create_summary(
                record.id,
                purpose=f"initial_tool_result:{tool_name}",
            )
            record = result.record or record
            archive_result.archive = record

        if len(str(text or "")) <= self.FULL_LIMIT:
            view = self._full_view(record=record, tool_name=tool_name, text=text)
            return self._apply_view(archive_result, view, summary=record.summary)
        if record.summary:
            view = self._summary_with_exact_prefix_view(
                record=record,
                tool_name=tool_name,
                text=text,
                summary=record.summary,
            )
            return self._apply_view(archive_result, view, summary=record.summary)
        view = self._content_preview_view(
            record=record,
            tool_name=tool_name,
            text=text,
            limit=self.FULL_LIMIT,
        )
        return self._apply_view(archive_result, view, summary=record.summary)

    @classmethod
    def needs_summary(cls, *, text: str, archive_result: ToolCallArchiveResult) -> bool:
        record = archive_result.archive
        if record is None or record.summary:
            return False
        return len(str(text or "")) >= cls.SUMMARY_THRESHOLD or archive_result.image_count > 0

    @staticmethod
    def _full_view(*, record: ArchivedContentRecord, tool_name: str, text: str) -> ContentView:
        label = exact_view_from_text(tool_name, text)
        return ContentView(
            label=label,
            body=str(text or ""),
            content_id=record.id,
            source=str(tool_name or ""),
            chars=len(str(text or "")),
            exactness=ContentExactness.EXACT,
            record=record,
        )

    @staticmethod
    def _content_preview_view(
        *,
        record: ArchivedContentRecord,
        tool_name: str,
        text: str,
        limit: int,
    ) -> ContentView:
        body = str(text or "")
        end = min(len(body), max(1, int(limit)))
        next_offset = end if end < len(body) else None
        return ContentView(
            label=ContentViewLabel("char", f"0-{end}"),
            body=f"next_offset={next_offset if next_offset is not None else 'none'}\n{body[:end]}",
            content_id=record.id,
            source=str(tool_name or ""),
            chars=len(body),
            exactness=ContentExactness.EXACT,
            preview=body[:end],
            record=record,
        )

    @classmethod
    def _summary_with_exact_prefix_view(
        cls,
        *,
        record: ArchivedContentRecord,
        tool_name: str,
        text: str,
        summary: str,
    ) -> ContentView:
        body = str(text or "")
        end = min(len(body), cls.SUMMARY_EXACT_PREFIX)
        next_offset = end if end < len(body) else None
        rendered = (
            f"summary:\n{str(summary or '').strip()}\n\n"
            f"exact_excerpt:\nchar_range=0-{end}\n"
            f"next_offset={next_offset if next_offset is not None else 'none'}\n{body[:end]}"
        )
        return ContentView(
            label=ContentViewLabel("archive", "summary+char"),
            body=rendered,
            content_id=record.id,
            source=str(tool_name or ""),
            chars=len(body),
            exactness=ContentExactness.DERIVED,
            preview=body[:end],
            record=record,
        )

    @staticmethod
    def _apply_view(handle: ToolCallArchiveResult, view: ContentView, *, summary: str = "") -> ToolCallArchiveResult:
        handle.display = view.render()
        handle.summary = str(summary or "")
        if not handle.summary and view.exactness == ContentExactness.EXACT:
            handle.summary = ToolCallArchiveService._inline_summary(view.source, view.preview or view.body)
        handle.view_label = view.label.value
        handle.view_kind = view.label.kind
        handle.view_desc = view.label.semantic_desc
        handle.view_exactness = view.exactness.value
        handle.content_id = view.content_id or handle.content_id
        if view.record is not None:
            handle.archive = view.record
        return handle
