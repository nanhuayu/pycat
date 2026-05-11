from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from core.llm.client import LLMClient
from core.llm.llm_config import LLMConfig
from core.capabilities.defaults import DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT
from models.conversation import Conversation, Message
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


class PromptOptimizer(QObject):
    optimize_started = pyqtSignal(str, str)
    optimize_complete = pyqtSignal(str, str, str)
    optimize_error = pyqtSignal(str, str, str)
    optimize_cancelled = pyqtSignal(str, str)

    def __init__(self, client: LLMClient, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._client = client
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}

    def start(
        self,
        *,
        provider: Provider,
        conversation_id: str,
        raw_prompt: str,
        model: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 800,
    ) -> str:
        request_id = str(uuid.uuid4())
        with self._lock:
            self._active[str(conversation_id or "")] = request_id

        self.optimize_started.emit(conversation_id, request_id)

        sys_prompt = (system_prompt or "").strip() or DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT
        user_prompt = (raw_prompt or "").strip()

        def run() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                conv = Conversation()
                conv.set_llm_config(
                    LLMConfig(
                        provider_id=getattr(provider, "id", "") or "",
                        provider_name=getattr(provider, "name", "") or "",
                        model=str(model or "").strip(),
                        stream=False,
                        system_prompt_override=sys_prompt,
                        temperature=0.2,
                        max_tokens=int(max_tokens),
                    )
                )

                conv.messages.append(
                    Message(
                        role="user",
                        content=(
                            "请优化下面这段提示词，用于和大模型对话。\n"
                            "要求：保持原意，不编造信息；结构清晰；保留占位符/变量/代码块；只输出优化后的提示词正文。\n\n"
                            "原提示词：\n<<<\n" + user_prompt + "\n>>>"
                        ),
                    )
                )

                async def do_call():
                    return await self._client.send_message(
                        provider,
                        conv,
                        enable_thinking=False,
                        prepared_tools=[],
                    )

                msg = loop.run_until_complete(do_call())
                content = _strip_code_fences(getattr(msg, "content", "") or "")

                with self._lock:
                    if self._active.get(str(conversation_id or "")) != request_id:
                        return

                self.optimize_complete.emit(conversation_id, request_id, content)

            except Exception as e:
                with self._lock:
                    if self._active.get(str(conversation_id or "")) != request_id:
                        return
                self.optimize_error.emit(conversation_id, request_id, str(e))
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception as exc:
                    logger.debug("Failed to shutdown prompt optimizer async generators: %s", exc)
                try:
                    loop.run_until_complete(loop.shutdown_default_executor())
                except Exception as exc:
                    logger.debug("Failed to shutdown prompt optimizer default executor: %s", exc)
                try:
                    loop.close()
                except Exception as exc:
                    logger.debug("Failed to close prompt optimizer event loop: %s", exc)

        th = threading.Thread(target=run, name="PromptOptimizer", daemon=True)
        th.start()
        return request_id

    def cancel(self, conversation_id: str) -> bool:
        conversation_key = str(conversation_id or "")
        with self._lock:
            request_id = self._active.pop(conversation_key, "")
        if not request_id:
            return False
        self.optimize_cancelled.emit(conversation_key, request_id)
        return True
