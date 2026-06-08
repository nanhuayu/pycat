from typing import Any, List

from models.state import SessionState


class SummaryService:
    @staticmethod
    def update_summary(state: SessionState, new_summary: str, current_seq: int) -> str:
        old_summary_len = len(state.summary)
        state.summary = new_summary
        state.last_updated_seq = current_seq
        return f"Summary updated manually ({old_summary_len} -> {len(state.summary)} chars)"

    @staticmethod
    async def archive_context(
        state: SessionState,
        llm_client: Any,
        conversation: Any,
        provider: Any,
        current_seq: int,
        keep_last_n: int = 3,
    ) -> List[str]:
        feedback: List[str] = []

        if not llm_client or not conversation or not provider:
            feedback.append("Cannot archive: context missing (client/conversation/provider).")
            return feedback

        from core.context.maintenance import ContextMaintenanceService, MaintenancePolicy

        feedback.append("Archiving context...")

        try:
            report = await ContextMaintenanceService(
                MaintenancePolicy(keep_last_turns=max(1, int(keep_last_n or 3)))
            ).maintain_async(
                conversation,
                client=llm_client,
                provider=provider,
                context_window_limit=0,
                current_seq=current_seq,
                force=True,
            )
            latest_state = conversation.get_state() if hasattr(conversation, "get_state") else None
            if isinstance(latest_state, SessionState):
                state.summary = latest_state.summary
                state.todos = latest_state.todos
                state.recent_completed_todos = latest_state.recent_completed_todos
                state.memory = latest_state.memory
                state.artifacts = latest_state.artifacts
                state.archive_index = latest_state.archive_index
                state.work_trace = latest_state.work_trace
                state.archived_summaries = latest_state.archived_summaries
                state.last_updated_seq = latest_state.last_updated_seq
                state.state_version = latest_state.state_version
                state.last_maintenance_seq = latest_state.last_maintenance_seq
            if report.summary_updated or report.archived_messages or report.summarized_messages:
                state.last_updated_seq = current_seq
                feedback.append("Context archived.")
            else:
                feedback.append("Archive skipped; nothing needed maintenance.")
        except Exception as exc:
            feedback.append(f"Archive failed: {exc}")

        return feedback
