"""Terminal projection of shared input actions and application operations."""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace

from pycat.cli.output import CliOutput
from pycat.core.app.container import AppContainer
from pycat.core.content.references import latest_turn_deliveries
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.tools.base import ApprovalDecision
from pycat.models.contracts.agent import RunRequest, RunStatus, ApplicationError


class CliExecutor:
    def __init__(self, container=None, *, data_dir=None, background_curation=False):
        self.container = container or AppContainer(data_dir=data_dir, background_curation=background_curation)
        self.services = self.container.services
        self._last_conversation_id = ""

    async def aclose(self):
        await self.container.aclose()

    async def run_once(self, request: RunRequest, output: CliOutput | None = None) -> int:
        out = output or CliOutput()
        if request.work_dir is None and not request.conversation_id:
            request = replace(request, work_dir=os.getcwd())
        handle = None
        try:
            action = await self.services.command_service.dispatch(request, source="cli",
                approval_callback=self.approval(out), questions_callback=self.questions(out))
            if action.kind != "run":
                if action.conversation:
                    self._last_conversation_id = action.conversation.id
                if action.kind == "panel":
                    out.result(await self.panel(action, work_dir=request.work_dir or ""))
                else:
                    out.final(status="completed", message=action.message,
                              conversation_id=self._last_conversation_id)
                return 0
            handle = action.handle
            async with handle:
                async for event in handle.events():
                    out.event(event)
                result = await handle.result()
            conversation = result.conversation
            self._last_conversation_id = conversation.id if conversation else request.conversation_id or ""
            deliveries = []
            if conversation:
                resolver = SessionContentResolver(self.services.content_service)
                for ref in latest_turn_deliveries(conversation):
                    value = ref.to_dict()
                    try:
                        value["path"] = str(resolver.resolve(conversation, ref))
                    except (OSError, ValueError):
                        value["path"] = ""
                    deliveries.append(value)
            out.final(status=result.status.value, message=result.final_message.content if result.final_message else "",
                error=result.error or "", conversation_id=self._last_conversation_id, run_id=handle.id,
                stop_reason=result.stop_reason.value, deliveries=deliveries)
            return 0 if result.status == RunStatus.COMPLETED else 1
        except asyncio.CancelledError:
            if handle:
                handle.cancel()
                await handle.result()
            out.final(status="cancelled", conversation_id=self._last_conversation_id,
                      run_id=handle.id if handle else "", stop_reason="cancelled")
            return 130
        except (ApplicationError, ValueError, OSError) as exc:
            out.final(status="failed", error=str(exc))
            return 2

    async def panel(self, action, *, work_dir=""):
        session = action.conversation.id if action.conversation else None
        if action.panel == "resume":
            return action.data
        if action.panel in {"model", "mode", "agents"}:
            return await self.services.workbench.execute("model.list" if action.panel == "model" else "mode.list",
                {} if action.panel == "model" else {"work_dir": work_dir})
        names = {"config": "config.read", "mcp": "mcp.list", "channels": "channels.list",
                 "context": "materials.list", "doctor": "doctor"}
        if action.panel in names:
            return await self.services.workbench.execute(names[action.panel],
                {"session": session} if action.panel == "context" else {})
        if action.panel in {"status", "permissions"} and session:
            conversation = self.services.command_service.require_session(session)
            return {"session": session, "model": conversation.model, "mode": conversation.mode,
                    "settings": conversation.settings, "active": self.services.run_service.active_runs()}
        if action.panel == "copy" and action.conversation:
            return next((m.content for m in reversed(action.conversation.messages) if m.role == "assistant"), "")
        raise ValueError(f"Use the interactive {action.panel} panel, or pycat {action.panel} --help.")

    @staticmethod
    def approval(out):
        async def ask(request):
            payload = {"version": 1, "type": "approval_request", "tool_name": request.tool_name,
                       "risk": request.risk, "message": request.message,
                       "external_path": request.external_path, "tool_call_id": request.tool_call_id}
            if out.stream_json:
                out.write_json(payload)
            else:
                out.note(f"[approval] {request.message or request.tool_name}")
            answer = await out.read_line_async("Allow? [y/N/r]: " if request.requires_path_approval else "Allow? [y/N]: ")
            answer = str(answer or "").strip().lower()
            approved = answer in {"y", "yes"} or request.requires_path_approval and answer in {"r", "run"}
            return ApprovalDecision(approved=approved,
                read_scope=("run" if answer in {"r", "run"} else "call") if approved and request.requires_path_approval else "")
        return ask

    @staticmethod
    def questions(out):
        async def ask(question):
            if out.stream_json:
                out.write_json({"version": 1, "type": "question_request", "question": question})
            out.note(str(question.get("text") or "Question"))
            options = question.get("options") or []
            for index, option in enumerate(options, 1):
                out.note(f"{index}. {option.get('label', '')}: {option.get('description', '')}")
            answer = await out.read_line_async("Answer (numbers separated by commas, or text; Enter skips): ")
            if not answer:
                return {"selected": [], "freeText": None, "skipped": True, "reason": "interaction_unavailable" if answer is None else "user_skipped"}
            values = [part.strip() for part in answer.split(",")]
            if all(value.isdigit() and 1 <= int(value) <= len(options) for value in values):
                selected = [options[int(value) - 1]["label"] for value in values]
                if not question.get("multiple") and len(selected) > 1:
                    raise ValueError("This question permits one selection.")
                return {"selected": selected, "freeText": None, "skipped": False}
            return {"selected": [], "freeText": answer, "skipped": False}
        return ask
