"""Request-only shaping of recoverable tool results."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pycat.core.content.archive_store import SessionArchiveStore, estimate_tokens
from pycat.core.content.view_protocol import ContentExactness, TOOL_SUMMARY_PROJECTION_CHARS
from pycat.models.conversation import Conversation, Message, normalize_tool_result, tool_call_name


EXACT_VIEW_KINDS = {"content", "full", "line", "char"}
CCR_PRESSURE_LEVELS = {"tight", "compact"}
CCR_EXCERPT_CHARS = 320
MIN_CCR_SAVINGS_TOKENS = 512
NORMAL_EXACT_TOOL_BATCHES = 8
TIGHT_EXACT_TOOL_BATCHES = 3


@dataclass
class ToolReplayItem:
    tool_call_id: str = ""
    tool_name: str = ""
    content_id: str = ""
    view_kind: str = ""
    exactness: str = ""
    chars: int = 0
    replay_view: str = "inline"
    reason: str = ""


@dataclass
class ToolReplayPlan:
    items: list[ToolReplayItem] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    token_estimate: int = 0
    reason: str = "tool_replay_planner"
    compact_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _ReplayCandidate:
    message_index: int
    result_index: int
    tool_call: dict[str, Any]
    payload: dict[str, Any]
    metadata: dict[str, Any]
    exact_chars: int
    image_count: int = 0


class ToolReplayPlanner:
    """Shape copied request messages without mutating persisted history.

    Tool results are archived before this planner runs. Normal requests retain
    the latest 8 completed tool batches; tight requests retain the latest 3.
    Every older replacement is a deterministic CCR marker whose original text
    and images stay recoverable from Archive.
    """

    def __init__(
        self,
        *,
        conversation: Conversation | None = None,
        replay_pressure: str = "normal",
        visible_tool_names: set[str] | None = None,
    ) -> None:
        self.conversation = conversation
        self.replay_pressure = str(replay_pressure or "normal").strip().lower() or "normal"
        self.visible_tool_names = (
            {str(name).strip() for name in visible_tool_names if str(name or "").strip()}
            if visible_tool_names is not None
            else None
        )
        self.plan = ToolReplayPlan()

    def prepare_messages(self, messages: list[Message]) -> ToolReplayPlan:
        self.plan = ToolReplayPlan()
        candidates: list[_ReplayCandidate] = []

        for message_index, msg in enumerate(messages):
            if msg.role != "assistant" or not msg.tool_calls:
                continue
            for index, tool_call in enumerate(msg.tool_calls or []):
                if not isinstance(tool_call, dict) or tool_call.get("result") is None:
                    continue
                payload = normalize_tool_result(tool_call.get("result"))
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                metadata = self._fresh_archive_metadata(dict(metadata))
                self._fill_basic_metadata(metadata, tool_call=tool_call, payload=payload)

                candidates.append(
                    _ReplayCandidate(
                        message_index=message_index,
                        result_index=index,
                        tool_call=tool_call,
                        payload=payload,
                        metadata=metadata,
                        exact_chars=len(self._render_inline_result(payload)),
                        image_count=len([item for item in (tool_call.get("result_images") or []) if str(item or "").strip()]),
                    )
                )

        self._apply_replay_policy(candidates)

        visible_chars = 0
        for candidate in candidates:
            candidate.payload["metadata"] = candidate.metadata
            candidate.tool_call["result"] = candidate.payload
            candidate.tool_call["result_metadata"] = dict(candidate.metadata)
            self.plan.items.append(
                self._plan_item(candidate.tool_call, candidate.payload, candidate.metadata)
            )
            visible_chars += len(self.render_tool_result(candidate.payload))

        self.plan.token_estimate = max(0, visible_chars // 4)
        return self.plan

    def render_tool_result(self, result: Any) -> str:
        payload = normalize_tool_result(result)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = self._fresh_archive_metadata(dict(metadata))
        if str(metadata.get("tool_result_replay_view") or "").strip() == "ccr":
            return self._render_ccr_result(payload, metadata)
        refreshed = self._render_refreshed_long_view(payload, metadata)
        if refreshed:
            return refreshed
        return self._render_inline_result(payload)

    @staticmethod
    def _render_refreshed_long_view(payload: dict[str, Any], metadata: dict[str, Any]) -> str:
        summary = str(metadata.get("tool_result_summary") or payload.get("summary") or "").strip()
        content = payload.get("content")
        if not summary or not isinstance(content, str) or not content.startswith("[summary:unavailable]"):
            return ""
        marker = content.find("\n\n[char:")
        if marker < 0:
            return ""
        content_id = str(metadata.get("content_id") or "").strip()
        try:
            chars = int(metadata.get("tool_result_chars") or metadata.get("archive_size") or 0)
        except Exception:
            chars = 0
        try:
            tokens = int(metadata.get("tool_result_tokens_estimate") or 0)
        except Exception:
            tokens = 0
        lines = ["[summary]"]
        if content_id:
            lines.append(f"content_id={content_id}")
        lines.extend(("derived=true", f"chars={chars}", f"tokens={tokens}", summary[:TOOL_SUMMARY_PROJECTION_CHARS]))
        return "\n".join(lines) + content[marker:]

    def _fill_basic_metadata(
        self,
        metadata: dict[str, Any],
        *,
        tool_call: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        content = payload.get("content")
        view_kind = str(metadata.get("tool_result_view_kind") or "").strip().lower()
        view_desc = str(metadata.get("tool_result_view_desc") or "").strip().lower()
        exactness = str(metadata.get("tool_result_exactness") or "").strip().lower()
        if (not view_kind or view_kind == "inline") and isinstance(content, str):
            view_kind, view_desc, exactness = self._infer_view_from_content(content, exactness)
            if view_kind:
                metadata.setdefault("tool_result_view_kind", view_kind)
                metadata.setdefault("tool_result_view", view_kind if not view_desc else f"{view_kind}:{view_desc}")
                metadata.setdefault("tool_result_view_desc", view_desc)
                metadata.setdefault("tool_result_exactness", exactness)

        content_id = str(metadata.get("content_id") or "").strip()
        if not content_id and isinstance(content, str):
            content_id = self._parse_content_id(content)
            if content_id:
                metadata["content_id"] = content_id
        metadata.setdefault("name", tool_call_name(tool_call) or "tool")

    def _apply_replay_policy(
        self,
        candidates: list[_ReplayCandidate],
    ) -> None:
        if self.visible_tool_names is not None and "archive__read" not in self.visible_tool_names:
            for candidate in candidates:
                self._restore_exact(candidate)
                self._mark_live(candidate.metadata, reason="recovery_tool_unavailable")
            return
        tight = self.replay_pressure in CCR_PRESSURE_LEVELS
        keep_batches = TIGHT_EXACT_TOOL_BATCHES if tight else NORMAL_EXACT_TOOL_BATCHES
        batches: dict[int, list[_ReplayCandidate]] = {}
        for candidate in candidates:
            batches.setdefault(candidate.message_index, []).append(candidate)
        batch_indexes = sorted(batches)
        protected = set(batch_indexes[-keep_batches:])
        for message_index in batch_indexes:
            batch = batches[message_index]
            if message_index in protected:
                for candidate in batch:
                    self._mark_live(
                        candidate.metadata,
                        reason="recent_tight_tool_batch" if tight else "recent_normal_tool_batch",
                    )
                continue
            if any(not self._is_recoverable(candidate) for candidate in batch):
                for candidate in batch:
                    self._mark_live(candidate.metadata, reason="unrecoverable_tool_batch")
                continue
            exact_tokens = sum(estimate_tokens(self._render_inline_result(candidate.payload)) for candidate in batch)
            ccr_tokens = sum(estimate_tokens(self._render_ccr_result(candidate.payload, candidate.metadata)) for candidate in batch)
            if exact_tokens - ccr_tokens < MIN_CCR_SAVINGS_TOKENS:
                for candidate in batch:
                    self._mark_live(candidate.metadata, reason="ccr_no_savings")
                continue
            for candidate in batch:
                self._mark_ccr(candidate, reason="tool_batch_window")

    def _restore_exact(self, candidate: _ReplayCandidate) -> None:
        content_id = str(candidate.metadata.get("content_id") or "").strip()
        if not content_id or self.conversation is None:
            return
        store = SessionArchiveStore(
            str(getattr(self.conversation, "work_dir", "") or ""),
            conversation_id=getattr(self.conversation, "id", None),
            data_dir=self.conversation.data_dir,
        )
        record = store.read_record(content_id)
        if record is None:
            return
        try:
            candidate.payload["content"] = store.read_original(record)
        except Exception:
            return
        images = store.read_images(record)
        if images:
            candidate.payload["images"] = list(images)
            candidate.tool_call["result_images"] = list(images)

    def _mark_ccr(self, candidate: _ReplayCandidate, *, reason: str) -> None:
        candidate.metadata["tool_result_replay_view"] = "ccr"
        candidate.metadata["tool_result_replay_reason"] = reason
        candidate.tool_call["result_images"] = []
        candidate.payload.pop("images", None)
        degraded_id = str(
            candidate.metadata.get("content_id")
            or candidate.tool_call.get("id")
            or candidate.result_index
        )
        if degraded_id and degraded_id not in self.plan.degraded:
            self.plan.degraded.append(degraded_id)

    @classmethod
    def _mark_live(cls, metadata: dict[str, Any], *, reason: str) -> None:
        metadata["tool_result_replay_view"] = cls._exact_or_inline(metadata)
        metadata["tool_result_replay_reason"] = reason

    @staticmethod
    def _is_recoverable(candidate: _ReplayCandidate) -> bool:
        metadata = candidate.metadata
        if not bool(metadata.get("archive_available")):
            return False
        if candidate.image_count:
            if int(metadata.get("archive_image_count") or 0) < candidate.image_count:
                return False
            if not bool(metadata.get("archive_images_restorable")):
                return False
        return bool(str(metadata.get("content_id") or "").strip())

    @staticmethod
    def _exact_or_inline(metadata: dict[str, Any]) -> str:
        view_kind = str(metadata.get("tool_result_view_kind") or "").strip().lower()
        exactness = str(metadata.get("tool_result_exactness") or "").strip().lower()
        return "exact" if view_kind in EXACT_VIEW_KINDS and exactness == ContentExactness.EXACT.value else "inline"

    def _render_ccr_result(self, payload: dict[str, Any], metadata: dict[str, Any]) -> str:
        content_id = str(metadata.get("content_id") or "").strip()
        source = str(metadata.get("name") or "tool").strip() or "tool"
        total_chars = self._result_chars(payload, metadata)
        summary = str(metadata.get("tool_result_summary") or payload.get("summary") or "").strip()
        excerpt = self._exact_excerpt(payload.get("content"))

        lines = ["[tool_result:archived]", f"source={source}"]
        if total_chars:
            lines.append(f"chars={total_chars}")
        if content_id:
            lines.append(f"content_id={content_id}")
        if summary:
            lines.append(f"summary={summary[:500]}")
        if excerpt:
            lines.extend(("exact_excerpt:", excerpt))
        if content_id:
            lines.append(
                f'Use archive__read(content_id="{content_id}", view="summary") for the full summary, '
                f'or archive__read(content_id="{content_id}", view="content", offset=0) for exact content and images.'
            )
        return "\n".join(lines)

    @staticmethod
    def _render_inline_result(payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)

    @staticmethod
    def _exact_excerpt(content: Any) -> str:
        if not isinstance(content, str):
            return ""
        if "\nexact_excerpt:\n" in content:
            content = content.split("\nexact_excerpt:\n", 1)[1]
        first_char_view = content.find("[char:")
        if first_char_view >= 0:
            content = content[first_char_view:]
            next_char_view = content.find("\n\n[char:")
            if next_char_view >= 0:
                content = content[:next_char_view]
        lines = content.strip().splitlines()
        while lines and (
            (lines[0].startswith("[") and "]" in lines[0][:80])
            or lines[0].startswith(
                ("content_id=", "exact=", "next_offset=", "char_range=", "derived=", "chars=", "tokens=")
            )
        ):
            lines.pop(0)
        excerpt = "\n".join(lines).strip()
        if len(excerpt) <= CCR_EXCERPT_CHARS:
            return excerpt
        return excerpt[:CCR_EXCERPT_CHARS].rstrip() + "..."

    def _fresh_archive_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        content_id = str(metadata.get("content_id") or "").strip()
        if isinstance(metadata.get("archive_record"), dict):
            content_id = content_id or str(metadata["archive_record"].get("id") or "").strip()
            metadata.pop("archive_record", None)
        conversation = self.conversation
        if not content_id or conversation is None:
            metadata["archive_available"] = False
            return metadata
        work_dir = str(getattr(conversation, "work_dir", "") or "").strip()
        store = SessionArchiveStore(work_dir, conversation_id=getattr(conversation, "id", None), data_dir=getattr(conversation, "data_dir", None))
        try:
            record = store.read_record(content_id)
        except Exception:
            record = None
        if record is None:
            metadata["archive_available"] = False
            return metadata

        fresh = dict(metadata)
        image_count = int((record.metadata or {}).get("image_count") or 0)
        fresh["content_id"] = record.id
        fresh["archive_available"] = True
        fresh["archive_size"] = int(record.size or 0)
        fresh["archive_updated_seq"] = int(record.updated_seq or record.created_seq or 0)
        fresh["archive_image_count"] = image_count
        fresh["archive_images_restorable"] = store.images_are_restorable(record, expected_count=image_count)
        if record.source:
            fresh.setdefault("name", record.source)
        if record.summary:
            fresh["tool_result_summary"] = record.summary
        return fresh

    def _plan_item(
        self,
        tool_call: dict[str, Any],
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ToolReplayItem:
        return ToolReplayItem(
            tool_call_id=str(tool_call.get("id") or ""),
            tool_name=tool_call_name(tool_call) or str(metadata.get("name") or ""),
            content_id=str(metadata.get("content_id") or ""),
            view_kind=str(metadata.get("tool_result_view_kind") or ""),
            exactness=str(metadata.get("tool_result_exactness") or ""),
            chars=self._result_chars(payload, metadata),
            replay_view=str(metadata.get("tool_result_replay_view") or ""),
            reason=str(metadata.get("tool_result_replay_reason") or ""),
        )

    @staticmethod
    def _result_chars(payload: dict[str, Any], metadata: dict[str, Any]) -> int:
        try:
            chars = int(metadata.get("tool_result_chars") or metadata.get("archive_size") or 0)
        except Exception:
            chars = 0
        content = payload.get("content")
        return chars if chars > 0 else len(content) if isinstance(content, str) else 0

    @staticmethod
    def _infer_view_from_content(content: str, exactness: str) -> tuple[str, str, str]:
        raw = str(content or "").lstrip()
        if not raw.startswith("[") or "]" not in raw[:80]:
            return "", "", exactness or ContentExactness.EXACT.value
        label = raw[1:raw.index("]")]
        kind, _, desc = label.partition(":")
        kind = kind.strip().lower()
        desc = desc.strip().lower()
        if kind in EXACT_VIEW_KINDS:
            return kind, desc, ContentExactness.EXACT.value
        return kind, desc, exactness or ContentExactness.EXACT.value

    @staticmethod
    def _parse_content_id(content: str) -> str:
        for line in str(content or "").splitlines()[:8]:
            clean = line.strip()
            if clean.startswith("content_id="):
                return clean.split("=", 1)[1].strip()
        return ""
