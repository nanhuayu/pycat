from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from core.capabilities.executor import CapabilityExecutor
from core.llm.model_selection import ResolvedModelSelection
from models.provider import Provider


logger = logging.getLogger(__name__)


def _strip_code_fences(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    m = re.match(r"^```(?:\w+)?\s*\n(.*)\n```\s*$", s, flags=re.S)
    if m:
        return (m.group(1) or "").strip()
    return s


def _build_optimizer_message(raw_prompt: str) -> str:
    return (
        "请优化下面这段提示词，用于和大模型对话。\n"
        "要求：保持原意，不编造信息；结构清晰；保留占位符/变量/代码块；只输出优化后的提示词正文。\n\n"
        "原提示词：\n<<<\n" + (raw_prompt or "").strip() + "\n>>>"
    )


@dataclass(frozen=True)
class _OptimizerControl:
    request_id: str
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task


class PromptOptimizer(QObject):
    """Qt bridge for the ``prompt_optimize`` capability.

    Threading, signals and cancellation stay here; model execution goes through
    the shared CapabilityExecutor (single model-selection, trace and error path).
    """

    optimize_started = pyqtSignal(str, str)
    optimize_complete = pyqtSignal(str, str, str)
    optimize_error = pyqtSignal(str, str, str)
    optimize_cancelled = pyqtSignal(str, str)

    def __init__(
        self,
        capability_executor: CapabilityExecutor,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._capability_executor = capability_executor
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._controls: dict[str, _OptimizerControl] = {}

    def start(
        self,
        *,
        provider: Provider,
        conversation_id: str,
        raw_prompt: str,
        resolved_selection: ResolvedModelSelection | None = None,
    ) -> str:
        conversation_key = str(conversation_id or "")
        self.cancel(conversation_key)
        request_id = str(uuid.uuid4())
        with self._lock:
            self._active[conversation_key] = request_id

        self.optimize_started.emit(conversation_key, request_id)

        user_prompt = (raw_prompt or "").strip()

        def run() -> None:
            loop: asyncio.AbstractEventLoop | None = None
            task: asyncio.Task | None = None
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                async def do_call():
                    return await self._capability_executor.run(
                        provider=provider,
                        capability_id="prompt_optimize",
                        message=_build_optimizer_message(user_prompt),
                        resolved_selection=resolved_selection,
                    )

                task = loop.create_task(do_call())
                self._register_control(conversation_key, request_id, loop, task)
                result = loop.run_until_complete(task)
                content = _strip_code_fences(getattr(result, "content", "") or "")
                validation_error = str(getattr(result, "validation_error", "") or "").strip()
                if validation_error:
                    raise RuntimeError(validation_error)

                if self._claim_terminal(conversation_key, request_id):
                    self.optimize_complete.emit(conversation_key, request_id, content)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if self._claim_terminal(conversation_key, request_id):
                    self.optimize_error.emit(conversation_key, request_id, str(e))
            finally:
                # Idempotent cleanup: a no-op when the terminal was already
                # claimed above, the request was cancelled, or a newer start()
                # owns the conversation key (request_id no longer matches).
                self._claim_terminal(conversation_key, request_id)
                if loop is not None and not loop.is_closed():
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception as exc:
                        logger.debug("Failed to shutdown prompt optimizer async generators: %s", exc)
                    try:
                        loop.run_until_complete(loop.shutdown_default_executor())
                    except Exception as exc:
                        logger.debug("Failed to shutdown prompt optimizer default executor: %s", exc)
                    asyncio.set_event_loop(None)
                    loop.close()

        th = threading.Thread(target=run, name="PromptOptimizer", daemon=True)
        th.start()
        return request_id

    def cancel(self, conversation_id: str) -> bool:
        conversation_key = str(conversation_id or "")
        with self._lock:
            request_id = self._active.pop(conversation_key, "")
            control = self._controls.pop(conversation_key, None)
        if not request_id:
            return False
        if control is not None and control.request_id == request_id:
            try:
                control.loop.call_soon_threadsafe(self._cancel_task, control.task)
            except RuntimeError as exc:
                logger.debug("Failed to cancel prompt optimizer task: %s", exc)
        self.optimize_cancelled.emit(conversation_key, request_id)
        return True

    def is_active(self, conversation_id: str) -> bool:
        with self._lock:
            return str(conversation_id or "") in self._active

    def _register_control(
        self,
        conversation_id: str,
        request_id: str,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task,
    ) -> None:
        with self._lock:
            if self._active.get(conversation_id) == request_id:
                self._controls[conversation_id] = _OptimizerControl(request_id, loop, task)
                return
        task.cancel()

    def _claim_terminal(self, conversation_id: str, request_id: str) -> bool:
        with self._lock:
            if self._active.get(conversation_id) != request_id:
                return False
            self._active.pop(conversation_id, None)
            control = self._controls.get(conversation_id)
            if control is not None and control.request_id == request_id:
                self._controls.pop(conversation_id, None)
            return True

    @staticmethod
    def _cancel_task(task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
