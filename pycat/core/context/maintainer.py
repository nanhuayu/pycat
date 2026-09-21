"""Runtime-facing context maintenance facade.

``ContextMaintenance`` owns the archive/summary maintenance algorithm.
This module owns the runtime glue around it: resolving model budgets, honoring
run policy compression switches, and normalizing thresholds.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from pycat.models.contracts.config import AppConfig
from pycat.core.context.maintenance import (
    ContextMaintenance,
    MaintenancePolicy,
    MaintenanceReport,
    TurnCapsuleReport,
)
from pycat.core.llm.token_budget import resolve_token_budget
from pycat.models.contracts.agent import RunPolicy
from pycat.models.conversation import Conversation
from pycat.models.provider import Provider

logger = logging.getLogger(__name__)


class ContextMaintainer:
    """Single runtime entry point for context maintenance decisions."""

    def __init__(
        self,
        *,
        client: Any = None,
        app_config: AppConfig,
        compression_factory: Any = None,
    ) -> None:
        self.client = client
        self._app_config = app_config
        self.compression_factory = compression_factory
        self._active_lock = threading.Lock()
        self._active_conversations: set[str] = set()

    def update_configuration(self, app_config: AppConfig) -> None:
        self._app_config = app_config

    def auto_enabled(self, policy: RunPolicy | None = None) -> bool:
        try:
            cfg_enabled = bool(getattr(getattr(self._app_config, "context", None), "agent_auto_compress_enabled", True))
        except Exception as exc:
            logger.debug("Failed to read auto compression config: %s", exc)
            cfg_enabled = True
        if policy is not None and getattr(policy, "auto_compress_enabled", None) is False:
            return False
        return cfg_enabled

    def resolve_prompt_limit(
        self,
        conversation: Conversation,
        *,
        provider: Provider | None = None,
    ) -> int:
        resolved = resolve_token_budget(conversation, provider=provider)
        return int(resolved.effective_prompt_limit or resolved.context_window or 0)

    def maintenance_policy(
        self,
        *,
        token_threshold_ratio: float | None = None,
    ) -> MaintenancePolicy:
        compression_policy = getattr(getattr(self._app_config, "context", None), "compression_policy", None)
        return MaintenancePolicy(
            recent_turn_target=3,
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
        prompt_limit: int = 0,
        current_seq: int | None = None,
        force: bool = False,
        force_check: bool = False,
        honor_auto_enabled: bool = True,
        token_threshold_ratio: float | None = None,
        request_token_estimate: int = 0,
        conversation_token_estimate: int = 0,
        exclude_message_ids: set[str] | None = None,
        recent_turn_target: int = 3,
        protect_current_turn: bool = False,
        debug_trace: Any = None,
    ) -> MaintenanceReport | None:
        if honor_auto_enabled and not self.auto_enabled(policy):
            return None
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not self._begin_maintenance(conversation_id):
            return MaintenanceReport(reason="maintenance_in_progress")
        try:
            effective_prompt_limit = int(
                prompt_limit or self.resolve_prompt_limit(conversation, provider=provider) or 0
            )
            service = ContextMaintenance(
                self.maintenance_policy(
                    token_threshold_ratio=token_threshold_ratio,
                ),
                compression_factory=self.compression_factory,
                debug_trace=debug_trace,
            )
            llm_client = client if client is not None else self.client
            llm_provider = provider if llm_client is not None else None
            report = await service.maintain_async(
                conversation,
                client=llm_client,
                provider=llm_provider,
                prompt_limit=effective_prompt_limit,
                current_seq=conversation.current_seq_id() if current_seq is None else int(current_seq or 0),
                force=bool(force),
                force_check=bool(force_check),
                request_token_estimate=int(request_token_estimate or 0),
                conversation_token_estimate=int(conversation_token_estimate or 0),
                exclude_message_ids=set(exclude_message_ids or set()),
                recent_turn_target=max(0, int(recent_turn_target or 0)),
                protect_current_turn=bool(protect_current_turn),
            )
        finally:
            self._end_maintenance(conversation_id)
        self._record_debug_trace(debug_trace, report)
        return report

    async def finalize_closed_turns_async(
        self,
        conversation: Conversation,
        *,
        provider: Provider | None = None,
        client: Any = None,
        close_current_turn: bool = False,
        terminal_status: object = "",
        terminal_reason: object = "",
        debug_trace: Any = None,
        enrich: bool = True,
        capture_metadata: dict | None = None,
        on_archive: Any = None,
    ) -> TurnCapsuleReport:
        """Persist closed-turn sidecars through the same maintenance owner."""
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not self._begin_maintenance(conversation_id):
            return TurnCapsuleReport(failed=1)
        try:
            service = ContextMaintenance(
                self.maintenance_policy(),
                compression_factory=self.compression_factory,
                debug_trace=debug_trace,
            )
            llm_client = client if client is not None else self.client
            llm_provider = provider if llm_client is not None else None
            return await service.finalize_closed_turns_async(
                conversation,
                client=llm_client,
                provider=llm_provider,
                close_current_turn=bool(close_current_turn),
                terminal_status=terminal_status,
                terminal_reason=terminal_reason,
                enrich=enrich,
                capture_metadata=capture_metadata,
                on_archive=on_archive,
            )
        finally:
            self._end_maintenance(conversation_id)

    def _begin_maintenance(self, conversation_id: str) -> bool:
        key = conversation_id or "__anonymous__"
        with self._active_lock:
            if key in self._active_conversations:
                return False
            self._active_conversations.add(key)
            return True

    def _end_maintenance(self, conversation_id: str) -> None:
        key = conversation_id or "__anonymous__"
        with self._active_lock:
            self._active_conversations.discard(key)

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
