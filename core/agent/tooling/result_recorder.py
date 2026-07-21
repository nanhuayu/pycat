"""Tool result recording, archive display, and work trace updates."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from core.agent.events.trace import trace_for_tool_call
from core.state.operations import record_work_step, remember_archive, state_checkpoint
from core.tools.base import ToolResult
from core.tools.tool_call_archive import ToolCallArchiveService, ToolResultViewService
from models.conversation import Conversation, Message

logger = logging.getLogger(__name__)


class ToolResultRecorder:
    """Build model/UI tool result blocks and update session trace state."""

    def __init__(
        self,
        *,
        summarize_tool_result: Callable[[str, str], str],
        client: Any = None,
        archive_compressor_factory: Any = None,
    ) -> None:
        self._summarize_tool_result = summarize_tool_result
        self._client = client
        self._archive_compressor_factory = archive_compressor_factory
        self._tool_call_archive: ToolCallArchiveService | None = None
        self._archive_key: tuple[str, str] | None = None

    async def build_block(
        self,
        *,
        conversation: Conversation,
        tool_name: str,
        tool_category: str = "",
        tool_call_id: Optional[str],
        result: ToolResult | str,
        summary: Optional[str] = None,
        tool_args: Optional[dict[str, Any]] = None,
        parent_message_id: str = "",
        provider: Any = None,
        debug_trace: Any = None,
        on_archive_prepare: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        result_text = self.tool_result_to_string(result)
        tool_images = self.extract_tool_images(result)

        archive_work_dir = str(getattr(conversation, "work_dir", "") or ".")
        archive_session_id = str(getattr(conversation, "id", "") or "default")
        archive_key = (str(archive_work_dir), str(archive_session_id))
        if self._tool_call_archive is None or self._archive_key != archive_key:
            self._tool_call_archive = ToolCallArchiveService(
                archive_work_dir,
                conversation_id=getattr(conversation, "id", None),
            )
            self._archive_key = archive_key

        handle = self._tool_call_archive.process(
            tool_name=tool_name,
            raw_text=result_text,
            tool_category=tool_category,
            tool_call_id=tool_call_id,
            tool_args=tool_args or {},
            seq_id=int(conversation.current_seq_id() or 0),
            images=tool_images,
        )
        if handle.archive is not None:
            compressor = None
            if self._archive_compressor_factory is not None and provider is not None:
                compressor = self._archive_compressor_factory(
                    client=self._client,
                    provider=provider,
                    store=self._tool_call_archive.archive_store,
                    debug_trace=debug_trace,
                )
            view_service = ToolResultViewService(
                work_dir=archive_work_dir,
                conversation_id=getattr(conversation, "id", None),
                conversation=conversation,
                compressor=compressor,
            )
            if (
                compressor is not None
                and view_service.needs_summary(text=result_text, archive_result=handle)
                and on_archive_prepare is not None
            ):
                on_archive_prepare()
            handle = await view_service.build_display(
                tool_name=tool_name,
                text=result_text,
                archive_result=handle,
            )

        metadata: dict[str, Any] = {"name": tool_name}
        if isinstance(result, ToolResult) and bool(getattr(result, "is_error", False)):
            metadata["is_error"] = True
        clean_parent_message_id = str(parent_message_id or "").strip()
        if clean_parent_message_id:
            metadata["parent_message_id"] = clean_parent_message_id
        if tool_call_id:
            metadata["tool_call_id"] = str(tool_call_id or "")

        try:
            metadata.update(handle.to_metadata())
            subtask = trace_for_tool_call(conversation, tool_call_id)
            if subtask and str(tool_name or "") == "agent__run":
                subtask_metadata = subtask.get("metadata") if isinstance(subtask.get("metadata"), dict) else {}
                metadata.update(
                    {
                        key: value
                        for key, value in dict(subtask_metadata or {}).items()
                        if value not in (None, "", [])
                    }
                )
                metadata["subtask_status"] = str(subtask.get("status") or "")
                metadata["subtask_summary"] = str(subtask.get("final_message") or subtask.get("error") or subtask.get("goal") or "")[:220]
                metadata["tool_count"] = int(subtask.get("tool_count") or metadata.get("tool_count") or 0)
                metadata["message_count"] = len([item for item in (subtask.get("messages") or []) if isinstance(item, dict)])
        except Exception as exc:
            logger.debug("Failed to set tool metadata: %s", exc)

        try:
            if summary:
                result_summary = summary
            elif handle.summary:
                result_summary = handle.summary
            elif handle.strategy == "inline":
                result_summary = self._summarize_tool_result(tool_name, handle.display)
            else:
                result_summary = ""
        except Exception as exc:
            logger.debug("Failed to summarize tool result for %s: %s", tool_name, exc)
            result_summary = summary or ""

        state_snapshot: dict[str, Any] | None = None
        try:
            state = conversation.get_state()
            if handle.archive is not None:
                remember_archive(state, handle.archive)
            self.record_work_trace_step(
                state=state,
                conversation=conversation,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result_summary=result_summary,
                metadata=metadata,
                handle=handle,
            )
            conversation.set_state(state)
            state_snapshot = state_checkpoint(state)
        except Exception as exc:
            logger.debug("Failed to snapshot state for tool result: %s", exc)

        result_payload: dict[str, Any] = {
            "type": "tool_result",
            "content": handle.display,
            "summary": result_summary,
            "metadata": metadata,
        }
        if tool_images:
            result_payload["images"] = list(tool_images)

        event_message = Message(
            role="tool",
            content=handle.display,
            tool_call_id=str(tool_call_id or ""),
            images=list(tool_images),
            metadata={
                "role": "tool_result",
                "tool_name": tool_name,
                "name": tool_name,
                "summary": result_summary,
                "result": result_payload,
                **dict(metadata),
            },
            state_snapshot=state_snapshot,
        )
        event_message.summary = result_summary

        return {
            "tool_call_id": str(tool_call_id or ""),
            "tool_name": tool_name,
            "result": result_payload,
            "images": list(tool_images),
            "state_snapshot": state_snapshot,
            "event": event_message,
        }

    @staticmethod
    def tool_result_to_string(result: ToolResult | str) -> str:
        if isinstance(result, ToolResult):
            return result.to_string()
        return str(result)

    @staticmethod
    def extract_tool_images(result: ToolResult | str) -> list[str]:
        if not isinstance(result, ToolResult):
            return []
        content = getattr(result, "content", None)
        if not isinstance(content, list):
            return []

        images: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") != "image":
                continue
            image_data = str(block.get("data") or "").strip()
            mime_type = str(block.get("mimeType") or "image/png").strip() or "image/png"
            if not image_data:
                continue
            if image_data.startswith(("data:", "http://", "https://")):
                images.append(image_data)
                continue
            images.append(f"data:{mime_type};base64,{image_data}")
        return images

    @staticmethod
    def record_work_trace_step(
        *,
        state,
        conversation: Conversation,
        tool_name: str,
        tool_call_id: Optional[str],
        result_summary: str,
        metadata: dict[str, Any],
        handle,
    ) -> None:
        label = ToolResultRecorder.work_trace_label(tool_name)
        target = ToolResultRecorder.work_trace_target(tool_name, metadata)
        refs: list[str] = []
        content_id = ""
        chars = 0
        try:
            archive = getattr(handle, "archive", None)
            if archive is not None:
                content_id = str(getattr(archive, "id", "") or "")
                chars = int(getattr(archive, "size", 0) or 0)
                archive_ref = str(getattr(archive, "original_ref", "") or "")
                if archive_ref:
                    refs.append(archive_ref)
                archive_metadata = getattr(archive, "metadata", {}) or {}
                refs.extend([str(item) for item in (archive_metadata.get("references") or []) if str(item).strip()])
        except Exception:
            pass
        for key in ("content_id", "archive_ref", "subtask_summary"):
            value = str(metadata.get(key) or "").strip()
            if value and value not in refs:
                refs.append(value)
        try:
            record_work_step(
                state,
                seq=int(conversation.current_seq_id() or 0),
                turn=0,
                kind=label,
                label=label,
                tool_name=str(tool_name or ""),
                target=target,
                status="completed",
                summary=str(result_summary or "")[:500],
                refs=refs[:8],
                content_id=content_id,
                chars=chars,
                goal=ToolResultRecorder.latest_user_goal(conversation),
            )
        except Exception as exc:
            logger.debug("Failed to record work trace for %s/%s: %s", tool_name, tool_call_id, exc)

    @staticmethod
    def work_trace_label(tool_name: str) -> str:
        name = str(tool_name or "").strip()
        if name.startswith("file__"):
            return {
                "file__read": "read",
                "file__list": "inspect",
                "file__search": "inspect",
                "file__write": "edit",
                "file__edit": "edit",
                "file__patch": "edit",
                "file__delete": "edit",
            }.get(name, "file")
        if name.startswith("web__"):
            return "search" if name == "web__search" else "read"
        if name.startswith("shell__") or name.startswith("python__"):
            return "execute"
        if name.startswith("state__artifact"):
            return "write_artifact"
        if name.startswith("state__todo"):
            return "update_todo"
        if name.startswith("state__memory"):
            return "update_memory"
        if name == "agent__run" or name.startswith("agent__"):
            return "delegate"
        if name.startswith("capability__"):
            return "summarize"
        return "tool"

    @staticmethod
    def work_trace_target(tool_name: str, metadata: dict[str, Any]) -> str:
        if str(tool_name or "") == "agent__run":
            return str(metadata.get("subtask_summary") or metadata.get("subtask_status") or "").strip()[:160]
        for key in ("content_id", "archive_ref", "mode_switch"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value[:160]
        return str(tool_name or "").strip()

    @staticmethod
    def latest_user_goal(conversation: Conversation) -> str:
        for msg in reversed(getattr(conversation, "messages", []) or []):
            if getattr(msg, "role", "") == "user" and str(getattr(msg, "content", "") or "").strip():
                return str(msg.content or "").strip()
        return ""
