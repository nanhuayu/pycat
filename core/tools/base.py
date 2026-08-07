from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List, Union, Callable

from models.contracts.tooling import RiskLevel, ToolDescriptor, normalize_risk_level, normalize_tool_category


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


@dataclass(frozen=True)
class ToolApprovalRequest:
    """Typed approval request emitted by the tool execution boundary."""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]
    category: str
    risk: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "arguments": dict(self.arguments or {}),
            "category": self.category,
            "risk": self.risk,
            "message": self.message,
        }


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Runtime identity and boundary data for one tool call."""

    source: str = "desktop"
    mode: str = "chat"
    agent_id: str = ""
    trace_id: str = ""
    tool_call_id: str = ""
    workspace_roots: tuple[str, ...] = ()
    permission: PermissionContext = field(default_factory=PermissionContext)
    capability_executor: Any = None
    compression_factory: Any = None
    compression_tasks: Any = None
    shell_config: Any = None
    process_manager: Any = None
    run_policy: Any = None
    debug_trace: Any = None


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
                 runtime: ToolRuntimeContext | None = None,
                 permission: PermissionContext | None = None):
        self.work_dir = work_dir
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
        self.runtime = runtime or ToolRuntimeContext(
            workspace_roots=(str(work_dir or "."),),
            permission=permission or PermissionContext(workspace_roots=(str(work_dir or "."),)),
        )
        self.permission = permission or self.runtime.permission

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

        def _default_answer(payload: Dict[str, Any]) -> Dict[str, Any]:
            options = payload.get("options") or []
            labels = _option_labels(payload)
            recommended = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                if option.get("recommended"):
                    label = str(option.get("label") or "").strip()
                    if label:
                        recommended.append(label)

            multi_select = bool(payload.get("multiple", False))
            selected = recommended[:] if multi_select else recommended[:1]
            if not selected and labels:
                selected = labels[:] if multi_select else labels[:1]

            return {
                "selected": selected,
                "freeText": None,
                "skipped": not bool(selected),
            }

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
        return _default_answer(payload)

    def resolve_path(self, path: str) -> Any: # Returns Path object
        from pathlib import Path
        import os
        
        p = (path or ".").strip() or "."
        roots = tuple(str(item or "").strip() for item in getattr(self.runtime, "workspace_roots", ()) if str(item or "").strip())
        root = Path(roots[0] if roots else self.work_dir).resolve()
        candidate = (root / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
        
        allowed = False
        for raw_root in roots or (str(root),):
            try:
                candidate.relative_to(Path(raw_root).resolve())
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise ValueError(f"Access denied: Path '{path}' is outside workspace roots {roots or (str(root),)}")
            
        return candidate

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
