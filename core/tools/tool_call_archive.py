"""Archive-first tool call result handling."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from core.context.archive_store import ArchivedContentRecord, SessionArchiveStore, estimate_tokens, stringify_content
from core.context.archive_view_service import ArchiveViewService
from core.content.view_protocol import (
    ArchivePolicy,
    ContentExactness,
    ContentViewLabel,
    ToolResultViewPolicy,
    exact_view_from_text,
    summary_view_value,
)
from models.conversation import Conversation
from models.provider import Provider


@dataclass(frozen=True)
class ToolResultStrategy:
    archive_policy: ArchivePolicy = ArchivePolicy.INLINE
    view_policy: ToolResultViewPolicy = ToolResultViewPolicy.INLINE
    full_limit_chars: int = 8_192
    summary_timeout_ms: int = 15_000
    never_force_archive: bool = False


@dataclass
class ContentView:
    label: ContentViewLabel
    body: str
    content_id: str = ""
    digest: str = ""
    source: str = ""
    chars: int = 0
    token_estimate: int = 0
    exactness: ContentExactness = ContentExactness.EXACT
    summary_status: str = "none"
    view: str = "inline"
    preview: str = ""
    record: ArchivedContentRecord | None = None

    def render(self) -> str:
        body = str(self.body or "")
        if self.content_id:
            header = f"{self.label.bracketed}\ncontent_id={self.content_id}"
            if self.source:
                header += f"\nsource={self.source}"
            if self.chars:
                header += f" chars={self.chars}"
            if self.digest:
                header += f" digest={self.digest[:16]}"
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
    digest: str = ""
    token_estimate: int = 0
    archive: ArchivedContentRecord | None = None
    summary_status: str = "none"
    compression_required: bool = False
    content_kind: str = "text"
    view_label: str = "inline"
    view_kind: str = "inline"
    view_desc: str = ""
    view_exactness: str = "exact"

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
        if self.digest:
            metadata["tool_result_digest"] = self.digest
        if self.hint:
            metadata["tool_result_hint"] = self.hint
        if self.summary:
            metadata["tool_result_summary"] = self.summary
        if self.summary_status:
            metadata["tool_result_summary_status"] = self.summary_status
        if self.compression_required:
            metadata["tool_result_compression_required"] = True
        if self.content_kind:
            metadata["tool_result_content_kind"] = self.content_kind
        if self.archive is not None:
            metadata["content_id"] = self.archive.id
            metadata["archive_kind"] = self.archive.kind
            metadata["archive_ref"] = self.archive.original_ref
            metadata["archive_status"] = self.archive.status
            metadata["archive_record"] = self.archive.to_dict()
        return metadata


class ToolCallArchiveService:
    """Archive important tool outputs before model-visible display shaping."""

    ARCHIVE_THRESHOLD_CHARS = 32_000
    VIEW_POLICY_ARCHIVE_CHARS = 8_192
    DEFAULT_SUMMARY_TIMEOUT_MS = 15_000
    DEFAULT_INLINE = ToolResultStrategy()
    NEVER_ARCHIVE = ToolResultStrategy(
        archive_policy=ArchivePolicy.NEVER_ARCHIVE,
        view_policy=ToolResultViewPolicy.NEVER_ARCHIVE,
        never_force_archive=True,
    )
    BUILT_IN_STRATEGIES: dict[str, ToolResultStrategy] = {
        "file__read": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.LINE_OR_SUMMARY, 8_192, 45_000),
        "file__search": ToolResultStrategy(ArchivePolicy.INLINE, ToolResultViewPolicy.FULL_OR_SUMMARY),
        "grep": ToolResultStrategy(ArchivePolicy.INLINE, ToolResultViewPolicy.FULL_OR_SUMMARY),
        "file__list": ToolResultStrategy(ArchivePolicy.INLINE, ToolResultViewPolicy.FULL_OR_SUMMARY),
        "ls": ToolResultStrategy(ArchivePolicy.INLINE, ToolResultViewPolicy.FULL_OR_SUMMARY),
        "skill__read_resource": NEVER_ARCHIVE,
        "content__list": NEVER_ARCHIVE,
        "content__read": NEVER_ARCHIVE,
        "web__search": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.FULL_OR_SUMMARY, 8_192, 30_000),
        "web__fetch": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.FULL_OR_SUMMARY, 4_096, 60_000),
        "file__write": NEVER_ARCHIVE,
        "file__edit": NEVER_ARCHIVE,
        "file__delete": NEVER_ARCHIVE,
        "file__patch": NEVER_ARCHIVE,
        "shell__run": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.FULL_OR_SUMMARY, 8_192, 30_000),
        "shell__start": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.FULL_OR_SUMMARY, 8_192, 30_000),
        "shell__logs": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.FULL_OR_SUMMARY, 8_192, 30_000),
        "shell__status": NEVER_ARCHIVE,
        "shell__wait": NEVER_ARCHIVE,
        "shell__kill": NEVER_ARCHIVE,
        "python__exec": ToolResultStrategy(ArchivePolicy.ARCHIVE, ToolResultViewPolicy.FULL_OR_SUMMARY, 8_192, 30_000),
        "state__memory": NEVER_ARCHIVE,
        "state__todo": NEVER_ARCHIVE,
        "state__artifact": NEVER_ARCHIVE,
        "user__ask": NEVER_ARCHIVE,
        "skill__load": NEVER_ARCHIVE,
        "agent__run": NEVER_ARCHIVE,
        "agent__complete": NEVER_ARCHIVE,
        "agent__switch": NEVER_ARCHIVE,
    }

    def __init__(self, work_dir: str, conversation_id: object = None):
        self.archive_store = SessionArchiveStore(work_dir, conversation_id=conversation_id)

    def process(
        self,
        *,
        tool_name: str,
        raw_text: Any,
        tool_call_id: str | None = None,
        tool_args: dict[str, Any] | None = None,
        seq_id: int = 0,
    ) -> ToolCallArchiveResult:
        text = stringify_content(raw_text)
        strategy = self._strategy(tool_name)
        if self._should_archive(tool_name, text, strategy):
            return self._archive(tool_name, text, tool_call_id, tool_args or {}, seq_id)
        return self._inline(tool_name, text)

    def _strategy(self, tool_name: str) -> ToolResultStrategy:
        name = str(tool_name or "")
        if name in self.BUILT_IN_STRATEGIES:
            return self.BUILT_IN_STRATEGIES[name]
        if str(tool_name or "").startswith("capability__"):
            return self.NEVER_ARCHIVE
        if str(tool_name or "").startswith("mcp__"):
            return ToolResultStrategy(ArchivePolicy.INLINE, ToolResultViewPolicy.FULL_OR_SUMMARY, 8_192, 30_000)
        return self.DEFAULT_INLINE

    def _should_archive(self, tool_name: str, text: str, strategy: ToolResultStrategy) -> bool:
        if strategy.archive_policy == ArchivePolicy.NEVER_ARCHIVE:
            return False
        if strategy.archive_policy == ArchivePolicy.ARCHIVE:
            return True
        if strategy.never_force_archive:
            return False
        if str(tool_name or "").startswith("mcp__") and len(text) > self.VIEW_POLICY_ARCHIVE_CHARS:
            return True
        if self._should_force_archive(tool_name, text):
            return True
        if len(text) > self.ARCHIVE_THRESHOLD_CHARS:
            return True
        return False

    @staticmethod
    def _should_force_archive(tool_name: str, text: str) -> bool:
        name = str(tool_name or "")
        raw = str(text or "")
        if name in {"web__fetch", "web__search"} and raw.strip():
            return True
        if name in {"shell__run", "shell__start", "shell__logs", "python__exec"} and len(raw) > ToolCallArchiveService.VIEW_POLICY_ARCHIVE_CHARS:
            return True
        if name in {"file__search", "file__list"} and len(raw) > ToolCallArchiveService.VIEW_POLICY_ARCHIVE_CHARS:
            return True
        if name == "file__read" and len(raw) >= 8_000:
            return True
        if name == "file__read" and re.search(r"^Lines \d+-\d+ of \d+:", raw[:80]) and len(raw) >= 4_000:
            return True
        if re.search(r"data:image/|\[Image:|\"type\"\s*:\s*\"image\"|\bmimeType\b", raw[:4000], re.I):
            return True
        if len(raw) >= 8_000 and raw.lstrip()[:1] in {"{", "["}:
            return True
        if len(raw) >= 8_000 and ("<html" in raw[:2000].lower() or "<!doctype html" in raw[:2000].lower()):
            return True
        return False

    def _inline(self, tool_name: str, text: str) -> ToolCallArchiveResult:
        digest = self._digest(text)
        label = self._leading_view_label(text)
        exactness = self._exactness_for_label(label)
        return ToolCallArchiveResult(
            display=text,
            total_chars=len(text),
            strategy="inline",
            summary=self._inline_summary(tool_name, text),
            digest=digest,
            token_estimate=estimate_tokens(text),
            summary_status="none",
            content_kind=self._detect_content_kind(tool_name, text),
            view_label=label.value if label else "inline",
            view_kind=label.kind if label else "inline",
            view_desc=label.semantic_desc if label else "",
            view_exactness=exactness.value,
        )

    def _archive(
        self,
        tool_name: str,
        text: str,
        tool_call_id: str | None,
        tool_args: dict[str, Any],
        seq_id: int,
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
                "summary_status": "pending",
                "compression_required": True,
                "references": self._extract_references(text),
            },
            input_payload={
                "tool_name": str(tool_name or ""),
                "tool_call_id": str(tool_call_id or ""),
                "arguments": dict(tool_args or {}),
            },
        )
        full_path = str(self.archive_store.resolve_ref(record.original_ref))
        display = (
            f"{ContentViewLabel.build('archive', 'pending')}\n"
            f"content_id={record.id}\n"
            f"source={tool_name or 'tool'} chars={len(text)} digest={record.digest[:16]}\n"
            "Original output was archived intact."
        )
        hint = f"Use content__read with content_id={record.id} to access archived output."
        return ToolCallArchiveResult(
            display=display,
            full_path=full_path,
            total_chars=len(text),
            is_processed=True,
            strategy="archive",
            hint=hint,
            summary=record.summary,
            digest=record.digest,
            token_estimate=record.token_estimate,
            archive=record,
            summary_status=record.summary_status,
            compression_required=not bool(record.summary),
            content_kind=content_kind,
            view_label="archive:pending",
            view_kind="archive",
            view_desc="pending",
            view_exactness="pending",
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
        if label.kind in {"full", "line", "char", "summary", "archive"}:
            return label
        return None

    @staticmethod
    def _exactness_for_label(label: ContentViewLabel | None) -> ContentExactness:
        if label is None:
            return ContentExactness.EXACT
        if label.kind in {"full", "line", "char"}:
            return ContentExactness.EXACT
        if label.kind == "summary" and label.desc in {"pending", "failed"}:
            return ContentExactness.PENDING
        if label.kind == "archive":
            return ContentExactness.PENDING
        if label.kind == "summary":
            return ContentExactness.DERIVED
        return ContentExactness.EXACT

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


class ToolResultViewService:
    """Choose the model-visible view for an archived tool result."""

    SMALL_FULL_LIMIT = 8_192
    WEB_FETCH_FULL_LIMIT = 4_096
    DEFAULT_SUMMARY_TIMEOUT_MS = ToolCallArchiveService.DEFAULT_SUMMARY_TIMEOUT_MS

    def __init__(
        self,
        *,
        work_dir: str,
        conversation_id: object = None,
        client: Any = None,
        provider: Provider | None = None,
        conversation: Conversation | None = None,
    ) -> None:
        self.work_dir = work_dir
        self.conversation_id = conversation_id
        self.client = client
        self.provider = provider
        self.conversation = conversation

    async def build_display(
        self,
        *,
        tool_name: str,
        text: str,
        archive_result: ToolCallArchiveResult,
        timeout_ms: int | None = None,
    ) -> ToolCallArchiveResult:
        if archive_result.archive is None:
            archive_result.view_label = "inline"
            archive_result.view_kind = "inline"
            archive_result.view_desc = ""
            archive_result.view_exactness = "exact"
            return archive_result

        record = archive_result.archive
        strategy = ToolCallArchiveService(self.work_dir, conversation_id=self.conversation_id)._strategy(tool_name)
        if self._should_return_full(tool_name, text, strategy):
            view = self._full_view(record=record, tool_name=tool_name, text=text)
            return self._apply_view(archive_result, view, summary=record.summary)

        summary_view = await self._summary_view(
            record=record,
            tool_name=tool_name,
            text=text,
            timeout_ms=timeout_ms or strategy.summary_timeout_ms,
        )
        return self._apply_view(archive_result, summary_view, summary=summary_view.body if summary_view.exactness == ContentExactness.DERIVED else record.summary)

    @staticmethod
    def _should_return_full(tool_name: str, text: str, strategy: ToolResultStrategy) -> bool:
        if strategy.view_policy == ToolResultViewPolicy.SUMMARY:
            return False
        if strategy.view_policy == ToolResultViewPolicy.LINE_OR_SUMMARY and str(tool_name or "") == "file__read":
            exact = exact_view_from_text(tool_name, text)
            if exact.kind == "line":
                return True
        return len(str(text or "")) <= int(strategy.full_limit_chars or ToolResultViewService.SMALL_FULL_LIMIT)

    @staticmethod
    def _full_view(*, record: ArchivedContentRecord, tool_name: str, text: str) -> ContentView:
        label = exact_view_from_text(tool_name, text)
        return ContentView(
            label=label,
            body=str(text or ""),
            content_id=record.id,
            digest=record.digest,
            source=str(tool_name or ""),
            chars=len(str(text or "")),
            token_estimate=record.token_estimate,
            exactness=ContentExactness.EXACT,
            summary_status=record.summary_status,
            view=label.value,
            record=record,
        )

    async def _summary_view(
        self,
        *,
        record: ArchivedContentRecord,
        tool_name: str,
        text: str,
        timeout_ms: int,
    ) -> ContentView:
        service = ArchiveViewService(
            work_dir=self.work_dir,
            conversation_id=self.conversation_id,
            client=self.client,
            provider=self.provider,
            conversation=self.conversation,
        )
        result = await service.get_or_create_summary(
            record.id,
            mode="balanced",
            wait=bool(self.client and self.provider),
            timeout_ms=timeout_ms,
            refresh=False,
            purpose=f"initial_tool_result:{tool_name}",
        )
        latest = result.record or record
        if result.text:
            return ContentView(
                label=ContentViewLabel.parse(result.label),
                body=result.text,
                content_id=latest.id,
                digest=latest.digest,
                source=str(tool_name or ""),
                chars=int(latest.size or len(text)),
                token_estimate=int(latest.token_estimate or estimate_tokens(text)),
                exactness=ContentExactness.DERIVED,
                summary_status="complete",
                view=ContentViewLabel.parse(result.label).value,
                record=latest,
            )
        preview = "" if self._is_opaque_preview(tool_name, text) else self._preview(text)
        body = (
            "Summary is not ready. Original output was archived intact.\n"
            "Use content__read(content_id, view=\"summary\"|\"full\"|\"lines\"|\"chars\") to retrieve it."
        )
        if preview:
            body += f"\n\nExact preview:\n{preview}"
        return ContentView(
            label=ContentViewLabel("summary", "pending"),
            body=body,
            content_id=latest.id,
            digest=latest.digest,
            source=str(tool_name or ""),
            chars=int(latest.size or len(text)),
            token_estimate=int(latest.token_estimate or estimate_tokens(text)),
            exactness=ContentExactness.PENDING,
            summary_status=latest.summary_status,
            view="summary",
            preview=preview,
            record=latest,
        )

    @staticmethod
    def _apply_view(handle: ToolCallArchiveResult, view: ContentView, *, summary: str = "") -> ToolCallArchiveResult:
        handle.display = view.render()
        handle.summary = str(summary or "")
        if not handle.summary and view.exactness == ContentExactness.EXACT:
            handle.summary = ToolCallArchiveService._inline_summary(view.source, view.body)
        handle.summary_status = view.summary_status
        handle.compression_required = view.exactness == ContentExactness.PENDING
        handle.view_label = view.label.value
        handle.view_kind = view.label.kind
        handle.view_desc = view.label.semantic_desc
        handle.view_exactness = view.exactness.value
        if view.record is not None:
            handle.archive = view.record
        return handle

    @staticmethod
    def _preview(text: str, limit: int = 1200) -> str:
        clean = str(text or "").strip()
        if not clean:
            return ""
        if len(clean) <= limit:
            return clean
        return clean[:limit].rstrip() + "\n...[preview truncated]"

    @staticmethod
    def _is_opaque_preview(tool_name: str, text: str) -> bool:
        raw = str(text or "")
        if str(tool_name or "") == "file__read" and re.search(r"data:image/", raw[:4000], re.I):
            return True
        return bool(re.search(r"data:image/|\[Image:|\"type\"\s*:\s*\"image\"|\bmimeType\b", raw[:4000], re.I))
