from __future__ import annotations

from core.content.archive_store import SessionArchiveStore
from models.conversation import Conversation, Message
from models.contracts.session_state import WorkTrace


def _history_range(record) -> tuple[int, set[str]]:
    metadata = record.metadata if isinstance(getattr(record, "metadata", None), dict) else {}
    return (
        int(metadata.get("end_seq", 0) or 0),
        {str(item) for item in (metadata.get("message_ids") or []) if str(item)},
    )


def reconcile_after_turn_restart(
    conversation: Conversation,
    *,
    target_user_seq: int,
    state_update_seq: int,
) -> None:
    """Restore context to the latest checkpoint strictly before a restarted turn."""

    prefix_ids = {
        str(message.id)
        for message in conversation.messages
        if 0 < int(getattr(message, "seq_id", 0) or 0) < target_user_seq
    }
    store = SessionArchiveStore(conversation.work_dir, conversation.id)
    safe_history = []
    for record in store.list_records(kind="history"):
        end_seq, message_ids = _history_range(record)
        if (
            record.summary
            and 0 < end_seq < target_user_seq
            and message_ids
            and message_ids.issubset(prefix_ids)
        ):
            safe_history.append(record)
    safe_history.sort(key=lambda record: _history_range(record)[0])
    safe_history_ids = {record.id for record in safe_history}
    checkpoint = safe_history[-1] if safe_history else None

    for message in conversation.messages:
        archive_id = str(getattr(message, "archived_content_id", "") or "")
        message_seq = int(getattr(message, "seq_id", 0) or 0)
        if archive_id and not (
            0 < message_seq < target_user_seq and archive_id in safe_history_ids
        ):
            message.archived_content_id = None

    state = conversation.get_state()
    if checkpoint is None:
        state.summary = ""
        state.last_maintenance_seq = 0
    else:
        state.summary = checkpoint.summary
        state.last_maintenance_seq = _history_range(checkpoint)[0]

    archive_index = {record.id: record.to_index_record() for record in safe_history}
    for content_id, index_record in (state.archive_index or {}).items():
        record = store.read_record(str(content_id))
        if record is not None and record.kind == "history":
            continue
        seq = int(
            getattr(record, "updated_seq", 0)
            or getattr(record, "created_seq", 0)
            or getattr(index_record, "updated_seq", 0)
            or getattr(index_record, "created_seq", 0)
            or 0
        )
        if 0 < seq < target_user_seq:
            archive_index[str(content_id)] = index_record

    state.archive_index = archive_index
    state.todos = []
    state.recent_completed_todos = []
    state.memory_candidates = {}
    state.work_trace = WorkTrace()
    state.last_updated_seq = int(state_update_seq or 0)
    state.state_version = int(state.state_version or 0) + 1
    conversation.set_state(state)
