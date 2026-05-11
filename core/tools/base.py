from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, Union, Callable

from core.tools.catalog import ToolDescriptor, normalize_tool_category

class ToolResult:
    """Standardized result from a tool execution."""
    def __init__(self, content: Union[str, List[Dict[str, Any]]], is_error: bool = False):
        self.content = content
        self.is_error = is_error

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
                 approval_callback: Optional[Callable[[str], bool]] = None, 
                 questions_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 state: Optional[Dict[str, Any]] = None,
                 llm_client: Any = None,
                 conversation: Any = None,
                 provider: Any = None):
        self.work_dir = work_dir
        self.approval_callback = approval_callback
        self.questions_callback = questions_callback
        self.state = state if state is not None else {}
        self.llm_client = llm_client
        self.conversation = conversation
        self.provider = provider

    async def ask_approval(self, message: str) -> bool:
        if self.approval_callback:
            import inspect
            if inspect.iscoroutinefunction(self.approval_callback):
                return await self.approval_callback(message)
            result = self.approval_callback(message)
            if inspect.isawaitable(result):
                return await result
            return result
        return True # Default to auto-approve if no callback provided (for now, or False for security)

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

            multi_select = bool(payload.get("multi_select", False))
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
        root = Path(self.work_dir).resolve()
        candidate = (root / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
        
        # Security check: ensure path is within workspace
        # For now, strict check
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError(f"Access denied: Path '{path}' is outside workspace '{self.work_dir}'")
            
        return candidate

class BaseTool(ABC):
    """Abstract base class for all tools (System & MCP).

    Each tool declares:
        - ``category``: tool selection and permission category, one of ``read``, ``search``,
            ``edit``, ``execute``, ``manage``, ``delegate``, ``extension``, or ``mcp``.
    """

    # Max output chars before truncation (0 = no limit)
    max_output_chars: int = 60_000

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
        return "extension"

    @property
    def source(self) -> str:
        """Catalog source: builtin, search, capability, mcp, etc."""
        return "builtin"

    @property
    def display_name(self) -> str:
        return self.name

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

    def truncate_output(self, text: str) -> str:
        """Truncate tool output if it exceeds max_output_chars."""
        if self.max_output_chars <= 0 or len(text) <= self.max_output_chars:
            return text
        half = self.max_output_chars // 2
        return (
            text[:half]
            + f"\n\n... [truncated {len(text) - self.max_output_chars} chars] ...\n\n"
            + text[-half:]
        )

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
