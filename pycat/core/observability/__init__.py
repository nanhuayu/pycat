"""Cross-cutting debug trace observability.

Owns the per-request debug trace context, sinks, redaction and on-disk layout.
This package is infrastructure: it must not import agent/llm/channel/gui code.
"""
