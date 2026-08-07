from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from cli.output import CliOutput
from core.agent.policy import RunPolicyBuilder
from core.app.runtime_paths import get_debug_log_path
from core.app.state import ConversationSelection
from core.content.references import delivery_refs_for_messages
from core.content.resolver import SessionContentResolver
from core.llm.model_selection import (
    provider_model_ids,
    resolve_provider_model_ref,
    select_default_provider_model,
)
from core.modes.manager import ModeManager
from core.tools.base import ToolApprovalRequest
from models.contracts.agent import RunStatus
from models.conversation import Conversation, Message
from models.model_ref import build_model_ref, provider_matches_name, split_model_ref
from models.provider import Provider


@dataclass(frozen=True)
class CliRunRequest:
    prompt: str
    mode: str = "chat"
    provider: str = ""
    model: str = ""
    work_dir: str = ""
    conversation_id: str = ""
    output: str = "text"
    permission: str = ""


class CliExecutor:
    """Thin CLI adapter over the application runtime services."""

    def __init__(self, container: Any | None = None) -> None:
        if container is None:
            from core.app.container import AppContainer

            container = AppContainer()
        self.container = container
        self.services = container.services

    def load_bootstrap(self):
        return self.services.app_bootstrap.load()

    async def run_once(self, request: CliRunRequest, output: CliOutput | None = None) -> int:
        out = output or CliOutput(mode=request.output)
        bootstrap = self.load_bootstrap()
        providers = list(bootstrap.providers)
        settings = dict(bootstrap.settings or {})
        provider, model = self.resolve_model(providers, settings, provider_arg=request.provider, model_arg=request.model)
        if provider is None:
            out.final(status="failed", error="No provider configured.")
            return 2

        conversation, created = self._load_or_create_conversation(request)
        self._configure_conversation(
            conversation,
            provider=provider,
            model=model,
            mode=request.mode,
            work_dir=request.work_dir,
            settings=settings,
            permission=request.permission,
            initialize_workspace=created,
        )
        conversation.add_message_with_seq(Message(role="user", content=request.prompt))
        self.services.conv_service.ensure_title(conversation)

        policy = self.build_run_policy(
            request,
            conversation=conversation,
            settings=settings,
        )
        debug_log_path = get_debug_log_path(settings, self.services.data_dir)

        approval_callback = self._build_approval_callback(out)
        run_message_start = len(conversation.messages)

        result = await self.services.agent_runtime.run(
            provider=provider,
            conversation=conversation,
            policy=policy,
            on_event=out.event,
            on_token=out.token,
            on_thinking=out.thinking,
            approval_callback=approval_callback,
            questions_callback=None,
            cancel_event=None,
            debug_log_path=debug_log_path,
        )
        self.services.conv_service.save(conversation)

        final_text = str(getattr(getattr(result, "final_message", None), "content", "") or "")
        error = str(getattr(result, "error", "") or "")
        status = getattr(getattr(result, "status", RunStatus.COMPLETED), "value", str(getattr(result, "status", "completed")))
        out.final(
            status=status,
            message=final_text,
            error=error,
            conversation_id=str(getattr(conversation, "id", "") or ""),
            deliveries=self._delivery_payloads(
                conversation,
                result,
                start_index=run_message_start,
            ),
        )
        return 0 if getattr(result, "status", RunStatus.COMPLETED) == RunStatus.COMPLETED else 1

    def _delivery_payloads(
        self,
        conversation: Conversation,
        result,
        *,
        start_index: int,
    ) -> list[dict[str, Any]]:
        final_message = getattr(result, "final_message", None)
        messages = list((getattr(conversation, "messages", []) or [])[max(0, int(start_index)):])
        if isinstance(final_message, Message) and all(
            str(getattr(item, "id", "") or "") != final_message.id for item in messages
        ):
            messages.append(final_message)
        refs = delivery_refs_for_messages(messages)
        resolver = SessionContentResolver(self.services.content_service)
        payloads: list[dict[str, Any]] = []
        for ref in refs:
            item = ref.to_dict()
            try:
                item["path"] = str(resolver.resolve(conversation, ref))
            except Exception:
                item["path"] = ""
            payloads.append(item)
        return payloads

    async def chat(self, *, mode: str = "chat", provider: str = "", model: str = "", work_dir: str = "", output_mode: str = "text", permission: str = "") -> int:
        conversation_id = ""
        out = CliOutput(mode=output_mode)
        out.note("PyCat CLI chat. Type /exit or /quit to leave.")
        while True:
            prompt = out.read_line("> ")
            if prompt is None:
                return 0
            text = str(prompt or "").strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                return 0
            if text == "/clear":
                conversation_id = ""
                out.note("Conversation cleared.")
                continue
            request = CliRunRequest(
                prompt=text,
                mode=mode,
                provider=provider,
                model=model,
                work_dir=work_dir,
                conversation_id=conversation_id,
                output=output_mode,
                permission=permission,
            )
            code = await self.run_once(request, out)
            if code != 0:
                return code
            if not conversation_id:
                conversations = self.services.conv_service.list_all()
                if conversations:
                    conversation_id = str(conversations[0].get("id") or "")

    @staticmethod
    def build_run_policy(
        request: CliRunRequest,
        *,
        conversation: Conversation,
        settings: dict[str, Any],
    ):
        """Map CLI inputs to the canonical run-policy builder."""

        return RunPolicyBuilder.build(
            conversation=conversation,
            app_settings=settings,
            mode_slug=request.mode,
            work_dir=request.work_dir or getattr(conversation, "work_dir", ""),
            source="cli",
        )

    def _build_approval_callback(self, out: CliOutput):
        async def ask_each(request: ToolApprovalRequest) -> bool:
            return self._prompt_for_approval(request, out)

        return ask_each

    @staticmethod
    def _prompt_for_approval(request: ToolApprovalRequest, out: CliOutput) -> bool:
        tool = str(getattr(request, "tool_name", "") or "tool")
        risk = str(getattr(request, "risk", "") or "").strip() or "unknown"
        message = str(getattr(request, "message", "") or "").strip()
        if out.json_mode:
            out.write_json({"type": "approval_request", "tool_name": tool, "risk": risk, "message": message})
        else:
            out.note(f"[approval:ask] {message or tool} (tool={tool}, risk={risk})")
        answer = out.read_line("Allow? [y/N]: ")
        if answer is None:
            out.note(f"[approval:deny] 非交互输入，已拒绝工具调用：{tool}")
            return False
        return str(answer or "").strip().lower() in {"y", "yes"}

    def list_items(self, kind: str, *, output_mode: str = "text", work_dir: str = "") -> int:
        out = CliOutput(mode=output_mode)
        bootstrap = self.load_bootstrap()
        normalized = str(kind or "").strip().lower()
        if normalized == "modes":
            rows = [
                {"slug": m.slug, "name": m.name, "source": m.source or "builtin"}
                for m in ModeManager(work_dir).list_modes()
            ]
            return self._print_rows(out, rows, ["slug", "name", "source"])
        if normalized == "providers":
            rows = [
                {"id": p.id, "name": p.name, "api_type": p.api_type, "enabled": bool(getattr(p, "enabled", True))}
                for p in bootstrap.providers
            ]
            return self._print_rows(out, rows, ["id", "name", "api_type", "enabled"])
        if normalized == "models":
            rows: list[dict[str, Any]] = []
            for p in bootstrap.providers:
                for model in provider_model_ids(p):
                    rows.append({"provider": p.name, "model": model, "ref": build_model_ref(p.name, model)})
            return self._print_rows(out, rows, ["provider", "model", "ref"])
        if normalized == "tools":
            descriptors = self.services.tool_manager.list_tool_descriptors()
            rows = [
                {"name": name, "category": descriptor.category, "source": descriptor.source}
                for name, descriptor in sorted(descriptors.items())
            ]
            return self._print_rows(out, rows, ["name", "category", "source"])
        if normalized == "conversations":
            rows = list(bootstrap.conversations)
            return self._print_rows(out, rows, ["id", "title", "updated_at"])
        out.final(status="failed", error=f"Unknown list target: {kind}")
        return 2

    def resolve_model(self, providers: Iterable[Provider], settings: dict[str, Any], *, provider_arg: str = "", model_arg: str = "") -> tuple[Provider | None, str]:
        provider_list = [p for p in providers if bool(getattr(p, "enabled", True))]
        provider_token = str(provider_arg or os.environ.get("PYCAT_PROVIDER") or "").strip()
        model_token = str(model_arg or os.environ.get("PYCAT_MODEL") or "").strip()

        if provider_token and "|" in provider_token and not model_token:
            provider_token, model_token = split_model_ref(provider_token)
        elif model_token and "|" in model_token and not provider_token:
            provider_token, model_token = split_model_ref(model_token)

        if not provider_token and not model_token:
            selection = select_default_provider_model(
                provider_list,
                default_model_ref=str(settings.get("default_chat_model") or "").strip(),
            )
            return selection.provider, selection.model

        if model_token and not provider_token:
            selection = resolve_provider_model_ref(provider_list, model_token)
            if selection.provider is not None:
                return selection.provider, selection.model

        provider = self._find_provider(provider_list, provider_token)
        if provider is None and provider_list:
            provider = provider_list[0]
        model = model_token
        if not model and provider is not None:
            models = provider_model_ids(provider)
            model = models[0] if models else ""
        return provider, model

    def _load_or_create_conversation(self, request: CliRunRequest) -> tuple[Conversation, bool]:
        conversation_id = str(request.conversation_id or "").strip()
        if conversation_id:
            loaded = self.services.conv_service.load(conversation_id)
            if loaded is not None:
                return loaded, False
        title = request.prompt.strip().replace("\n", " ")[:50] or "CLI Chat"
        return self.services.conv_service.create(title=title), True

    def _configure_conversation(
        self,
        conversation: Conversation,
        *,
        provider: Provider,
        model: str,
        mode: str,
        work_dir: str,
        settings: dict[str, Any],
        permission: str,
        initialize_workspace: bool = False,
    ) -> None:
        selection = ConversationSelection(
            provider_id=str(getattr(provider, "id", "") or ""),
            provider_name=str(getattr(provider, "name", "") or ""),
            api_type=str(getattr(provider, "api_type", "") or ""),
            model=str(model or ""),
            mode_slug=str(mode or "chat"),
            work_dir=str(
                work_dir
                or (os.getcwd() if initialize_workspace else getattr(conversation, "work_dir", ""))
                or ""
            ),
            show_thinking=bool(settings.get("show_thinking", True)),
        )
        self.services.app_coordinator.apply_selection(
            conversation,
            selection,
            initialize_workspace=initialize_workspace,
        )
        normalized = str(permission or "").strip().lower()
        if normalized in {"default", "ask", "deny", "allow", "custom"}:
            self.services.conv_service.set_settings(
                conversation,
                {"permission_preset": normalized},
            )

    @staticmethod
    def _find_provider(providers: list[Provider], token: str) -> Provider | None:
        value = str(token or "").strip()
        if not value:
            return None
        for provider in providers:
            if str(getattr(provider, "id", "") or "") == value:
                return provider
            if provider_matches_name(provider, value):
                return provider
        return None

    @staticmethod
    def _print_rows(out: CliOutput, rows: list[dict[str, Any]], columns: list[str]) -> int:
        if out.json_mode:
            out.write_json({"type": "list", "items": rows})
            return 0
        for row in rows:
            print("\t".join(str(row.get(column, "") or "") for column in columns), file=out.stream)
        return 0
