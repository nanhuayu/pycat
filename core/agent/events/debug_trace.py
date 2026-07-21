"""Flat debug trace recording for one user request.

The trace is intentionally small: ``events.jsonl`` is the fact source, and
request/response/stream payload files are optional debugging attachments.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from models.conversation import Conversation

logger = logging.getLogger(__name__)


TRACE_SCHEMA_VERSION = 2

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|secret|cookie|password|proxy)",
    re.IGNORECASE,
)
_GENERIC_TOKEN_KEYS = {"token", "id_token", "session_token"}
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[a-z0-9._~+/=-]{8,}"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def _safe_name(value: object, *, limit: int = 120) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "")).strip("._-")
    return (text or "default")[:limit]


def resolve_debug_trace_dir(*, conversation_id: str, work_dir: str = ".") -> Path:
    """Return the canonical debug directory for one conversation."""

    root = Path(work_dir or ".").expanduser().resolve()
    session_id = _safe_name(conversation_id or "default")
    return root / ".pycat" / "sessions" / session_id / "debug"


def _truncate_text(text: str, limit: int) -> str | dict[str, Any]:
    if len(text) <= limit:
        return text
    return {
        "truncated": True,
        "original_length": len(text),
        "preview": text[:limit],
    }


def _purpose_label(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "main").strip().lower()).strip("-._")
    return clean or "main"


def _json_default(value: Any) -> Any:
    try:
        if hasattr(value, "to_dict"):
            return value.to_dict()
    except Exception:
        pass
    try:
        return str(value)
    except Exception:
        return repr(value)


@dataclass(frozen=True)
class DebugTraceContext:
    """Current trace attachment point.

    ``sink`` is shared across the whole request; the other fields describe where
    child events should attach in the tree.
    """

    sink: Any = None
    conversation_id: str = ""
    request_id: str = ""
    turn: int = 0
    node_id: str = ""
    parent_id: str = ""
    subtask_id: str = ""
    tool_call_id: str = ""
    scope_id: str = ""
    default_purpose: str = "main"
    refs: dict[str, str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.sink and getattr(self.sink, "enabled", False))

    def with_turn(self, turn: int) -> "DebugTraceContext":
        return replace(self, turn=max(0, int(turn or 0)))

    def with_purpose(self, purpose: str) -> "DebugTraceContext":
        return replace(self, default_purpose=_purpose_label(purpose))

    def with_refs(self, refs: dict[str, str] | None) -> "DebugTraceContext":
        return replace(self, refs=dict(refs or {}))

    def child(
        self,
        *,
        node_id: str = "",
        parent_id: str = "",
        subtask_id: str = "",
        tool_call_id: str = "",
        scope_id: str = "",
        default_purpose: str = "",
    ) -> "DebugTraceContext":
        return replace(
            self,
            node_id=str(node_id or self.node_id or ""),
            parent_id=str(parent_id or self.node_id or self.parent_id or ""),
            subtask_id=str(subtask_id or self.subtask_id or ""),
            tool_call_id=str(tool_call_id or self.tool_call_id or ""),
            scope_id=str(scope_id or self.scope_id or ""),
            default_purpose=_purpose_label(default_purpose or self.default_purpose or "main"),
            refs=self.refs,
        )

    def record_event(self, **kwargs: Any) -> None:
        if self.enabled:
            self.sink.record_event(self, **kwargs)


class NullDebugTraceSink:
    enabled = False
    capture_payloads = False
    capture_stream = False

    def root_context(self, **_: Any) -> DebugTraceContext:
        return DebugTraceContext(sink=self)

    def record_event(self, *_: Any, **__: Any) -> None:
        return None

    def start_llm(self, context: DebugTraceContext | None = None, **_: Any) -> DebugTraceContext:
        return context or self.root_context()

    def write_json(self, *_: Any, **__: Any) -> str:
        return ""

    def open_stream_file(self, *_: Any, **__: Any) -> TextIO | None:
        return None

    def tool_payload_refs(self, *_: Any, **__: Any) -> dict[str, str]:
        return {}


class DebugTraceSink:
    """Write request trace events under ``.pycat/sessions/<id>/debug``."""

    enabled = True

    def __init__(
        self,
        *,
        conversation_id: str,
        request_id: str,
        work_dir: str = ".",
        capture_payloads: bool = False,
        capture_stream: bool = False,
        max_string_chars: int = 12_000,
    ) -> None:
        self.conversation_id = str(conversation_id or "").strip()
        self.request_id = str(request_id or "").strip()
        self.work_dir = Path(work_dir or ".").expanduser().resolve()
        self.capture_payloads = bool(capture_payloads)
        self.capture_stream = bool(capture_stream)
        self.max_string_chars = max(1000, int(max_string_chars or 12_000))
        self.session_id = _safe_name(self.conversation_id or "default")
        self.debug_dir = resolve_debug_trace_dir(
            conversation_id=self.conversation_id,
            work_dir=str(self.work_dir),
        )
        self.events_path = self.debug_dir / "events.jsonl"
        self._lock = threading.Lock()
        self._llm_count = 0
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.debug("Failed to create debug trace dir %s: %s", self.debug_dir, exc)

    def root_context(self, *, parent_id: str = "run") -> DebugTraceContext:
        return DebugTraceContext(
            sink=self,
            conversation_id=self.conversation_id,
            request_id=self.request_id,
            parent_id=parent_id,
            default_purpose="main",
        )

    def turn_node_id(self, turn: int, *, scope_id: str = "") -> str:
        prefix = _safe_name(scope_id, limit=48) if scope_id else ""
        base = f"turn{max(0, int(turn or 0)):02d}"
        return f"{prefix}-{base}" if prefix else base

    def tool_node_id(self, *, tool_call_id: str = "", tool_name: str = "") -> str:
        clean_call = _safe_name(tool_call_id, limit=80) if tool_call_id else ""
        if clean_call:
            return f"tool-{clean_call}"
        return f"tool-{_safe_name(tool_name or str(int(time.time() * 1000)), limit=80)}"

    def subtask_node_id(self, subtask_id: str) -> str:
        return f"subtask-{_safe_name(subtask_id or str(int(time.time() * 1000)), limit=80)}"

    def tool_payload_refs(
        self,
        context: DebugTraceContext,
        *,
        turn: int = 0,
        tool_name: str = "",
    ) -> dict[str, str]:
        if not self.capture_payloads:
            return {}
        turn_number = max(0, int(turn or context.turn or 0))
        node_id = _safe_name(context.node_id or tool_name or "tool", limit=100)
        clean_tool = _purpose_label(tool_name or "tool")
        prefix = (
            f"{int(time.time() * 1000)}__req-{self.request_id}"
            f"__t{turn_number:02d}__{node_id}-{clean_tool}"
        )
        return {
            "request": f"{prefix}__request.json",
            "response": f"{prefix}__response.json",
        }

    def record_event(
        self,
        context: DebugTraceContext | None = None,
        *,
        kind: str,
        phase: str = "event",
        turn: int | None = None,
        node_id: str = "",
        parent_id: str = "",
        name: str = "",
        status: str = "",
        duration_ms: int = 0,
        tool_call_id: str = "",
        tool_name: str = "",
        subtask_id: str = "",
        refs: dict[str, str] | None = None,
        summary: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        ctx = context or self.root_context()
        now = time.time()
        event = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "time": now,
            "ts": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "turn": int(turn if turn is not None else (ctx.turn or 0)),
            "node_id": str(node_id or ctx.node_id or ""),
            "parent_id": str(parent_id or ctx.parent_id or ""),
            "kind": str(kind or "event"),
            "phase": str(phase or "event"),
            "name": str(name or ""),
            "status": str(status or ""),
            "duration_ms": max(0, int(duration_ms or 0)),
            "tool_call_id": str(tool_call_id or ctx.tool_call_id or ""),
            "tool_name": str(tool_name or ""),
            "subtask_id": str(subtask_id or ctx.subtask_id or ""),
            "refs": {str(k): str(v) for k, v in (refs or {}).items() if str(v or "").strip()},
            "summary": str(summary or ""),
        }
        if data:
            event["data"] = self.redact(data)
        try:
            line = json.dumps(event, ensure_ascii=False, default=_json_default)
            with self._lock:
                self._ensure_dir()
                with self.events_path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            logger.debug("Failed to append debug trace event: %s", exc)

    def start_llm(
        self,
        context: DebugTraceContext | None = None,
        *,
        turn: int = 0,
        purpose: str = "main",
        provider: str = "",
        model: str = "",
        response_format: str = "",
        include_stream: bool = True,
    ) -> DebugTraceContext:
        ctx = context or self.root_context()
        clean_purpose = _purpose_label(purpose or ctx.default_purpose or "main")
        with self._lock:
            self._llm_count += 1
            llm_index = self._llm_count
        node_id = f"llm{llm_index:02d}"
        turn_number = max(0, int(turn or ctx.turn or 0))
        parent_id = ctx.node_id or ctx.parent_id or self.turn_node_id(turn_number, scope_id=ctx.scope_id)
        epoch_ms = int(time.time() * 1000)
        prefix = (
            f"{epoch_ms}__req-{self.request_id}"
            f"__t{turn_number:02d}__{node_id}-{clean_purpose}"
        )
        refs: dict[str, str] = {}
        if self.capture_payloads:
            refs["request"] = f"{prefix}__request.json"
            refs["response"] = f"{prefix}__response.json"
        if self.capture_stream and include_stream:
            refs["stream"] = f"{prefix}__stream.jsonl"
        llm_ctx = replace(
            ctx,
            turn=turn_number,
            node_id=node_id,
            parent_id=parent_id,
            default_purpose=clean_purpose,
            refs=refs,
        )
        self.record_event(
            llm_ctx,
            kind="llm",
            phase="start",
            name=clean_purpose,
            status="running",
            refs=refs,
            data={
                "provider": provider,
                "model": model,
                "response_format": response_format,
            },
        )
        return llm_ctx

    def finish_llm(
        self,
        context: DebugTraceContext,
        *,
        status: str = "completed",
        duration_ms: int = 0,
        refs: dict[str, str] | None = None,
        summary: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.record_event(
            context,
            kind="llm",
            phase="end" if status != "error" else "error",
            name=context.default_purpose,
            status=status,
            duration_ms=duration_ms,
            refs=refs,
            summary=summary,
            data=data,
        )

    def write_json(self, relative_name: str, payload: Any, *, force: bool = False) -> str:
        if not (self.capture_payloads or force):
            return ""
        rel = str(relative_name or "").strip()
        if not rel:
            return ""
        target = (self.debug_dir / rel).resolve()
        try:
            target.relative_to(self.debug_dir.resolve())
        except ValueError:
            logger.debug("Refusing to write debug payload outside debug dir: %s", target)
            return ""
        try:
            self._ensure_dir()
            target.write_text(
                json.dumps(self.redact(payload), ensure_ascii=False, indent=2, default=_json_default),
                encoding="utf-8",
            )
            return rel
        except Exception as exc:
            logger.debug("Failed to write debug payload %s: %s", target, exc)
            return ""

    def open_stream_file(self, relative_name: str) -> TextIO | None:
        if not self.capture_stream:
            return None
        rel = str(relative_name or "").strip()
        if not rel:
            return None
        target = (self.debug_dir / rel).resolve()
        try:
            target.relative_to(self.debug_dir.resolve())
        except ValueError:
            logger.debug("Refusing to open debug stream outside debug dir: %s", target)
            return None
        try:
            self._ensure_dir()
            return target.open("a", encoding="utf-8", newline="\n")
        except Exception as exc:
            logger.debug("Failed to open debug stream %s: %s", target, exc)
            return None

    def redact(self, value: Any, *, _depth: int = 0) -> Any:
        if _depth > 12:
            return "<max-depth>"
        if isinstance(value, dict):
            clean: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                normalized_key = key_text.strip().lower().replace("-", "_")
                generic_secret = normalized_key in _GENERIC_TOKEN_KEYS and not isinstance(item, (int, float))
                if _SENSITIVE_KEY_RE.search(key_text) or generic_secret:
                    clean[key_text] = "<redacted>"
                else:
                    clean[key_text] = self.redact(item, _depth=_depth + 1)
            return clean
        if isinstance(value, (list, tuple, set)):
            return [self.redact(item, _depth=_depth + 1) for item in value]
        if isinstance(value, bytes):
            return {"bytes": len(value), "redacted": True}
        if isinstance(value, str):
            text = _BEARER_RE.sub("Bearer <redacted>", value)
            text = _OPENAI_KEY_RE.sub("<redacted-api-key>", text)
            text = _KEY_VALUE_SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
            return _truncate_text(text, self.max_string_chars)
        return value


def ensure_debug_trace(value: Any) -> DebugTraceContext | None:
    if isinstance(value, DebugTraceContext):
        return value if value.enabled else None
    if isinstance(value, DebugTraceSink):
        return value.root_context()
    return None


def create_run_debug_trace(
    *,
    conversation: Conversation,
    request_id: str,
    model_name: str = "",
    mode: str = "",
    source: str = "runtime",
    capture_payloads: bool = False,
    capture_stream: bool = False,
) -> DebugTraceContext | None:
    """Create and start the shared trace for one top-level Agent run."""

    conversation_id = str(getattr(conversation, "id", "") or "").strip()
    if not conversation_id:
        return None
    try:
        sink = DebugTraceSink(
            conversation_id=conversation_id,
            request_id=str(request_id or "").strip(),
            work_dir=str(getattr(conversation, "work_dir", "") or "."),
            capture_payloads=capture_payloads,
            capture_stream=capture_stream,
        )
        trace = sink.root_context(parent_id="run")
        latest_user = ""
        for message in reversed(getattr(conversation, "messages", []) or []):
            if str(getattr(message, "role", "") or "") != "user":
                continue
            latest_user = str(getattr(message, "content", "") or "").strip()
            if latest_user:
                break
        sink.record_event(
            trace,
            kind="run",
            phase="start",
            turn=0,
            node_id="run",
            parent_id="",
            name="request",
            status="running",
            summary=latest_user[:240],
            data={
                "model": str(model_name or ""),
                "mode": str(mode or getattr(conversation, "mode", "") or "chat"),
                "source": str(source or "runtime"),
                "work_dir": str(getattr(conversation, "work_dir", "") or "."),
                "capture_payloads": bool(capture_payloads),
                "capture_stream": bool(capture_stream),
            },
        )
        return trace
    except Exception as exc:
        logger.debug("Failed to create run debug trace: %s", exc)
        return None


def finish_run_debug_trace(
    debug_trace: DebugTraceContext | None,
    *,
    status: str,
    summary: str = "",
) -> None:
    """Write the terminal event for a top-level Agent run."""

    if debug_trace is None:
        return
    normalized = str(status or "completed").strip().lower() or "completed"
    debug_trace.record_event(
        kind="run",
        phase="error" if normalized in {"error", "failed"} else "end",
        turn=0,
        node_id="run",
        parent_id="",
        name="request",
        status=normalized,
        summary=str(summary or "")[:220],
    )
