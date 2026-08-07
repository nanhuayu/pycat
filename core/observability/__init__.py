"""Cross-cutting debug trace observability.

Owns the per-request debug trace context, sinks, redaction and on-disk layout.
This package is infrastructure: it must not import agent/llm/channel/gui code.
"""

from core.observability.debug_trace import (
    DebugTraceContext,
    DebugTraceSink,
    NullDebugTraceSink,
    create_run_debug_trace,
    ensure_debug_trace,
    finish_run_debug_trace,
    redact_debug_payload,
    resolve_debug_trace_dir,
)

__all__ = [
    "DebugTraceContext",
    "DebugTraceSink",
    "NullDebugTraceSink",
    "create_run_debug_trace",
    "ensure_debug_trace",
    "finish_run_debug_trace",
    "redact_debug_payload",
    "resolve_debug_trace_dir",
]
