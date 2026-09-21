from __future__ import annotations

from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.context.history import build_turn_blocks, is_real_user_message, turn_fingerprint
from pycat.models.conversation import Conversation, Message
from pycat.models.contracts.session_state import WorkTrace


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
    store = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None))
    safe_history = []
    for record in store.list_records(kind="history"):
        end_seq, message_ids = _history_range(record)
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        if (
            metadata.get("scope") == "checkpoint"
            and record.summary
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

    archive_index = {}
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
    state.work_trace = WorkTrace()
    state.last_updated_seq = int(state_update_seq or 0)
    state.state_version = int(state.state_version or 0) + 1
    conversation.set_state(state)

    for block in build_turn_blocks(conversation.messages):
        if not block or not is_real_user_message(block[0]):
            continue
        user = block[0]
        ref = (user.metadata or {}).get("turn_capsule_ref") if isinstance(user.metadata, dict) else None
        if not isinstance(ref, dict):
            continue
        record = store.read_record(str(ref.get("content_id") or ""), kind="history")
        metadata = record.metadata if record is not None and isinstance(record.metadata, dict) else {}
        if (
            int(getattr(user, "seq_id", 0) or 0) >= target_user_seq
            or metadata.get("scope") != "turn_capsule"
            or str(ref.get("fingerprint") or "") != turn_fingerprint(block)
            or metadata.get("fingerprint") != ref.get("fingerprint")
        ):
            user.metadata = dict(user.metadata or {})
            user.metadata.pop("turn_capsule_ref", None)
