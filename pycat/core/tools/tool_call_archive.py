"""Archive-first tool call result handling."""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pycat.core.content.archive_store import (
    ArchivedContentRecord,
    SessionArchiveStore,
    estimate_tokens,
    stringify_content,
)
from pycat.core.content.view_protocol import (
    TOOL_SUMMARY_PROJECTION_CHARS,
    ContentExactness,
    ContentViewLabel,
    exact_view_from_text,
)
from pycat.models.conversation import Message, normalize_tool_result, tool_call_name

logger = logging.getLogger(__name__)


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
    def __init__(self, work_dir: str, conversation_id: object = None, *, data_dir: str | Path | None = None):
        self.archive_store = SessionArchiveStore(work_dir, conversation_id=conversation_id, data_dir=data_dir)

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
        is_error: bool = False,
        result_metadata: dict[str, Any] | None = None,
    ) -> ToolCallArchiveResult:
        text = stringify_content(raw_text)
        if is_error:
            metadata = {**(result_metadata or {}), "is_error": True, "auto_summary": False}
            handle = ToolCallArchiveResult(display=text, total_chars=len(text))
            if len(text) > ToolResultViewService.FULL_LIMIT:
                try:
                    handle = self._archive(tool_name, text, tool_call_id, tool_args or {}, seq_id, images or [], metadata)
                except OSError as exc:
                    logger.warning("Failed to archive error output for %s: %s", tool_name, exc)
            return ToolResultViewService.build_error_display(tool_name=tool_name, text=text, handle=handle, metadata=metadata)
        if self._should_archive(tool_name, tool_category, text):
            return self._archive(tool_name, text, tool_call_id, tool_args or {}, seq_id, images or [], result_metadata or {})
        return self._inline(tool_name, text)

    def prepare_request_views(
        self, messages: list[Message], *, errors_only: bool = True,
        max_result_chars: int | None = None, keep_recent_batches: int = 0,
        require_archive: bool = False,
    ) -> tuple[list[Message], bool]:
        """Archive oversized receipts and return a copy without changing the input."""
        max_result_chars = ToolResultViewService.FULL_LIMIT if max_result_chars is None else max(1, int(max_result_chars))
        keep_recent_batches = max(0, int(keep_recent_batches))
        batches = [i for i, msg in enumerate(messages) if msg.role == "assistant" and msg.tool_calls]
        protected = set(batches[-keep_recent_batches:]) if keep_recent_batches else set()
        prepared = None
        records = None
        for index in batches:
            if index in protected:
                continue
            for result_index, call in enumerate(messages[index].tool_calls or []):
                if not isinstance(call, dict) or call.get("result") is None:
                    continue
                payload = normalize_tool_result(call["result"])
                text = payload.get("content")
                metadata = dict(payload.get("metadata") or {})
                is_error = bool(metadata.get("is_error")) or str(metadata.get("tool_result_view_kind")) == "error"
                if not isinstance(text, str) or len(text) <= max_result_chars or (errors_only and not is_error):
                    continue
                name = tool_call_name(call)
                arguments = (call.get("function") or {}).get("arguments", call.get("arguments", {}))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {"raw_arguments": arguments}
                if not isinstance(arguments, dict):
                    arguments = {"raw_arguments": arguments}
                record = self.archive_store.read_record(str(metadata.get("content_id") or ""))
                if (is_error and record is not None
                        and (record.source != name or record.metadata.get("tool_call_id") != str(call.get("id") or ""))):
                    record = None
                source_text = text
                # A read may point at a larger source Archive while returning
                # only an explicit range. Restore only this invocation's receipt.
                if (record is not None and record.source == name
                        and record.metadata.get("tool_call_id") == str(call.get("id") or "")):
                    source_text = self.archive_store.read_original(record)
                    if not self.archive_store.original_matches(record, source_text):
                        raise ValueError(f"Archive original digest mismatch: {record.id}")
                elif record is None:
                    # Reuse a previously repaired invocation without a second index/store.
                    if records is None:
                        records = self.archive_store.list_records(kind="tool_call")
                    expected_input = {"tool_name": name, "tool_call_id": str(call.get("id") or ""), "arguments": arguments}
                    for existing in records:
                        if existing.source != name or existing.metadata.get("tool_call_id") != str(call.get("id") or ""):
                            continue
                        if not self.archive_store.original_matches(existing, text):
                            continue
                        input_ref = existing.metadata.get("input_ref")
                        if not input_ref or json.loads(self.archive_store.resolve_ref(input_ref).read_text(encoding="utf-8")) != expected_input:
                            continue
                        if self.archive_store.original_matches(existing, self.archive_store.read_original(existing)):
                            record = existing
                            break
                handle = ToolCallArchiveResult(display=text, total_chars=len(source_text), archive=record)
                if record is None:
                    try:
                        handle = self._archive(name, source_text, call.get("id"), arguments, messages[index].seq_id,
                                               call.get("result_images") or [],
                                               {**metadata, "is_error": is_error, "auto_summary": False})
                    except OSError as exc:
                        logger.warning("Failed to archive oversized request result for %s: %s", name, exc)
                if require_archive and handle.archive is None:
                    # Persistence repair must never replace exact text with an
                    # unrecoverable excerpt. Request-only safety views may do so.
                    continue
                if is_error:
                    handle = ToolResultViewService.build_error_display(tool_name=name, text=source_text, handle=handle, metadata=metadata)
                else:
                    handle = ToolResultViewService.build_bounded_display(text=source_text, handle=handle, max_chars=max_result_chars)
                if prepared is None:
                    prepared = copy.deepcopy(messages)
                payload["content"] = handle.display
                payload["summary"] = handle.summary
                payload["metadata"] = {**metadata, **handle.to_metadata(), "is_error": is_error}
                copied_call = prepared[index].tool_calls[result_index]
                copied_call["result"] = payload
                copied_call["result_summary"] = handle.summary
                copied_call["result_metadata"] = dict(payload["metadata"])
        return (prepared, True) if prepared is not None else (messages, False)

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
            return name in {"file__read", "file__view", "file__ocr", "file__list", "file__search"}
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
        result_metadata: dict[str, Any],
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
                **{key: value for key, value in result_metadata.items() if key in {
                    "model", "provider", "image_protocol", "image_operation", "image_options", "image_outputs",
                    "input_image_count", "has_mask", "request_id", "image_errors", "usage",
                    "auto_summary", "source_ref", "source_digest", "is_error", "error_code", "retryable",
                }},
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

    SHORT_LIMIT = 2_000
    FULL_LIMIT = 8_000
    SUMMARY_LIMIT = TOOL_SUMMARY_PROJECTION_CHARS
    HEAD_LIMIT = 2_000
    TAIL_LIMIT = 2_000

    @classmethod
    def exact_excerpts(cls, text: str, *, head_chars: int | None = None, tail_chars: int | None = None) -> str:
        head_chars = cls.HEAD_LIMIT if head_chars is None else head_chars
        tail_chars = cls.TAIL_LIMIT if tail_chars is None else tail_chars
        total = len(text)
        head_end = min(total, max(0, head_chars))
        tail_start = max(head_end, total - max(0, tail_chars))
        return (
            f"[char:0-{head_end}]\nexact=true\n{text[:head_end]}\n\n"
            f"[char:{tail_start}-{total}]\nexact=true\n{text[tail_start:]}"
        )

    @classmethod
    def build_error_display(
        cls, *, tool_name: str, text: str, handle: ToolCallArchiveResult, metadata: dict[str, Any],
    ) -> ToolCallArchiveResult:
        code = str(metadata.get("error_code") or "tool_failed").strip() or "tool_failed"
        lines = ["[error]", "status=error", f"error_code={code}",
                 f"retryable={'true' if metadata.get('retryable', False) else 'false'}"]
        if metadata.get("source_ref"):
            lines.append(f"source_ref={metadata['source_ref']}")
        long = len(text) > cls.FULL_LIMIT
        if long:
            if handle.archive is not None:
                lines.append(f"content_id={handle.archive.id}")
            lines.extend(("derived=true", f"chars={len(text)}", "message=" + cls.exact_excerpts(text)))
            if handle.archive is not None:
                lines.append(f'Use archive__read(content_id="{handle.archive.id}", view="content", offset={cls.HEAD_LIMIT}) '
                             "to restore omitted exact error text.")
            else:
                lines.append("Omitted exact text is not recoverable: archive unavailable.")
        else:
            lines.append(f"message={text or code}")
        # Inspect only the bounded tail, not a multi-megabyte JSON/binary dump.
        tail = text[-cls.TAIL_LIMIT:]
        stderr = re.search(r'"stderr"\s*:\s*("(?:\\.|[^"\\])*")\s*}\s*$', tail, re.DOTALL)
        diagnostic = ""
        if stderr:
            try:
                diagnostic = next((line.strip() for line in reversed(json.loads(stderr[1]).splitlines()) if line.strip()), "")
            except ValueError:
                pass
        if not diagnostic:
            diagnostic = next((line.strip() for line in text[:512].splitlines() if line.strip()), code)
        handle.display = "\n".join(lines)
        handle.total_chars = len(text)
        handle.strategy = handle.view_label = handle.view_kind = "error"
        handle.view_desc = code
        handle.view_exactness = "derived" if long else "exact"
        handle.summary = f"{tool_name or 'tool'} failed: {diagnostic}"[:220]
        handle.token_estimate = estimate_tokens(handle.display)
        if handle.archive is not None:
            handle.content_id = handle.archive.id
        return handle

    @classmethod
    def build_bounded_display(
        cls, *, text: str, handle: ToolCallArchiveResult, max_chars: int,
    ) -> ToolCallArchiveResult:
        marker = f"[truncated by hard-limit fallback: {len(text)} chars total]\n"
        if handle.archive is not None:
            marker += f"content_id={handle.archive.id}\n"
        else:
            marker += "archive unavailable\n"
        available = max(0, min(cls.HEAD_LIMIT + cls.TAIL_LIMIT, max_chars - len(marker) - 128))
        handle.display = marker + cls.exact_excerpts(text, head_chars=available // 2, tail_chars=available - available // 2)
        handle.total_chars = len(text)
        handle.strategy = "bounded"
        handle.view_label = handle.view_kind = "bounded"
        handle.view_exactness = "derived"
        handle.token_estimate = estimate_tokens(handle.display)
        if handle.archive is not None:
            handle.content_id = handle.archive.id
        return handle

    def build_display(
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
        if record.metadata.get("auto_summary") is False or len(str(text or "")) <= self.FULL_LIMIT:
            view = self._full_view(record=record, tool_name=tool_name, text=text)
            return self._apply_view(archive_result, view, summary=record.summary)
        view = self._long_view(
            record=record,
            tool_name=tool_name,
            text=text,
            summary=record.summary,
        )
        return self._apply_view(archive_result, view, summary=record.summary)

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
    def _long_view(
        *,
        record: ArchivedContentRecord,
        tool_name: str,
        text: str,
        summary: str = "",
    ) -> ContentView:
        body = str(text or "")
        total = len(body)
        head_end = min(total, ToolResultViewService.HEAD_LIMIT)
        summary_text = str(summary or "").strip()[: ToolResultViewService.SUMMARY_LIMIT]
        summary_body = summary_text or "No semantic summary is available; use the exact excerpts and Archive reference."
        rendered = (
            f"derived={'true' if summary_text else 'false'}\n"
            f"chars={total}\n"
            f"tokens={int(record.token_estimate or estimate_tokens(body))}\n"
            f"{summary_body}\n\n"
            f"{ToolResultViewService.exact_excerpts(body)}\n\n"
            f'Use archive__read(content_id="{record.id}", view="content", offset={head_end}) '
            "to restore omitted exact text and image refs; use file__view for those images."
        )
        return ContentView(
            label=ContentViewLabel("summary", "" if summary_text else "unavailable"),
            body=rendered,
            content_id=record.id,
            source=str(tool_name or ""),
            chars=total,
            exactness=ContentExactness.DERIVED,
            preview=body[:head_end],
            record=record,
        )

    @classmethod
    def rebuild_long_display(
        cls,
        *,
        tool_name: str,
        text: str,
        archive_result: ToolCallArchiveResult,
        summary: str = "",
    ) -> ToolCallArchiveResult:
        record = archive_result.archive
        if record is None:
            return archive_result
        view = cls._long_view(
            record=record,
            tool_name=tool_name,
            text=text,
            summary=summary or record.summary,
        )
        return cls._apply_view(archive_result, view, summary=summary or record.summary)

    @staticmethod
    def _apply_view(handle: ToolCallArchiveResult, view: ContentView, *, summary: str = "") -> ToolCallArchiveResult:
        handle.display = view.render()
        handle.summary = str(summary or "")[: ToolResultViewService.SUMMARY_LIMIT]
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
