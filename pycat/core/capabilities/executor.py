"""Execute configured LLM transformations through one shared runtime boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from pycat.core.capabilities.defaults import default_capabilities_config
from pycat.core.capabilities.manager import CapabilitiesManager
from pycat.core.capabilities.validation import output_schema_contract, parse_and_validate_output, validate_json_value
from pycat.core.content.images import decode_image
from pycat.core.llm.images import with_cancellation
from pycat.core.llm.model_selection import ResolvedModelSelection, resolve_model_target
from pycat.core.prompts.renderer import PromptRenderer
from pycat.models.contracts.agent import RunPolicy, RunStatus
from pycat.models.contracts.capability import CapabilitiesConfig, CapabilityConfig, ImageGenerationOptions
from pycat.models.contracts.tooling import ToolSelectionPolicy
from pycat.models.conversation import Conversation, Message
from pycat.models.llm_config import LLMConfig
from pycat.models.provider import Provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilityRunContext:
    caller: str = "internal"
    source: str = "internal"
    entrypoint: str = ""
    trace_id: str = ""
    parent_run_id: str = ""
    debug_trace: Any = None
    parent_policy: RunPolicy | None = None
    approval_callback: Any = None
    questions_callback: Any = None
    cancel_event: Any = None


@dataclass(frozen=True)
class CapabilityRunResult:
    content: str = ""
    parsed: Any = None
    validation_error: str = ""
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    images: tuple[str, ...] = ()


class CapabilityExecutor:
    """Execute one capability as a direct call or through the shared AgentRuntime."""

    def __init__(
        self,
        *,
        client: Any,
        prompt_renderer: PromptRenderer,
        capabilities: CapabilitiesConfig | None = None,
        provider_catalog_provider: Callable[[], Iterable[Provider]] | None = None,
        default_auxiliary_model: str = "",
        data_dir: str | None = None,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.prompt_renderer = prompt_renderer
        self.capabilities = capabilities or default_capabilities_config()
        self._provider_catalog_provider = provider_catalog_provider or (lambda: ())
        self._default_auxiliary_model = str(default_auxiliary_model or "").strip()
        self._agent_runtime: Any = None

    def bind_agent_runtime(self, runtime: Any) -> None:
        self._agent_runtime = runtime

    def update_configuration(
        self,
        *,
        capabilities: CapabilitiesConfig,
        default_auxiliary_model: str = "",
    ) -> None:
        self.capabilities = CapabilitiesManager.merge(default_capabilities_config(), capabilities)
        self._default_auxiliary_model = str(default_auxiliary_model or "").strip()

    def get_capability(
        self,
        capability_id: str,
        *,
        config: CapabilitiesConfig | None = None,
    ) -> CapabilityConfig:
        cfg = config or self.capabilities or default_capabilities_config()
        if config is not None:
            cfg = CapabilitiesManager.merge(default_capabilities_config(), config)
        capability = cfg.capability(capability_id)
        if capability is None:
            raise ValueError(f"Unknown capability: {capability_id}")
        return capability

    async def run(
        self,
        *,
        provider: Provider,
        capability_id: str,
        message: str,
        conversation: Conversation | None = None,
        context: CapabilityRunContext | None = None,
        config: CapabilitiesConfig | None = None,
        extra_system_contract: str = "",
        title: str = "",
        input_data: dict[str, Any] | None = None,
        images: list[str] | None = None,
        mask: str | None = None,
        resolved_selection: ResolvedModelSelection | None = None,
    ) -> CapabilityRunResult:
        return await self.run_capability(
            provider=provider,
            capability_id=capability_id,
            message=message,
            conversation=conversation,
            context=context,
            config=config,
            extra_system_contract=extra_system_contract,
            title=title,
            input_data=input_data,
            images=images,
            mask=mask,
            resolved_selection=resolved_selection,
        )

    async def run_capability(
        self,
        *,
        provider: Provider,
        capability_id: str,
        message: str,
        conversation: Conversation | None = None,
        context: CapabilityRunContext | None = None,
        config: CapabilitiesConfig | None = None,
        extra_system_contract: str = "",
        title: str = "",
        input_data: dict[str, Any] | None = None,
        images: list[str] | None = None,
        mask: str | None = None,
        resolved_selection: ResolvedModelSelection | None = None,
    ) -> CapabilityRunResult:
        capability = self.get_capability(capability_id, config=config)
        if not capability.enabled:
            raise ValueError(f"Capability is disabled: {capability_id}")
        input_error = validate_json_value(input_data, capability.input_schema) if input_data is not None else ""
        if input_error:
            return CapabilityRunResult(validation_error=f"Input schema validation failed: {input_error}")

        call_context = context or CapabilityRunContext()
        selection = resolved_selection or self._resolve_model_selection(provider, capability, conversation)
        execution_provider = selection.provider or provider
        model = str(selection.model or "").strip()
        if execution_provider is None or not model:
            raise ValueError(f"No model is available for capability: {capability_id}")
        profile = execution_provider.effective_model_profile(model)
        if capability.operation == "image":
            if not capability.model_target.model_ref or selection.source != "explicit":
                raise ValueError("请先为图像能力指定一个可用的图像模型。")
            values = input_data or {}
            overrides = {key: values[key] for key in ("size", "quality", "output_format", "background", "n") if key in values}
            response = await self.client.generate_image(
                provider=execution_provider, model=model,
                prompt="\n\n".join(part for part in (capability.prompt, message) if part),
                options=ImageGenerationOptions.from_dict({**capability.image_options.to_dict(), **overrides}),
                images=[decode_image(image).data for image in images or []],
                mask=decode_image(mask).data if mask else None,
                cancel_event=getattr(call_context, "cancel_event", None),
            )
            return CapabilityRunResult(content=response.content, model=model, images=tuple(response.images),
                                       metadata={**response.metadata, "deliver_images": True})
        if profile.model_type != "chat":
            raise ValueError("文本或视觉识别能力需要聊天模型，不能使用生图模型。")
        if images and not profile.supports_input("image"):
            raise ValueError("所选模型不支持图片输入，请选择视觉模型。")
        if capability.runtime == "agent_loop":
            return await self._run_agent_loop(
                provider=provider,
                capability=capability,
                message=message,
                conversation=conversation,
                context=call_context,
                extra_system_contract=extra_system_contract,
                title=title,
                images=images,
                resolved_selection=selection,
            )

        system_prompt = "\n\n".join(
            part
            for part in (
                str(capability.prompt or "").strip(),
                str(extra_system_contract or "").strip(),
                output_schema_contract(capability.output_schema),
            )
            if part
        )
        temp_conversation = Conversation(
            data_dir=getattr(conversation, "data_dir", None) or self.data_dir or "",
            title=str(title or f"Capability: {capability.name}"),
            messages=[Message(role="user", content=str(message or ""), images=list(images or []))],
            mode="chat",
            work_dir=str(getattr(conversation, "work_dir", "") or "") if conversation is not None else "",
        )
        request_config = LLMConfig(
            model=model,
            stream=False,
            system_prompt_override=system_prompt,
            temperature=capability.temperature,
            max_tokens=capability.max_tokens,
        )
        temp_conversation.set_llm_config(request_config)
        api_messages = self.prompt_renderer.build_api_messages(
            temp_conversation.messages,
            execution_provider,
            conversation=temp_conversation,
        )
        request_body = self.prompt_renderer.build_request_body(
            execution_provider,
            temp_conversation,
            api_messages,
            tools=[],
            llm_config=request_config,
            reasoning_mode="off",
        )
        response = await with_cancellation(self.client.send_request(
            provider=execution_provider,
            request_body=request_body,
            show_thinking=False,
            debug_trace=getattr(call_context, "debug_trace", None),
            debug_turn=int(getattr(getattr(call_context, "debug_trace", None), "turn", 0) or 0),
            debug_purpose=str(
                getattr(getattr(call_context, "debug_trace", None), "default_purpose", "") or "capability"
            ),
            conversation_id=str(temp_conversation.id or ""),
            model_hint=model,
            cancel_event=getattr(call_context, "cancel_event", None),
        ), getattr(call_context, "cancel_event", None))
        metadata = dict(getattr(response, "metadata", {}) or {})
        content = str(getattr(response, "content", "") or "")
        if metadata.get("runtime_error"):
            return CapabilityRunResult(
                content=content,
                validation_error=content.strip() or "Model request failed",
                model=str(metadata.get("model") or model),
                metadata=metadata,
            )
        if metadata.get("incomplete"):
            return CapabilityRunResult(
                content=content,
                validation_error="Model response incomplete: " + str(metadata.get("incomplete_reason") or metadata.get("finish_reason") or "unknown"),
                model=str(metadata.get("model") or model), metadata=metadata,
            )
        parsed, validation_error = parse_and_validate_output(content, capability.output_schema)
        return CapabilityRunResult(
            content=content,
            parsed=parsed,
            validation_error=validation_error,
            model=str(metadata.get("model") or model),
            metadata=metadata,
        )

    async def _run_agent_loop(
        self,
        *,
        provider: Provider,
        capability: CapabilityConfig,
        message: str,
        conversation: Conversation | None,
        context: CapabilityRunContext,
        extra_system_contract: str,
        title: str,
        images: list[str] | None,
        resolved_selection: ResolvedModelSelection | None,
    ) -> CapabilityRunResult:
        if self._agent_runtime is None:
            return CapabilityRunResult(validation_error="Capability Agent runtime is unavailable.")
        selection = resolved_selection or self._resolve_model_selection(provider, capability, conversation)
        execution_provider = selection.provider or provider
        model = str(selection.model or "").strip()
        if not model:
            return CapabilityRunResult(validation_error=f"No model is available for capability: {capability.id}")

        system_prompt = "\n\n".join(
            part
            for part in (
                str(capability.prompt or "").strip(),
                str(extra_system_contract or "").strip(),
                output_schema_contract(capability.output_schema),
                "Complete only by calling agent__complete with the final result.",
            )
            if part
        )
        temp_conversation = Conversation(
            data_dir=getattr(conversation, "data_dir", None) or self.data_dir or "",
            title=str(title or f"Capability: {capability.name}"),
            messages=[Message(role="user", content=str(message or ""), images=list(images or []))],
            mode="agent",
            work_dir=str(getattr(conversation, "work_dir", "") or "") if conversation is not None else "",
        )
        if conversation is not None:
            try:
                temp_conversation.set_state(conversation.get_state())
            except Exception:
                pass
        temp_conversation.set_llm_config(
            LLMConfig(
                model=model,
                stream=False,
                system_prompt_override=system_prompt,
                temperature=capability.temperature,
                max_tokens=capability.max_tokens,
            )
        )

        capability_selection = ToolSelectionPolicy.from_categories(capability.allowed_tool_categories)
        parent_policy = context.parent_policy
        if parent_policy is not None:
            policy = replace(
                parent_policy,
                mode="agent",
                max_turns=int(capability.max_turns or 20),
                completion_policy="explicit",
                completion_schema=dict(capability.output_schema or {}) or None,
                tool_selection=parent_policy.tool_selection.intersect(capability_selection),
                model=model,
                pycat_assistant_enabled=True,
                source="capability",
            )
        else:
            policy = RunPolicy(
                mode="agent",
                max_turns=int(capability.max_turns or 20),
                completion_policy="explicit",
                completion_schema=dict(capability.output_schema or {}) or None,
                tool_selection=capability_selection,
                model=model,
                source="capability",
            )
        run_result = await self._agent_runtime.run(
            provider=execution_provider,
            conversation=temp_conversation,
            policy=policy,
            approval_callback=context.approval_callback,
            questions_callback=context.questions_callback,
            debug_trace=context.debug_trace,
        )
        content = str(getattr(run_result.final_message, "content", "") or "")
        parsed, validation_error = parse_and_validate_output(content, capability.output_schema)
        if run_result.status != RunStatus.COMPLETED and not validation_error:
            validation_error = f"Capability run ended with status {run_result.status.value}."
        return CapabilityRunResult(
            content=content,
            parsed=parsed,
            validation_error=validation_error,
            model=model,
            metadata={
                "status": run_result.status.value,
                "stop_reason": run_result.stop_reason.value,
            },
        )

    def resolve_capability_target(
        self,
        *,
        provider: Provider | None,
        capability_id: str,
        conversation: Conversation | None = None,
        config: CapabilitiesConfig | None = None,
    ) -> ResolvedModelSelection:
        capability = self.get_capability(capability_id, config=config)
        return self._resolve_model_selection(provider, capability, conversation)

    def _resolve_model_selection(
        self,
        provider: Provider | None,
        capability: CapabilityConfig,
        conversation: Conversation | None,
    ) -> ResolvedModelSelection:
        try:
            providers = list(self._provider_catalog_provider() or ())
        except Exception as exc:
            logger.debug("Failed to read provider catalog for capability %s: %s", capability.id, exc)
            providers = []
        if provider is not None and provider not in providers:
            providers.append(provider)
        primary_model = str(getattr(conversation, "model", "") or "").strip() if conversation else ""
        selection = resolve_model_target(
            providers,
            capability.model_target,
            primary_provider=provider,
            primary_model=primary_model,
            auxiliary_model_ref=self._default_auxiliary_model,
        )
        if selection.fallback_from:
            logger.warning(
                "Capability %s requested model is unavailable; using %s",
                capability.id,
                selection.model or "<empty>",
            )
        return selection
