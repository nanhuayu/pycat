from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.llm.request_builder import (
    build_api_messages as _build_api_messages,
    build_request_body as _build_request_body,
)
from core.prompts.system_builder import build_system_prompt
from core.prompts.sections import PromptSections
from models.contracts.config import AppConfig
from models.conversation import Conversation, Message
from models.llm_config import LLMConfig
from models.provider import Provider
from core.llm.token_budget import TokenBudget


class PromptRenderer:
    """Render prepared context/messages into provider request payloads."""

    def __init__(self, *, app_config: AppConfig | None = None, work_dir: str = ".") -> None:
        self.app_config = app_config or AppConfig()
        self.work_dir = work_dir

    def build_api_messages(
        self,
        messages: List[Message],
        provider: Provider,
        *,
        conversation: Conversation | None = None,
        tool_result_renderer=None,
    ) -> List[Dict[str, Any]]:
        return _build_api_messages(
            messages,
            provider,
            conversation=conversation,
            tool_result_renderer=tool_result_renderer,
        )

    def resolve_system_prompt(
        self,
        conversation: Conversation,
        tools: List[Dict[str, Any]],
        provider: Provider,
        *,
        app_config: AppConfig | None = None,
        llm_config: LLMConfig | None = None,
        sections: PromptSections | None = None,
        pycat_assistant_enabled: bool | None = None,
        completion_policy: str | None = None,
    ) -> str:
        cfg = app_config or self.app_config
        explicit_request_config = llm_config is not None
        request_cfg = llm_config or LLMConfig.from_conversation(conversation)
        if explicit_request_config and request_cfg.system_prompt_override.strip():
            return request_cfg.system_prompt_override.strip()

        work_dir = getattr(conversation, "work_dir", "") or self.work_dir
        sections = sections or PromptSections()
        return build_system_prompt(
            conversation=conversation,
            tools=tools,
            provider=provider,
            app_config=cfg,
            default_work_dir=work_dir,
            sections=sections,
            pycat_assistant_enabled=pycat_assistant_enabled,
            completion_policy=completion_policy,
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
        reasoning_mode: str | None = None,
        token_budget: TokenBudget | None = None,
        sections: PromptSections | None = None,
        pycat_assistant_enabled: bool | None = None,
        completion_policy: str | None = None,
    ) -> Dict[str, Any]:
        request_cfg = llm_config or LLMConfig.from_conversation(conversation)
        profile = provider.effective_model_profile(request_cfg.resolved_model())
        effective_tools = tools if bool(getattr(profile, "supports_tools", True)) else []
        prepared_messages = list(api_messages)
        if not any(str(msg.get("role") or "") == "system" for msg in prepared_messages):
            system_prompt = self.resolve_system_prompt(
                conversation,
                effective_tools or [],
                provider,
                app_config=app_config,
                llm_config=request_cfg,
                sections=sections,
                pycat_assistant_enabled=pycat_assistant_enabled,
                completion_policy=completion_policy,
            )
            if system_prompt:
                prepared_messages.insert(0, {"role": "system", "content": system_prompt})
        return _build_request_body(
            provider,
            conversation,
            prepared_messages,
            tools=effective_tools,
            llm_config=request_cfg,
            reasoning_mode=reasoning_mode,
            token_budget=token_budget,
        )
