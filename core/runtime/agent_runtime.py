from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from core.capabilities import CapabilitiesConfig, CapabilityConfig, default_capabilities_config
from core.capabilities.manager import CapabilitiesManager
from core.config.io import load_app_config
from core.llm.llm_config import LLMConfig
from core.runtime.policy_factory import RuntimePolicyFactory
from core.task.task import Task
from core.task.types import RunPolicy, TaskResult, TaskStatus
from core.tools.catalog import ToolSelectionPolicy
from core.tools.manager import ToolManager
from models.conversation import Conversation, Message
from models.provider import Provider, normalize_provider_name, split_model_ref

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeCallContext:
    caller: str = "internal"
    source: str = "internal"
    entrypoint: str = ""
    trace_id: str = ""
    parent_run_id: str = ""


@dataclass(frozen=True)
class RuntimeTextResult:
    content: str = ""
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    task_result: TaskResult | None = None


class AgentRuntime:
    """Single runtime facade for agent and capability execution."""

    def __init__(self, *, client: Any, tool_manager: ToolManager | None = None, task: Task | None = None) -> None:
        self.client = client
        self.tool_manager = tool_manager or getattr(client, "tool_manager", None) or ToolManager()
        self.task = task or Task(client=client, tool_manager=self.tool_manager)

    async def run_agent(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        on_event: Callable | None = None,
        on_token=None,
        on_thinking=None,
        approval_callback=None,
        questions_callback=None,
        cancel_event=None,
        debug_log_path: str | None = None,
    ) -> TaskResult:
        return await self.task.run(
            provider=provider,
            conversation=conversation,
            policy=policy,
            on_event=on_event,
            on_token=on_token,
            on_thinking=on_thinking,
            approval_callback=approval_callback,
            questions_callback=questions_callback,
            cancel_event=cancel_event,
            debug_log_path=debug_log_path,
        )

    async def run_capability(
        self,
        *,
        provider: Provider,
        capability_id: str,
        message: str,
        conversation: Conversation | None = None,
        context: RuntimeCallContext | None = None,
        config: CapabilitiesConfig | None = None,
        extra_system_contract: str = "",
        title: str = "",
    ) -> RuntimeTextResult:
        call_context = context or RuntimeCallContext()
        capability = self._resolve_capability(capability_id, config=config)
        if capability.hidden:
            raise ValueError(f"Capability is hidden and cannot be called: {capability_id}")

        if capability.execution_mode == "tool_limited_loop" or tuple(capability.allowed_tool_categories or ()):
            return await self._run_loop_capability(
                provider=provider,
                capability=capability,
                message=message,
                conversation=conversation,
                context=call_context,
                title=title,
            )
        return await self._run_direct_capability(
            provider=provider,
            capability=capability,
            message=message,
            conversation=conversation,
            extra_system_contract=extra_system_contract,
            title=title,
        )

    def _resolve_capability(self, capability_id: str, *, config: CapabilitiesConfig | None = None) -> CapabilityConfig:
        cfg = config
        if cfg is None:
            try:
                app_cfg = load_app_config()
                user_cfg = getattr(app_cfg, "capabilities", None)
                if isinstance(user_cfg, CapabilitiesConfig):
                    cfg = CapabilitiesManager.merge(default_capabilities_config(), user_cfg)
            except Exception as exc:
                logger.debug("Failed to load app capabilities: %s", exc)
        cfg = cfg or default_capabilities_config()
        capability = cfg.capability(capability_id)
        if capability is None:
            raise ValueError(f"Unknown capability: {capability_id}")
        return capability

    async def _run_direct_capability(
        self,
        *,
        provider: Provider,
        capability: CapabilityConfig,
        message: str,
        conversation: Conversation | None,
        extra_system_contract: str = "",
        title: str = "",
    ) -> RuntimeTextResult:
        model = self._select_model(provider=provider, model_ref=capability.model_ref, conversation=conversation)
        system_prompt = "\n\n".join(
            part
            for part in (str(capability.system_prompt or "").strip(), str(extra_system_contract or "").strip())
            if part
        )
        temp_conv = Conversation(
            id=str(title or f"runtime_{capability.id}_capability"),
            title=str(title or f"Capability: {capability.name}"),
            messages=[Message(role="user", content=str(message or ""))],
            mode="chat",
            work_dir=str(getattr(conversation, "work_dir", "") or "") if conversation is not None else "",
        )
        temp_conv.set_llm_config(
            LLMConfig(
                model=model,
                stream=False,
                system_prompt_override=system_prompt,
            )
        )
        response = await self.client.send_message(
            provider,
            temp_conv,
            enable_thinking=False,
            prepared_tools=[],
        )
        metadata = dict(getattr(response, "metadata", {}) or {})
        return RuntimeTextResult(
            content=str(getattr(response, "content", "") or ""),
            model=str(metadata.get("model") or model),
            metadata=metadata,
        )

    async def _run_loop_capability(
        self,
        *,
        provider: Provider,
        capability: CapabilityConfig,
        message: str,
        conversation: Conversation | None,
        context: RuntimeCallContext,
        title: str = "",
    ) -> RuntimeTextResult:
        child = Conversation(
            id=str(title or f"runtime_{capability.id}_capability"),
            title=str(title or f"Capability: {capability.name}"),
            messages=[Message(role="user", content=str(message or ""))],
            mode="agent",
            work_dir=str(getattr(conversation, "work_dir", "") or ".") if conversation is not None else ".",
        )
        if capability.system_prompt:
            child.set_llm_config(
                child.get_llm_config().with_updates(system_prompt_override=str(capability.system_prompt or "").strip())
            )
        policy = RuntimePolicyFactory.build(
            conversation=child,
            app_settings={},
            mode_slug="agent",
            tool_selection=ToolSelectionPolicy.from_categories(capability.allowed_tool_categories or ()),
            source=str(context.source or "internal"),
        )
        max_turns = self._capability_max_turns(capability)
        if max_turns:
            policy = replace(policy, max_turns=max_turns)
        if not capability.exposed_as_tool:
            policy = replace(policy, force_agent_complete=False)
        result = await self.task.run(provider=provider, conversation=child, policy=policy)
        content = ""
        if result.final_message is not None:
            content = str(result.final_message.content or "")
        elif result.error:
            content = str(result.error)
        return RuntimeTextResult(
            content=content,
            model=str(getattr(policy, "model", "") or ""),
            metadata={"status": getattr(result.status, "value", str(result.status))},
            task_result=result,
        )

    @staticmethod
    def _capability_max_turns(capability: CapabilityConfig) -> int | None:
        options = capability.options if isinstance(capability.options, dict) else {}
        try:
            value = int(options.get("max_turns") or 0)
        except Exception:
            value = 0
        return value if value > 0 else None

    @staticmethod
    def _select_model(*, provider: Provider, model_ref: str = "", conversation: Conversation | None = None) -> str:
        configured = str(model_ref or "").strip()
        if not configured and conversation is not None:
            configured = str(getattr(conversation, "model", "") or "").strip()
        if not configured:
            configured = str(getattr(provider, "default_model", "") or "").strip()
        provider_name, model_name = split_model_ref(configured)
        if provider_name and provider_name != normalize_provider_name(getattr(provider, "name", "")):
            logger.debug(
                "Ignoring capability provider override '%s' because active provider is '%s'",
                provider_name,
                normalize_provider_name(getattr(provider, "name", "")),
            )
        return model_name or configured
