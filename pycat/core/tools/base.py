from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
import os
import hashlib
import asyncio
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Union

from pycat.models.contracts.tooling import (
    FilesystemScope,
    RiskLevel,
    ToolDescriptor,
    normalize_risk_level,
    normalize_tool_category,
)
from pycat.models.session_paths import normalize_work_dir
from pycat.models.workspace import WorkspaceLocation


@dataclass(frozen=True)
class PermissionContext:
    """Permission facts for one tool execution."""

    source: str = "desktop"
    mode: str = "chat"
    tool_name: str = ""
    category: str = "capability"
    risk: str = "low"
    agent_id: str = ""
    trace_id: str = ""
    workspace_roots: tuple[str, ...] = ()
    read_roots: tuple[str, ...] = ()
    filesystem_mode: str = "confined"


@dataclass(frozen=True)
class ToolApprovalRequest:
    """Typed approval request emitted by the tool execution boundary."""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]
    category: str
    risk: str
    message: str
    requires_tool_approval: bool = True
    external_path: str = ""
    read_grant_root: str = ""

    @property
    def requires_path_approval(self) -> bool:
        return bool(self.external_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "arguments": dict(self.arguments or {}),
            "category": self.category,
            "risk": self.risk,
            "message": self.message,
            "requires_tool_approval": bool(self.requires_tool_approval),
            "external_path": self.external_path,
            "read_grant_root": self.read_grant_root,
        }


ReadApprovalScope = Literal["", "call", "run"]


@dataclass(frozen=True)
class ApprovalDecision:
    """One approval response; path roots remain owned by the executor."""

    approved: bool = False
    read_scope: ReadApprovalScope = ""

    def __post_init__(self) -> None:
        scope = str(self.read_scope or "").strip().lower()
        if scope not in {"", "call", "run"}:
            scope = ""
        object.__setattr__(self, "approved", bool(self.approved))
        object.__setattr__(self, "read_scope", scope)


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Runtime identity and boundary data for one tool call."""

    source: str = "desktop"
    mode: str = "chat"
    agent_id: str = ""
    trace_id: str = ""
    tool_call_id: str = ""
    workspace_roots: tuple[str, ...] = ()
    read_roots: tuple[str, ...] = ()
    filesystem_scope: FilesystemScope = field(default_factory=FilesystemScope)
    permission: PermissionContext = field(default_factory=PermissionContext)
    capability_executor: Any = None
    compression_factory: Any = None
    compression_tasks: Any = None
    shell_config: Any = None
    process_manager: Any = None
    terminal_controller: str = "agent"
    run_policy: Any = None
    debug_trace: Any = None
    cancel_event: Any = None
    run_control: Any = None


@dataclass(frozen=True)
class ScheduledSubtaskAction:
    """Typed request to run a child agent/capability."""

    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload or {})


@dataclass(frozen=True)
class ToolControlAction:
    """A typed non-text action requested by a tool."""

    kind: str
    subtask: ScheduledSubtaskAction | None = None
    completion_result: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def schedule_subtask(cls, payload: dict[str, Any]) -> "ToolControlAction":
        return cls(kind="schedule_subtask", subtask=ScheduledSubtaskAction(dict(payload or {})))

    @classmethod
    def complete(cls, result: str) -> "ToolControlAction":
        return cls(kind="complete", completion_result=str(result or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subtask": self.subtask.to_dict() if self.subtask is not None else None,
            "completion_result": self.completion_result,
            "metadata": dict(self.metadata or {}),
        }


class ToolResult:
    """Standardized result from a tool execution."""
    def __init__(
        self,
        content: Union[str, List[Dict[str, Any]]],
        is_error: bool = False,
        *,
        control_action: ToolControlAction | None = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.content = content
        self.is_error = is_error
        self.control_action = control_action
        self.metadata = dict(metadata or {})

    def to_string(self) -> str:
        if isinstance(self.content, str):
            return self.content
        # Handle list of content blocks (MCP style)
        text_parts = []
        for item in self.content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    text_parts.append(f"[Image: {item.get('mimeType')}]")
        return "\n".join(text_parts)

class ToolContext:
    """Context passed to tool execution."""
    def __init__(self,
                 work_dir: str,
                 approval_callback: Optional[Callable[[ToolApprovalRequest], Any]] = None,
                 questions_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 state: Optional[Dict[str, Any]] = None,
                 llm_client: Any = None,
                 conversation: Any = None,
                 provider: Any = None,
                 content_service: Any = None,
                 workspace_service: Any = None,
                 runtime: ToolRuntimeContext | None = None,
                 permission: PermissionContext | None = None):
        self.work_dir = normalize_work_dir(work_dir)
        self._file_cancelled = threading.Event()
        self.workspace_service = workspace_service
        if WorkspaceLocation.parse(self.work_dir).is_remote and workspace_service is None:
            raise ValueError("SSH workspace service is unavailable")
        self.files = workspace_service.files(self.work_dir) if workspace_service is not None else None
        self.approval_callback = approval_callback
        self.questions_callback = questions_callback
        self.state = state if state is not None else {}
        self.llm_client = llm_client
        self.conversation = conversation
        self.provider = provider
        # The application container owns this service. Standalone callers may
        # leave it unset; tools then reject references that require it rather
        # than constructing a second service owner.
        self.content_service = content_service
        default_roots = self._canonical_roots((self.work_dir,) if self.work_dir else ())
        if runtime is None:
            scope = FilesystemScope(allow_home_read=True)
            read_roots = self._default_read_roots(
                default_roots,
                scope.granted_read_roots,
                allow_home_read=scope.allow_home_read,
            )
            runtime = ToolRuntimeContext(
                workspace_roots=default_roots,
                read_roots=read_roots,
                filesystem_scope=scope,
                permission=permission
                or PermissionContext(
                    workspace_roots=default_roots,
                    read_roots=read_roots,
                    filesystem_mode=scope.mode,
                ),
            )
        else:
            scope = runtime.filesystem_scope
            workspace_roots = self._canonical_roots(runtime.workspace_roots or default_roots)
            read_roots = self._canonical_roots(
                (
                    *self._default_read_roots(
                        workspace_roots,
                        scope.granted_read_roots,
                        allow_home_read=scope.allow_home_read,
                    ),
                    *runtime.read_roots,
                )
            )
            runtime = replace(
                runtime,
                workspace_roots=workspace_roots,
                read_roots=read_roots,
                filesystem_scope=scope,
            )
        base_permission = permission or runtime.permission
        self.permission = replace(
            base_permission,
            workspace_roots=runtime.workspace_roots,
            read_roots=runtime.read_roots,
            filesystem_mode=runtime.filesystem_scope.mode,
        )
        self.runtime = replace(runtime, permission=self.permission)
        self._sync_remote_scope()

    def _sync_remote_scope(self):
        if self.files is not None:
            host_roots = tuple(self.files.connection.info["roots"])
            self.files.read_roots = host_roots if self.runtime.filesystem_scope.is_full_access else self.runtime.read_roots
            self.files.write_roots = host_roots if self.runtime.filesystem_scope.is_full_access else self.runtime.workspace_roots

    def file_is_file(self, path):
        return self.files.stat(path)["file"] if self.files else path.is_file()

    def file_is_dir(self, path):
        return self.files.stat(path)["dir"] if self.files else path.is_dir()

    def read_bytes(self, path):
        return self.files.read_bytes(path) if self.files else path.read_bytes()

    def write_bytes(self, path, data, *, expected=None):
        self.check_file_cancelled()
        if self.files:
            self.files.write_bytes(path, data, expected=expected, check_cancelled=self.check_file_cancelled)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def check_file_cancelled(self):
        if self._file_cancelled.is_set() or (self.runtime.cancel_event and self.runtime.cancel_event.is_set()):
            raise InterruptedError("File operation cancelled before commit")

    async def run_file_io(self, operation, *args):
        """Do not release the tool lifetime while a mutation worker can still run."""
        pending = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            self._file_cancelled.set()
            try:
                await asyncio.shield(pending)
            except Exception:
                pass
            raise

    def file_digest(self, path):
        if self.files:
            return self.files.stat(path, digest=True)["digest"]
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    @property
    def data_dir(self):
        return getattr(self.conversation, "data_dir", None) or getattr(self.content_service, "data_dir", None)

    async def ask_question(self, question: Dict[str, Any]) -> Dict[str, Any]:
        def _option_labels(payload: Dict[str, Any]) -> List[str]:
            labels: List[str] = []
            for option in payload.get("options") or []:
                if isinstance(option, dict):
                    label = str(option.get("label") or "").strip()
                else:
                    label = str(option or "").strip()
                if label:
                    labels.append(label)
            return labels

        def _normalize_answer(payload: Dict[str, Any], answer: Any) -> Dict[str, Any]:
            labels = _option_labels(payload)
            selected: List[str] = []
            free_text = None
            skipped = False

            if isinstance(answer, dict):
                raw_selected = answer.get("selected")
                if isinstance(raw_selected, list):
                    selected = [str(item).strip() for item in raw_selected if str(item).strip()]
                raw_free_text = answer.get("freeText")
                if raw_free_text is not None:
                    free_text = str(raw_free_text).strip() or None
                skipped = bool(answer.get("skipped", False))
            elif isinstance(answer, list):
                selected = [str(item).strip() for item in answer if str(item).strip()]
            elif isinstance(answer, int):
                if 0 <= int(answer) < len(labels):
                    selected = [labels[int(answer)]]
            elif isinstance(answer, str):
                free_text = answer.strip() or None

            selected = [item for item in selected if item]
            if not free_text and labels:
                selected = [item for item in selected if item in labels]

            if not selected and not free_text and not skipped:
                skipped = True

            return {
                "selected": selected,
                "freeText": free_text,
                "skipped": skipped,
                **({"reason": str(answer["reason"])} if isinstance(answer, dict) and answer.get("reason") else {}),
            }

        payload = dict(question or {})
        if self.questions_callback:
            import inspect
            if inspect.iscoroutinefunction(self.questions_callback):
                return _normalize_answer(payload, await self.questions_callback(payload))
            result = self.questions_callback(payload)
            if inspect.isawaitable(result):
                return _normalize_answer(payload, await result)
            return _normalize_answer(payload, result)
        return {"selected": [], "freeText": None, "skipped": True, "reason": "interaction_unavailable"}

    def resolve_read_path(self, path: str) -> Path:
        candidate = self._canonical_candidate(path, allow_home_fallback=True)
        if self.runtime.filesystem_scope.is_full_access:
            return candidate
        roots = self._canonical_roots(self.runtime.read_roots)
        if self._is_allowed(candidate, roots):
            return candidate
        raise ValueError(
            f"Access denied: Path '{path}' is outside readable roots {roots}"
        )

    def resolve_workspace_path(self, path: str) -> Path:
        candidate = self._canonical_candidate(
            path,
            allow_home_fallback=self.runtime.filesystem_scope.is_full_access,
        )
        if self.runtime.filesystem_scope.is_full_access:
            return candidate
        roots = self._canonical_roots(self.runtime.workspace_roots)
        if not roots:
            raise ValueError("an active workspace is required for filesystem paths")
        if self._is_allowed(candidate, roots):
            return candidate
        raise ValueError(
            f"Access denied: Path '{path}' is outside workspace roots {roots}"
        )

    def external_read_request(self, path: str) -> tuple[Path, Path] | None:
        """Return canonical call/run grant targets when a read is outside scope."""

        candidate = self._canonical_candidate(path, allow_home_fallback=True)
        if self.runtime.filesystem_scope.is_full_access:
            return None
        if self._is_allowed(candidate, self._canonical_roots(self.runtime.read_roots)):
            return None
        is_dir = (self.files.connection.call("stat", root=candidate.anchor, path=str(candidate))["dir"]
                  if self.files else candidate.is_dir())
        grant_root = candidate if is_dir else candidate.parent
        return candidate, grant_root

    def add_call_read_root(self, root: str | Path) -> None:
        roots = self._canonical_roots((*self.runtime.read_roots, str(root or "")))
        self.runtime = replace(self.runtime, read_roots=roots)
        self.permission = replace(
            self.permission,
            read_roots=roots,
        )
        self.runtime = replace(self.runtime, permission=self.permission)
        self._sync_remote_scope()

    def apply_filesystem_scope(self, scope: FilesystemScope) -> None:
        """Refresh the current call after a live run access change."""

        roots = self._default_read_roots(
            self.runtime.workspace_roots,
            scope.granted_read_roots,
            allow_home_read=scope.allow_home_read,
        )
        self.permission = replace(
            self.permission,
            read_roots=roots,
            filesystem_mode=scope.mode,
        )
        self.runtime = replace(
            self.runtime,
            read_roots=roots,
            filesystem_scope=scope,
            permission=self.permission,
        )
        self._sync_remote_scope()

    def display_path(self, path: str | Path) -> str:
        if self.files:
            try:
                return str(self.files.absolute(path).relative_to(self.files.location.root))
            except ValueError:
                return str(path)
        candidate = Path(path)
        if self.work_dir:
            try:
                return candidate.relative_to(Path(self.work_dir).expanduser().resolve()).as_posix()
            except ValueError:
                pass
        return str(candidate)

    def _canonical_candidate(self, path: str, *, allow_home_fallback: bool) -> Path:
        if self.files:
            return self.files.canonical(path)
        raw = str(path or ".").strip() or "."
        self._reject_device_namespace(raw)
        expanded = Path(raw).expanduser()
        drive, _tail = os.path.splitdrive(raw)
        if drive and not expanded.is_absolute():
            raise ValueError(f"drive-relative paths are not supported: {path}")
        if expanded.is_absolute():
            candidate = expanded
        else:
            workspace_roots = self._canonical_roots(self.runtime.workspace_roots)
            if workspace_roots:
                base = Path(workspace_roots[0])
            elif allow_home_fallback:
                base = Path.home().resolve()
            else:
                raise ValueError("an active workspace is required for filesystem paths")
            candidate = base / expanded
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"invalid filesystem path: {path}") from exc

    def _canonical_roots(self, roots) -> tuple[str, ...]:
        if self.files:
            return tuple(dict.fromkeys(WorkspaceLocation.parse(str(r)).root for r in roots if r))
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in roots or ():
            value = str(raw or "").strip()
            if not value:
                continue
            self._reject_device_namespace(value)
            try:
                canonical = str(Path(value).expanduser().resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"invalid filesystem root: {value}") from exc
            key = os.path.normcase(canonical)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(canonical)
        return tuple(normalized)

    def _default_read_roots(
        self,
        workspace_roots: tuple[str, ...],
        granted_roots: tuple[str, ...],
        *,
        allow_home_read: bool,
    ) -> tuple[str, ...]:
        roots = list(workspace_roots)
        if allow_home_read:
            roots.append(self.files.connection.info["home"] if self.files else str(Path.home().resolve()))
        roots.extend(granted_roots)
        return self._canonical_roots(roots)

    def _is_allowed(self, candidate: Path, roots: tuple[str, ...]) -> bool:
        if self.files:
            return any(candidate.is_relative_to(root) for root in roots)
        candidate_key = os.path.normcase(str(candidate))
        for raw_root in roots:
            root_key = os.path.normcase(str(Path(raw_root)))
            try:
                if os.path.commonpath((candidate_key, root_key)) == root_key:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _reject_device_namespace(path: str) -> None:
        normalized = str(path or "").replace("/", "\\").casefold()
        if normalized.startswith(("\\\\.\\", "\\\\?\\", "\\device\\", "\\??\\")):
            raise ValueError("Windows device namespace paths are not supported")

class BaseTool(ABC):
    """Abstract base class for all tools (System & MCP).

    Each tool declares:
        - ``category``: one of the canonical tool selection categories.
        - ``risk``: the minimum confirmation risk for an invocation.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    def category(self) -> str:
        """Canonical permission category."""
        return "capability"

    @property
    def risk(self) -> RiskLevel:
        return "low"

    @property
    def source(self) -> str:
        """Catalog source: builtin, search, capability, mcp, etc."""
        return "builtin"

    @property
    def display_name(self) -> str:
        return self.name

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> RiskLevel:
        """Return invocation risk. Tools may raise their static risk from arguments."""
        return normalize_risk_level(self.risk)

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Allow {self.display_name} ({self.name})?"

    def requested_read_path(self, arguments: Dict[str, Any]) -> str:
        """Return the single local path requiring read-scope evaluation."""

        return ""

    def descriptor(self, *, available: bool = True) -> ToolDescriptor:
        return ToolDescriptor.from_tool(
            self,
            source=self.source,
            available=available,
            display_name=self.display_name,
        )

    @property
    @abstractmethod
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema for input parameters."""
        pass

    @abstractmethod
    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool logic."""
        pass

    def to_openai_tool(self) -> Dict[str, Any]:
        """Convert to OpenAI tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema
            }
        }
