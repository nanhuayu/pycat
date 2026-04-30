from __future__ import annotations

from typing import Any, Dict, List, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.config import AppConfig, load_app_config
from core.llm.llm_config import LLMConfig
from core.llm.request_builder import (
    build_api_messages as _build_api_messages,
    build_request_body as _build_request_body,
    select_base_messages as _select_base_messages,
)
from core.prompts.system import PromptManager


class PromptAssembler:
    """Facade that centralizes prompt/history/request assembly.

    Phase 1 keeps the proven request-builder logic underneath while giving the
    runtime a single, injectable assembly entry point.
    """

    def __init__(self, work_dir: str = ".") -> None:
        self.work_dir = work_dir

    def select_base_messages(
        self,
        conversation: Conversation,
        *,
        app_config: AppConfig | None = None,
    ) -> List[Message]:
        return _select_base_messages(conversation, app_config=app_config)

    def build_api_messages(
        self,
        messages: List[Message],
        provider: Provider,
        *,
        conversation: Conversation | None = None,
    ) -> List[Dict[str, Any]]:
        return _build_api_messages(messages, provider, conversation=conversation)

    def resolve_system_prompt(
        self,
        conversation: Conversation,
        tools: List[Dict[str, Any]],
        provider: Provider,
        *,
        app_config: AppConfig | None = None,
        llm_config: LLMConfig | None = None,
    ) -> str:
        cfg = app_config
        if cfg is None:
            try:
                cfg = load_app_config()
            except Exception:
                cfg = AppConfig()

        request_cfg = llm_config or LLMConfig.from_conversation(conversation)
        if request_cfg.system_prompt_override.strip():
            return request_cfg.system_prompt_override.strip()

        work_dir = getattr(conversation, "work_dir", "") or self.work_dir
        prompt_manager = PromptManager(work_dir)
        return prompt_manager.get_system_prompt(
            conversation,
            tools,
            provider,
            app_config=cfg,
        )

    def build_request_body(
        self,
        provider: Provider,
        conversation: Conversation,
        api_messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        app_config: AppConfig | None = None,
        llm_config: LLMConfig | None = None,
    ) -> Dict[str, Any]:
        return _build_request_body(
            provider,
            conversation,
            api_messages,
            tools=tools,
            app_config=app_config,
            llm_config=llm_config,
        )
