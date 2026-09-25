from __future__ import annotations

from pycat.models.contracts.content import ArchivedContentRecord
from pycat.models.contracts.session_state import (
    RECENT_COMPLETED_TODO_LIMIT,
    SessionArtifact,
    SessionState,
    TodoDigest,
    TodoItem,
    TodoStatus,
    WorkTraceStep,
)


def ensure_artifact(state: SessionState, name: str, *, default_content: str = "") -> SessionArtifact:
    artifact = state.artifacts.get(name)
    if artifact is None:
        artifact = SessionArtifact(name=name, content=default_content)
        state.artifacts[name] = artifact
    return artifact


def remember_archive(state: SessionState, record: ArchivedContentRecord) -> None:
    if not record.id:
        return
    state.archive_index[record.id] = record.to_index_record()
    state.last_updated_seq = max(state.last_updated_seq, int(record.updated_seq or record.created_seq or 0))
    state.state_version += 1


def record_work_step(
    state: SessionState,
    *,
    seq: int,
    turn: int = 0,
    kind: str,
    label: str = "",
    tool_name: str = "",
    target: str = "",
    status: str = "completed",
    summary: str = "",
    refs: list[str] | None = None,
    content_id: str = "",
    chars: int = 0,
    goal: str = "",
) -> None:
    record_trace_step(
        state.work_trace,
        WorkTraceStep(
            seq=int(seq or 0),
            turn=int(turn or 0),
            kind=str(kind or "tool"),
            label=str(label or kind or tool_name or "tool"),
            tool_name=str(tool_name or ""),
            target=str(target or ""),
            status=str(status or "completed"),
            summary=str(summary or ""),
            refs=[str(item) for item in (refs or []) if str(item).strip()],
            content_id=str(content_id or ""),
            chars=int(chars or 0),
        ),
        goal=goal,
    )
    state.last_updated_seq = max(state.last_updated_seq, int(seq or 0))
    state.state_version += 1


def record_trace_step(work_trace, step: WorkTraceStep, *, goal: str = "") -> None:
    if goal:
        work_trace.goal = str(goal).strip()[:500]
    existing = [
        item for item in work_trace.steps
        if not (item.seq == step.seq and item.tool_name == step.tool_name and item.target == step.target)
    ]
    existing.append(step)
    work_trace.steps = existing[-32:]
    work_trace.updated_seq = max(int(work_trace.updated_seq or 0), int(step.seq or 0))
    work_trace.phase = str(step.kind or work_trace.phase or "").strip()


def archive_trace_through(work_trace, seq: int) -> None:
    cutoff = int(seq or 0)
    if cutoff <= 0:
        return
    work_trace.steps = [step for step in work_trace.steps if int(step.seq or 0) > cutoff][-32:]
    if work_trace.steps:
        work_trace.updated_seq = max(int(step.seq or 0) for step in work_trace.steps)
        work_trace.phase = str(work_trace.steps[-1].kind or "").strip()
    else:
        work_trace.phase = ""


def get_active_todos(state: SessionState) -> list[TodoItem]:
    return [
        todo for todo in state.todos
        if todo.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS, TodoStatus.BLOCKED)
    ]


def remember_completed_todo(state: SessionState, todo: TodoItem, current_seq: int) -> None:
    title = str(todo.title or "").strip()
    if not title:
        return
    normalized = title.casefold()
    state.recent_completed_todos = [
        item for item in state.recent_completed_todos
        if str(item.title or "").strip().casefold() != normalized
    ]
    state.recent_completed_todos.append(
        TodoDigest(
            id=str(todo.id or ""),
            title=title,
            status=todo.status if isinstance(todo.status, TodoStatus) else TodoStatus(str(todo.status)),
            note=str(todo.note or ""),
            refs=list(todo.refs or []),
            completed_seq=current_seq,
        )
    )
    state.recent_completed_todos = state.recent_completed_todos[-RECENT_COMPLETED_TODO_LIMIT:]


def find_todo(state: SessionState, todo_id: str) -> TodoItem | None:
    return next((todo for todo in state.todos if todo.id == todo_id), None)
