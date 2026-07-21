"""Runtime-facing context maintenance facade.

``ContextMaintenance`` owns the archive/summary maintenance algorithm.
This module owns the runtime glue around it: resolving model budgets, honoring
run policy compression switches, and normalizing thresholds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from models.contracts.config import AppConfig
from core.context.maintenance import ContextMaintenance, MaintenancePolicy, MaintenanceReport
from core.llm.token_budget import resolve_token_budget
from models.contracts.agent import RunPolicy
from models.conversation import Conversation
from models.provider import Provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeContextBudget:
    context_window: int = 0
    prompt_limit: int = 0


class ContextMaintainer:
    """Single runtime entry point for context maintenance decisions."""

    def __init__(
        self,
        *,
        client: Any = None,
        app_config: AppConfig,
        compressor_factory: Any = None,
    ) -> None:
        self.client = client
        self._app_config = app_config
        self.compressor_factory = compressor_factory
        self._maintenance_lock = asyncio.Lock()

    def auto_enabled(self, policy: RunPolicy | None = None) -> bool:
        try:
            cfg_enabled = bool(getattr(getattr(self._app_config, "context", None), "agent_auto_compress_enabled", True))
        except Exception as exc:
            logger.debug("Failed to read auto compression config: %s", exc)
            cfg_enabled = True
        if policy is not None and getattr(policy, "auto_compress_enabled", None) is False:
            return False
        return cfg_enabled

    def budget(
        self,
        conversation: Conversation,
        *,
        provider: Provider | None = None,
        policy: RunPolicy | None = None,
        context_window_limit: int = 0,
    ) -> RuntimeContextBudget:
        mode_limit = int(
            context_window_limit
            or (getattr(policy, "context_window_limit", 0) if policy is not None else 0)
            or 0
        )
        resolved = resolve_token_budget(conversation, provider=provider, mode_context_window_limit=mode_limit)
        prompt_limit = int(resolved.effective_prompt_limit or resolved.context_window or mode_limit or 0)
        return RuntimeContextBudget(context_window=int(resolved.context_window or 0), prompt_limit=prompt_limit)

    def maintenance_policy(
        self,
        *,
        keep_last_turns: int | None = None,
        token_threshold_ratio: float | None = None,
    ) -> MaintenancePolicy:
        compression_policy = getattr(getattr(self._app_config, "context", None), "compression_policy", None)
        return MaintenancePolicy(
            keep_last_turns=int(
                keep_last_turns
                if keep_last_turns is not None
                else getattr(compression_policy, "history_keep_last_turns", 3)
                or 3
            ),
            token_threshold_ratio=float(
                token_threshold_ratio
                if token_threshold_ratio is not None
                else getattr(compression_policy, "token_threshold_ratio", 0.80)
                or 0.80
            ),
        )

    async def maintain_async(
        self,
        conversation: Conversation,
        *,
        provider: Provider | None = None,
        policy: RunPolicy | None = None,
        client: Any = None,
        context_window_limit: int = 0,
        current_seq: int | None = None,
        force: bool = False,
        force_check: bool = False,
        honor_auto_enabled: bool = True,
        keep_last_turns: int | None = None,
        token_threshold_ratio: float | None = None,
        request_token_estimate: int = 0,
        conversation_token_estimate: int = 0,
        exclude_message_ids: set[str] | None = None,
        debug_trace: Any = None,
    ) -> MaintenanceReport | None:
        if honor_auto_enabled and not self.auto_enabled(policy):
            return None
        if self._maintenance_lock.locked() and not force:
            return MaintenanceReport(reason="maintenance_in_progress")

        budget = self.budget(
            conversation,
            provider=provider,
            policy=policy,
            context_window_limit=context_window_limit,
        )
        service = ContextMaintenance(
            self.maintenance_policy(
                keep_last_turns=keep_last_turns,
                token_threshold_ratio=token_threshold_ratio,
            ),
            compressor_factory=self.compressor_factory,
            debug_trace=debug_trace,
        )
        llm_client = client if client is not None else self.client
        llm_provider = provider if llm_client is not None else None
        async with self._maintenance_lock:
            report = await service.maintain_async(
                conversation,
                client=llm_client,
                provider=llm_provider,
                context_window_limit=int(context_window_limit or budget.prompt_limit or 0),
                current_seq=conversation.current_seq_id() if current_seq is None else int(current_seq or 0),
                force=bool(force),
                force_check=bool(force_check),
                request_token_estimate=int(request_token_estimate or 0),
                conversation_token_estimate=int(conversation_token_estimate or 0),
                exclude_message_ids=set(exclude_message_ids or set()),
            )
        self._record_debug_trace(debug_trace, report)
        return report

    @staticmethod
    def _record_debug_trace(debug_trace: Any, report: MaintenanceReport) -> None:
        if debug_trace is None or report is None:
            return
        data = {
            "reason": str(report.reason or ""),
            "summarized_messages": int(report.summarized_messages or 0),
            "archived_messages": int(report.archived_messages or 0),
            "summary_updated": bool(report.summary_updated),
            "archive_updates": int(report.archive_updates or 0),
            **dict(report.metrics or {}),
        }
        changed = bool(report.archived_messages or report.summary_updated or report.archive_updates)
        turn = int(getattr(debug_trace, "turn", 0) or 0)
        debug_trace.record_event(
            kind="condense",
            phase="end",
            turn=turn,
            node_id=f"condense-{turn:02d}",
            parent_id=str(getattr(debug_trace, "node_id", "") or getattr(debug_trace, "parent_id", "") or ""),
            name=str(report.reason or "condense"),
            status="completed" if changed else "skipped",
            summary=(
                f"archived={report.archived_messages}, input={data.get('input_chars', 0)}, "
                f"output={data.get('output_chars', 0)}, calls={data.get('compression_calls', 0)}"
            ),
            data=data,
        )
