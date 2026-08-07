"""Agent run events and nested-run trace helpers.

Debug trace sinks live in ``core.observability`` (infrastructure); this package
keeps only agent-domain event emission.
"""

from .emitter import EventEmitter

__all__ = ["EventEmitter"]
