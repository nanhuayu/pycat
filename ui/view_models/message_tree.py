from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Set

from models.conversation import Message, normalize_tool_calls, normalize_tool_result

VISIBLE_ROLES = {"system", "user", "assistant"}


@dataclass(frozen=True)
class ToolInvocationView:
    id: str
    name: str
    kind: str
    tool_call: Dict[str, Any]
    result: Dict[str, Any] | None
    is_resolved: bool
    is_error: bool
    parent_message_id: str
    root_tool_call_id: str


@dataclass(frozen=True)
class MessageView:
    message: Message
    tool_invocations: List[ToolInvocationView] = field(default_factory=list)


@dataclass(frozen=True)
class MessageTreeLookups:
    messages_by_id: Dict[str, Message] = field(default_factory=dict)
    tool_calls_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    results_by_tool_call_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    parent_message_by_tool_call_id: Dict[str, str] = field(default_factory=dict)
    root_tool_call_by_id: Dict[str, str] = field(default_factory=dict)
    resolved_tool_call_ids: Set[str] = field(default_factory=set)
    errored_tool_call_ids: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class MessageTreeViewModel:
    messages: List[MessageView]
    lookups: MessageTreeLookups


def tool_call_name(tool_call: Dict[str, Any] | None) -> str:
    func = (tool_call or {}).get("function") if isinstance((tool_call or {}).get("function"), dict) else {}
    return str(func.get("name") or "unknown_tool").strip() or "unknown_tool"


def tool_call_kind(name: str) -> str:
    if str(name or "").startswith("subagent__"):
        return "subagent"
    if str(name or "").startswith("capability__"):
        return "capability"
    return "tool"


def normalize_messages(messages: Iterable[Message | Dict[str, Any]]) -> List[Message]:
    """Return visible, normalized RunTree messages.

    role='tool' is intentionally excluded: tool results are represented by
    assistant.tool_calls[].result after Conversation/model normalization.
    """
    normalized: List[Message] = []
    for item in messages or []:
        message = item if isinstance(item, Message) else Message.from_dict(item) if isinstance(item, dict) else None
        if message is None:
            continue
        if str(message.role or "") not in VISIBLE_ROLES:
            continue
        clone = Message.from_dict(message.to_dict())
        clone.tool_calls = normalize_tool_calls(clone.tool_calls)
        normalized.append(clone)
    return normalized


def _route_from_result(result: Dict[str, Any] | None, tool_call_id: str, parent_message_id: str) -> tuple[str, str]:
    payload = result or {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    root_tool_call_id = str(
        payload.get("root_tool_call_id")
        or run.get("root_tool_call_id")
        or metadata.get("root_tool_call_id")
        or tool_call_id
        or ""
    )
    parent_id = str(
        payload.get("parent_message_id")
        or run.get("parent_message_id")
        or metadata.get("parent_message_id")
        or parent_message_id
        or ""
    )
    return parent_id, root_tool_call_id


def build_message_lookups(messages: Iterable[Message | Dict[str, Any]]) -> MessageTreeLookups:
    normalized = normalize_messages(messages)
    messages_by_id: Dict[str, Message] = {}
    tool_calls_by_id: Dict[str, Dict[str, Any]] = {}
    results_by_tool_call_id: Dict[str, Dict[str, Any]] = {}
    parent_message_by_tool_call_id: Dict[str, str] = {}
    root_tool_call_by_id: Dict[str, str] = {}
    resolved_tool_call_ids: Set[str] = set()
    errored_tool_call_ids: Set[str] = set()

    def visit(items: Iterable[Message], current_root: str = "") -> None:
        for message in items:
            message_id = str(getattr(message, "id", "") or "")
            if message_id:
                messages_by_id[message_id] = message
            for tool_call in message.tool_calls or []:
                if not isinstance(tool_call, dict):
                    continue
                tool_call_id = str(tool_call.get("id") or "").strip()
                if not tool_call_id or tool_call_id in tool_calls_by_id:
                    continue
                result = normalize_tool_result(tool_call.get("result")) if "result" in tool_call else None
                parent_id, result_root = _route_from_result(result, tool_call_id, message_id)
                root_id = current_root or result_root or tool_call_id
                tool_calls_by_id[tool_call_id] = tool_call
                parent_message_by_tool_call_id[tool_call_id] = parent_id or message_id
                root_tool_call_by_id[tool_call_id] = root_id
                if result is not None:
                    results_by_tool_call_id[tool_call_id] = result
                    resolved_tool_call_ids.add(tool_call_id)
                    if result.get("metadata", {}).get("is_error") or result.get("is_error") or result.get("error"):
                        errored_tool_call_ids.add(tool_call_id)
                    run = result.get("run") if isinstance(result.get("run"), dict) else None
                    if run:
                        child_messages = normalize_messages(run.get("messages") or [])
                        visit(child_messages, current_root=root_id)

    visit(normalized)
    return MessageTreeLookups(
        messages_by_id=messages_by_id,
        tool_calls_by_id=tool_calls_by_id,
        results_by_tool_call_id=results_by_tool_call_id,
        parent_message_by_tool_call_id=parent_message_by_tool_call_id,
        root_tool_call_by_id=root_tool_call_by_id,
        resolved_tool_call_ids=resolved_tool_call_ids,
        errored_tool_call_ids=errored_tool_call_ids,
    )


def build_message_tree_view_model(messages: Iterable[Message | Dict[str, Any]]) -> MessageTreeViewModel:
    normalized = normalize_messages(messages)
    lookups = build_message_lookups(normalized)
    views: List[MessageView] = []
    processed_tool_call_ids: Set[str] = set()
    for message in normalized:
        invocations: List[ToolInvocationView] = []
        parent_message_id = str(getattr(message, "id", "") or "")
        for tool_call in message.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = str(tool_call.get("id") or "").strip()
            if not tool_call_id or tool_call_id in processed_tool_call_ids:
                continue
            processed_tool_call_ids.add(tool_call_id)
            name = tool_call_name(tool_call)
            result = lookups.results_by_tool_call_id.get(tool_call_id)
            invocations.append(
                ToolInvocationView(
                    id=tool_call_id,
                    name=name,
                    kind=tool_call_kind(name),
                    tool_call=tool_call,
                    result=result,
                    is_resolved=tool_call_id in lookups.resolved_tool_call_ids,
                    is_error=tool_call_id in lookups.errored_tool_call_ids,
                    parent_message_id=lookups.parent_message_by_tool_call_id.get(tool_call_id, parent_message_id),
                    root_tool_call_id=lookups.root_tool_call_by_id.get(tool_call_id, tool_call_id),
                )
            )
        views.append(MessageView(message=message, tool_invocations=invocations))
    return MessageTreeViewModel(messages=views, lookups=lookups)


def view_model_for_message(message: Message | Dict[str, Any]) -> MessageView | None:
    model = build_message_tree_view_model([message])
    return model.messages[0] if model.messages else None


def resolve_root_tool_call_id(tool_call_id: str, lookups: MessageTreeLookups) -> str:
    return lookups.root_tool_call_by_id.get(str(tool_call_id or ""), str(tool_call_id or ""))
