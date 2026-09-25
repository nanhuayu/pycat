"""PyCat's embedded Python application API. GUI imports are intentionally separate."""

from pycat.core.app.services.run import RunHandle
from pycat.core.content.export import export_document
from pycat.core.tools.base import ApprovalDecision, ToolApprovalRequest
from pycat.models.contracts.agent import (
    ApplicationError,
    ConversationBusyError,
    InvalidRequestError,
    PersistenceError,
    RunEvent,
    RunEventKind,
    RunRequest,
    RunResult,
    RunStatus,
    RunStopReason,
    SlowConsumerError,
    ToolCallResult,
)
from pycat.models.contracts.content import ContentRef
from pycat.models.model_profile import ModelProfile
from pycat.models.provider import Provider

from .application import PyCat, run_sync

__all__ = [
    "PyCat",
    "run_sync",
    "export_document",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RunStopReason",
    "RunEvent",
    "RunEventKind",
    "Provider",
    "ModelProfile",
    "ApplicationError",
    "ConversationBusyError",
    "InvalidRequestError",
    "PersistenceError",
    "SlowConsumerError",
    "RunHandle",
    "ToolCallResult",
    "ApprovalDecision",
    "ToolApprovalRequest",
    "ContentRef",
]
