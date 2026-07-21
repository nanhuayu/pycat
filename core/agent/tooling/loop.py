"""Tool-call coordination for one assistant turn."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.agent.run.control_messages import REPETITION_WARNING
from core.agent.events.emitter import EventEmitter
from core.agent.tooling.repetition import ToolRepetitionDetector
from models.contracts.agent import RunPolicy, RunEventKind, TurnState, TurnContext, TurnOutcome, TurnOutcomeKind
from core.tools.base import ToolResult
from core.agent.events.debug_trace import DebugTraceContext

logger = logging.getLogger(__name__)


@dataclass
class ToolCallCoordinator:
    """Executes assistant tool calls and converts results back into turn outcomes."""

    client: Any
    tool_executor: Any
    subtask_coordinator: Any
    tool_result_to_string: Callable[[ToolResult | str], str]
    summarize_tool_result: Callable[[str, str], str]
    build_tool_result_block: Callable[..., Awaitable[dict[str, Any]]]

    async def execute(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        turn_context: TurnContext,
        assistant_msg: Message,
        repetition_detector: ToolRepetitionDetector,
        emitter: EventEmitter,
        approval_callback,
        questions_callback,
        cancel_event,
        on_event,
        turns_limit: int,
        debug_trace: DebugTraceContext | None = None,
    ) -> TurnOutcome:
        turn_context.had_tool_work = True
        turn_context.incomplete_responses = 0
        total_tools = len(assistant_msg.tool_calls or [])
        for tool_call in assistant_msg.tool_calls or []:
            if cancel_event and cancel_event.is_set():
                turn_context.state = TurnState.CANCELLED
                return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context, final_message=assistant_msg)

            tool_name, args, tool_call_id = self.tool_executor.parse_tool_call(tool_call)
            trace_args, trace_arg_chars = self._trace_arguments(args)
            tool_trace: DebugTraceContext | None = None
            tool_started_at = time.time()
            tool_event_base = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id or "",
                "phase": "start",
                "allowed": None,
                "is_error": False,
                "total_tools": total_tools,
            }
            if repetition_detector.record(tool_name, args if isinstance(args, dict) else {}):
                logger.warning("Tool repetition detected: %s", tool_name)
                if debug_trace is not None:
                    node_id = debug_trace.sink.tool_node_id(tool_call_id=tool_call_id or "", tool_name=tool_name)
                    tool_trace = debug_trace.child(
                        node_id=node_id,
                        parent_id=debug_trace.node_id or debug_trace.parent_id,
                        tool_call_id=tool_call_id or "",
                        default_purpose="subtask",
                    )
                    tool_trace.record_event(
                        kind="tool",
                        phase="repetition",
                        turn=turn_context.turn,
                        node_id=node_id,
                        parent_id=tool_trace.parent_id,
                        name=tool_name,
                        status="error",
                        duration_ms=int((time.time() - tool_started_at) * 1000),
                        tool_call_id=tool_call_id or "",
                        tool_name=tool_name,
                        summary="Repeated identical arguments.",
                        data={"args": trace_args, "argument_chars": trace_arg_chars},
                    )
                emitter.emit(
                    RunEventKind.TOOL_END,
                    turn=turn_context.turn,
                    detail=f"Tool {tool_name} stopped: repeated identical arguments.",
                    data={**tool_event_base, "phase": "repetition", "is_error": True},
                )
                turn_context.runtime_messages = [Message(role="user", content=REPETITION_WARNING)]
                repetition_detector.reset()
                turn_context.state = TurnState.TURN_COMPLETE
                return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

            allowed = self.tool_executor.is_tool_allowed(tool_name, policy)
            if debug_trace is not None:
                node_id = debug_trace.sink.tool_node_id(tool_call_id=tool_call_id or "", tool_name=tool_name)
                tool_trace = debug_trace.child(
                    node_id=node_id,
                    parent_id=debug_trace.node_id or debug_trace.parent_id,
                    tool_call_id=tool_call_id or "",
                    default_purpose="subtask",
                )
                refs = debug_trace.sink.tool_payload_refs(
                    tool_trace,
                    turn=turn_context.turn,
                    tool_name=tool_name,
                )
                tool_trace = tool_trace.with_refs(refs)
                request_ref = refs.get("request", "")
                if request_ref:
                    debug_trace.sink.write_json(
                        request_ref,
                        {
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id or "",
                            "allowed": bool(allowed),
                            "arguments": args if isinstance(args, dict) else {},
                        },
                    )
                tool_trace.record_event(
                    kind="tool",
                    phase="start",
                    turn=turn_context.turn,
                    node_id=node_id,
                    parent_id=tool_trace.parent_id,
                    name=tool_name,
                    status="running",
                    tool_call_id=tool_call_id or "",
                    tool_name=tool_name,
                    refs=refs,
                    data={
                        "allowed": bool(allowed),
                        "args": trace_args,
                        "argument_chars": trace_arg_chars,
                        "total_tools": total_tools,
                    },
                )
            emitter.emit(
                RunEventKind.TOOL_START,
                turn=turn_context.turn,
                detail=f"Tool {tool_name} started.",
                data={**tool_event_base, "allowed": bool(allowed)},
            )
            context = self.tool_executor.build_tool_context(
                conversation=conversation,
                provider=provider,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                llm_client=self.client,
                policy=policy,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                debug_trace=tool_trace,
            )
            tool_result = await self.tool_executor.execute_tool(
                tool_name=tool_name,
                tool_args=args,
                allowed=allowed,
                policy=policy,
                context=context,
            )
            raw_tool_result = tool_result
            result_text = self.tool_result_to_string(tool_result)
            tool_is_error = bool(getattr(tool_result, "is_error", False))
            control_action = getattr(tool_result, "control_action", None)

            subtask_execution = await self.subtask_coordinator.handle_control_action(
                action=control_action,
                assistant_msg=assistant_msg,
                tool_call_id=tool_call_id,
                conversation=conversation,
                provider=provider,
                policy=policy,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
                on_event=on_event,
                turn=turn_context.turn,
                debug_trace=tool_trace,
            )
            if subtask_execution is not None:
                tool_result = subtask_execution.result
                result_text = self.tool_result_to_string(tool_result)
                tool_is_error = bool(getattr(tool_result, "is_error", False))
                try:
                    self.tool_executor.refresh_context_state(conversation, context)
                except AttributeError:
                    logger.debug("Tool executor does not expose refresh_context_state")

            self.tool_executor.sync_state(conversation, context)

            def report_archive_prepare() -> None:
                emitter.emit(
                    RunEventKind.TOOL_START,
                    turn=turn_context.turn,
                    data={
                        **tool_event_base,
                        "phase": "organizing",
                        "allowed": bool(allowed),
                    },
                )

            result_block = await self.build_tool_result_block(
                conversation=conversation,
                tool_name=tool_name,
                tool_category=str(getattr(getattr(context, "permission", None), "category", "") or ""),
                tool_call_id=tool_call_id,
                result=tool_result,
                tool_args=args if isinstance(args, dict) else {},
                parent_message_id=str(getattr(assistant_msg, "id", "") or ""),
                provider=provider,
                debug_trace=tool_trace,
                on_archive_prepare=report_archive_prepare,
            )
            self._write_tool_response(
                tool_trace,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                allowed=allowed,
                raw_result=raw_tool_result,
                final_is_error=tool_is_error,
                result_block=result_block,
            )
            self._record_tool_end(
                tool_trace,
                turn=turn_context.turn,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                allowed=allowed,
                is_error=tool_is_error,
                started_at=tool_started_at,
                result_block=result_block,
                summary=str(result_block["result"].get("summary") or ""),
            )
            emitter.emit(
                RunEventKind.TOOL_END,
                turn=turn_context.turn,
                detail=f"Tool {tool_name} {'failed' if tool_is_error else 'completed'}.",
                data={
                    **tool_event_base,
                    "phase": "end",
                    "allowed": bool(allowed),
                    "is_error": tool_is_error,
                    "summary": str(result_block["result"].get("summary") or ""),
                },
            )

            emitter.emit(RunEventKind.STEP, turn=turn_context.turn, data=result_block["event"])
            conversation.attach_tool_result(
                tool_call_id,
                result_block["result"],
                summary=str(result_block["result"].get("summary") or ""),
                metadata=dict(result_block["result"].get("metadata") or {}),
                images=list(result_block.get("images") or []),
                state_snapshot=result_block.get("state_snapshot") if isinstance(result_block.get("state_snapshot"), dict) else None,
            )
            if control_action is not None and control_action.kind == "complete" and not tool_is_error:
                completion_result = str(control_action.completion_result or "").strip()
                assistant_msg.content = completion_result
                assistant_msg.summary = completion_result[:240]
                assistant_msg.metadata["completion"] = True
                assistant_msg.metadata["completion_policy"] = "explicit"
                self.tool_executor.attach_state_snapshot(conversation, assistant_msg)
                turn_context.state = TurnState.TURN_COMPLETE
                turn_context.runtime_messages = []
                return TurnOutcome(
                    kind=TurnOutcomeKind.COMPLETE,
                    context=turn_context,
                    final_message=assistant_msg,
                )

        turn_context.state = TurnState.TURN_COMPLETE
        turn_context.runtime_messages = []
        return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

    @staticmethod
    def _trace_arguments(arguments: Any, *, limit: int = 2_000) -> tuple[dict[str, Any], int]:
        payload = arguments if isinstance(arguments, dict) else {}
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            encoded = str(payload)
        if len(encoded) <= limit:
            return payload, len(encoded)
        return {
            "truncated": True,
            "keys": sorted(str(key) for key in payload),
            "preview": encoded[: min(500, limit)],
        }, len(encoded)

    @staticmethod
    def _write_tool_response(
        debug_trace: DebugTraceContext | None,
        *,
        tool_name: str,
        tool_call_id: str | None,
        allowed: bool,
        raw_result: ToolResult | str,
        final_is_error: bool | None = None,
        result_block: dict[str, Any],
    ) -> None:
        if debug_trace is None or not debug_trace.refs:
            return
        response_ref = str(debug_trace.refs.get("response") or "")
        if not response_ref:
            return
        if isinstance(raw_result, ToolResult):
            control_action = getattr(raw_result, "control_action", None)
            raw_payload: Any = {
                "content": raw_result.content,
                "is_error": bool(raw_result.is_error),
                "control_action": control_action.to_dict() if control_action is not None else None,
            }
        else:
            raw_payload = {"content": str(raw_result or ""), "is_error": False, "control_action": None}
        model_result = result_block.get("result") if isinstance(result_block, dict) else {}
        metadata = model_result.get("metadata") if isinstance(model_result, dict) else {}
        metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        model_visible: dict[str, Any] = {
            "content": model_result.get("content") if isinstance(model_result, dict) else "",
        }
        images = result_block.get("images") if isinstance(result_block, dict) else None
        if isinstance(images, list) and images:
            model_visible["images"] = images
        debug_trace.sink.write_json(
            response_ref,
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id or "",
                "allowed": bool(allowed),
                "is_error": bool(raw_payload.get("is_error")) if final_is_error is None else bool(final_is_error),
                "raw_result": raw_payload,
                "model_result": model_visible,
                "archive": {
                    "content_id": str(metadata.get("content_id") or ""),
                    "archive_ref": str(metadata.get("archive_ref") or metadata.get("original_ref") or ""),
                    "chars": int(metadata.get("tool_result_chars") or metadata.get("archive_size") or 0),
                },
            },
        )

    @staticmethod
    def _record_tool_end(
        debug_trace: DebugTraceContext | None,
        *,
        turn: int,
        tool_name: str,
        tool_call_id: str | None,
        allowed: bool,
        is_error: bool,
        started_at: float,
        result_block: dict[str, Any],
        summary: str = "",
    ) -> None:
        if debug_trace is None:
            return
        result = result_block.get("result") if isinstance(result_block, dict) else {}
        metadata = result.get("metadata") if isinstance(result, dict) else {}
        metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        refs = dict(debug_trace.refs or {})
        refs.update({
            key: str(metadata.get(key) or "")
            for key in ("content_id", "archive_ref", "original_ref")
            if str(metadata.get(key) or "").strip()
        })
        debug_trace.record_event(
            kind="tool",
            phase="end",
            turn=int(turn or debug_trace.turn or 0),
            node_id=debug_trace.node_id,
            parent_id=debug_trace.parent_id,
            name=tool_name,
            status="error" if is_error else "completed",
            duration_ms=int((time.time() - started_at) * 1000),
            tool_call_id=tool_call_id or "",
            tool_name=tool_name,
            refs=refs,
            summary=summary or str(result.get("summary") if isinstance(result, dict) else "")[:220],
            data={
                "allowed": bool(allowed),
                "is_error": bool(is_error),
                "content_id": str(metadata.get("content_id") or ""),
                "archive_ref": str(metadata.get("archive_ref") or metadata.get("original_ref") or ""),
                "archive_size": int(metadata.get("tool_result_chars") or metadata.get("archive_size") or 0),
                "subtask_id": str(metadata.get("agent_run_id") or metadata.get("child_session_id") or ""),
            },
        )
