from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from cli.output import CliOutput
from core.app.state import ConversationSelection
from core.modes.manager import ModeManager
from core.runtime.policy_factory import RuntimePolicyFactory
from core.task.types import TaskStatus
from models.conversation import Conversation, Message
from models.model_ref import split_model_ref
from models.provider import Provider, build_model_ref, provider_matches_name
from services.agent_service import AgentService


@dataclass(frozen=True)
class CliRunRequest:
    prompt: str
    mode: str = "chat"
    provider: str = ""
    model: str = ""
    work_dir: str = ""
    conversation_id: str = ""
    output: str = "text"


class CliExecutor:
    """Thin CLI adapter over the application runtime services."""

    def __init__(self, container: Any | None = None) -> None:
        if container is None:
            from core.container import AppContainer

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

        conversation = self._load_or_create_conversation(request)
        self._configure_conversation(
            conversation,
            provider=provider,
            model=model,
            mode=request.mode,
            work_dir=request.work_dir,
            settings=settings,
        )
        conversation.add_message_with_seq(Message(role="user", content=request.prompt))
        self.services.conv_service.ensure_title(conversation)

        policy = RuntimePolicyFactory.build(
            conversation=conversation,
            app_settings=settings,
            mode_slug=request.mode,
            work_dir=request.work_dir or getattr(conversation, "work_dir", ""),
            source="desktop",
        )
        debug_log_path = AgentService.get_debug_log_path(settings, self.services.storage)

        async def approval_callback(_message: str) -> bool:
            return True

        result = await self.services.turn_engine.run(
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
        status = getattr(getattr(result, "status", TaskStatus.COMPLETED), "value", str(getattr(result, "status", "completed")))
        out.final(status=status, message=final_text, error=error, conversation_id=str(getattr(conversation, "id", "") or ""))
        return 0 if getattr(result, "status", TaskStatus.COMPLETED) == TaskStatus.COMPLETED else 1

    async def chat(self, *, mode: str = "chat", provider: str = "", model: str = "", work_dir: str = "", output_mode: str = "text") -> int:
        conversation_id = ""
        print("PyCat CLI chat. Type /exit or /quit to leave.")
        while True:
            try:
                prompt = input("> ")
            except EOFError:
                print("")
                return 0
            text = str(prompt or "").strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                return 0
            if text == "/clear":
                conversation_id = ""
                print("Conversation cleared.")
                continue
            request = CliRunRequest(
                prompt=text,
                mode=mode,
                provider=provider,
                model=model,
                work_dir=work_dir,
                conversation_id=conversation_id,
                output=output_mode,
            )
            out = CliOutput(mode=output_mode)
            code = await self.run_once(request, out)
            if code != 0:
                return code
            if not conversation_id:
                conversations = self.services.conv_service.list_all()
                if conversations:
                    conversation_id = str(conversations[0].get("id") or "")

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
                models = [profile.model_id for profile in p.get_model_profiles()]
                if not models and p.default_model:
                    models = [p.default_model]
                for model in models:
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
            for key in ("primary_model_ref", "default_model_ref", "model_ref"):
                ref = str(settings.get(key) or "").strip()
                if ref:
                    provider_token, model_token = split_model_ref(ref)
                    break

        provider = self._find_provider(provider_list, provider_token)
        if provider is None and provider_list:
            provider = provider_list[0]
        model = model_token or str(getattr(provider, "default_model", "") or "").strip()
        if not model and provider is not None:
            profiles = provider.get_model_profiles()
            if profiles:
                model = profiles[0].model_id
        return provider, model

    def _load_or_create_conversation(self, request: CliRunRequest) -> Conversation:
        conversation_id = str(request.conversation_id or "").strip()
        if conversation_id:
            loaded = self.services.conv_service.load(conversation_id)
            if loaded is not None:
                return loaded
        title = request.prompt.strip().replace("\n", " ")[:50] or "CLI Chat"
        return self.services.conv_service.create(title=title)

    def _configure_conversation(self, conversation: Conversation, *, provider: Provider, model: str, mode: str, work_dir: str, settings: dict[str, Any]) -> None:
        selection = ConversationSelection(
            provider_id=str(getattr(provider, "id", "") or ""),
            provider_name=str(getattr(provider, "name", "") or ""),
            api_type=str(getattr(provider, "api_type", "") or ""),
            model=str(model or ""),
            mode_slug=str(mode or "chat"),
            work_dir=str(work_dir or os.getcwd()),
            show_thinking=bool(settings.get("show_thinking", True)),
        )
        self.services.app_coordinator.apply_selection(conversation, selection)

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
