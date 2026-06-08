"""
SessionState: Centralized state management for conversations.

This module implements the "State-Driven + Event-Sourcing Lite" architecture:
- SessionState holds summary/todos/memory/artifacts as structured data (not scattered in messages)
- All state changes are tracked via seq_id for rollback/time-travel
- Tools write to state explicitly via dedicated state tools
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Set
from enum import Enum
import uuid
import copy
from datetime import datetime

from core.context.archive_store import ArchivedContentRecord


class TodoStatus(str, Enum):
    """Todo lifecycle states."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class TodoPriority(str, Enum):
    """Todo priority levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


RECENT_COMPLETED_TODO_LIMIT = 5
WORK_TRACE_STEP_LIMIT = 32
MEMORY_CONTENT_LIMIT = 600
MEMORY_CATEGORIES = {"preference", "fact", "decision", "convention", "command", "gotcha"}


@dataclass
class TodoItem:
    """A structured, user-visible progress milestone."""

    title: str
    description: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: TodoStatus = TodoStatus.PENDING
    priority: TodoPriority = TodoPriority.MEDIUM
    kind: str = ""
    acceptance: str = ""
    depends_on: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    blocked_reason: str = ""
    tags: List[str] = field(default_factory=list)
    created_seq: int = 0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'status': self.status.value,
            'priority': self.priority.value,
            'kind': self.kind,
            'acceptance': self.acceptance,
            'depends_on': list(self.depends_on),
            'evidence_refs': list(self.evidence_refs),
            'blocked_reason': self.blocked_reason,
            'tags': self.tags,
            'created_seq': self.created_seq,
            'updated_seq': self.updated_seq
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TodoItem':
        status = str(data.get('status', 'pending') or 'pending')
        if status not in {item.value for item in TodoStatus}:
            status = TodoStatus.PENDING.value
        priority = str(data.get('priority', 'medium') or 'medium')
        if priority not in {item.value for item in TodoPriority}:
            priority = TodoPriority.MEDIUM.value
        return cls(
            id=data.get('id', str(uuid.uuid4())[:8]),
            title=str(data.get('title') or '').strip(),
            description=str(data.get('description') or '').strip(),
            status=TodoStatus(status),
            priority=TodoPriority(priority),
            kind=str(data.get('kind') or '').strip(),
            acceptance=str(data.get('acceptance') or '').strip(),
            depends_on=[str(item).strip() for item in (data.get('depends_on') or []) if str(item).strip()],
            evidence_refs=[str(item).strip() for item in (data.get('evidence_refs') or []) if str(item).strip()],
            blocked_reason=str(data.get('blocked_reason') or '').strip(),
            tags=[str(item).strip() for item in (data.get('tags') or []) if str(item).strip()],
            created_seq=data.get('created_seq', 0),
            updated_seq=data.get('updated_seq', 0)
        )

    def update(self, current_seq: int, **kwargs):
        """Update todo fields and bump updated_seq."""
        for key, value in kwargs.items():
            if value is None:
                continue
            if key == 'status':
                self.status = TodoStatus(value) if isinstance(value, str) else value
            elif key == 'priority':
                self.priority = TodoPriority(value) if isinstance(value, str) else value
            elif hasattr(self, key):
                setattr(self, key, value)
        self.updated_seq = current_seq

@dataclass
class TodoDigest:
    """Compact trace for recently completed/cancelled todos.

    Active todos stay in ``SessionState.todos`` for the live progress UI. Terminal
    todos are compacted here so the next prompt can tell completion from absence
    and avoid recreating equivalent milestones.
    """
    title: str
    status: TodoStatus = TodoStatus.COMPLETED
    priority: TodoPriority = TodoPriority.MEDIUM
    completed_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'title': self.title,
            'status': self.status.value,
            'priority': self.priority.value,
            'completed_seq': self.completed_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TodoDigest':
        status = str(data.get('status', 'completed') or 'completed')
        if status not in {item.value for item in TodoStatus}:
            status = TodoStatus.COMPLETED.value
        priority = str(data.get('priority', 'medium') or 'medium')
        if priority not in {item.value for item in TodoPriority}:
            priority = TodoPriority.MEDIUM.value
        return cls(
            title=str(data.get('title') or '').strip(),
            status=TodoStatus(status),
            priority=TodoPriority(priority),
            completed_seq=int(data.get('completed_seq', 0) or 0),
        )


@dataclass
class MemoryRecord:
    """One explicit durable memory fact.

    Memory is not an archive index and not an execution trace. It stores short,
    reusable facts or preferences that the model/user deliberately chose to keep.
    """

    key: str
    content: str
    scope: str = "session"
    category: str = "fact"
    evidence_refs: List[str] = field(default_factory=list)
    confidence: float = 1.0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key': self.key,
            'content': self.content,
            'scope': self.scope,
            'category': self.category,
            'evidence_refs': list(self.evidence_refs),
            'confidence': float(self.confidence or 0.0),
            'updated_seq': int(self.updated_seq or 0),
        }

    @classmethod
    def from_dict(cls, key: str, data: Any) -> 'MemoryRecord':
        if isinstance(data, dict):
            category = str(data.get('category') or 'fact').strip().lower()
            if category not in MEMORY_CATEGORIES:
                category = 'fact'
            return cls(
                key=str(data.get('key') or key or '').strip(),
                content=str(data.get('content') or data.get('value') or '').strip()[:MEMORY_CONTENT_LIMIT],
                scope=str(data.get('scope') or 'session').strip().lower() or 'session',
                category=category,
                evidence_refs=[str(item).strip() for item in (data.get('evidence_refs') or []) if str(item).strip()],
                confidence=float(data.get('confidence') or 1.0),
                updated_seq=int(data.get('updated_seq', 0) or 0),
            )
        return cls(key=str(key or '').strip(), content=str(data or '').strip()[:MEMORY_CONTENT_LIMIT])


@dataclass
class SessionArtifact:
    """A model-managed session artifact such as a plan, report, note, or reference.

    Artifacts are not memory and are not project instructions. Prompt assembly
    injects only their index/abstract by default; tools can read full content
    when needed.
    """
    name: str
    content: str = ""
    abstract: str = ""
    kind: str = ""
    status: str = "draft"
    references: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    content_path: str = ""
    content_digest: str = ""
    content_chars: int = 0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'content': self.content,
            'abstract': self.abstract,
            'kind': self.kind,
            'status': self.status,
            'references': list(self.references),
            'related': list(self.related),
            'frontmatter': dict(self.frontmatter),
            'content_path': self.content_path,
            'content_digest': self.content_digest,
            'content_chars': self.content_chars,
            'updated_seq': self.updated_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionArtifact':
        return cls(
            name=data.get('name', ''),
            content=data.get('content', ''),
            abstract=data.get('abstract', ''),
            kind=data.get('kind', ''),
            status=data.get('status', 'draft'),
            references=[str(item) for item in (data.get('references', []) or []) if str(item).strip()],
            related=[str(item) for item in (data.get('related', []) or []) if str(item).strip()],
            frontmatter=dict(data.get('frontmatter', {}) or {}) if isinstance(data.get('frontmatter', {}), dict) else {},
            content_path=data.get('content_path', ''),
            content_digest=data.get('content_digest', ''),
            content_chars=int(data.get('content_chars', 0) or 0),
            updated_seq=data.get('updated_seq', 0),
        )


@dataclass
class WorkTraceStep:
    """Runtime-maintained summary of one meaningful execution step.

    Work trace is not memory and is not written by the model. It gives the
    next prompt a compact route through recent tool work without replaying raw
    tool results.
    """

    seq: int = 0
    turn: int = 0
    kind: str = "tool"
    label: str = ""
    tool_name: str = ""
    target: str = ""
    status: str = "completed"
    summary: str = ""
    refs: List[str] = field(default_factory=list)
    content_id: str = ""
    chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'seq': int(self.seq or 0),
            'turn': int(self.turn or 0),
            'kind': self.kind,
            'label': self.label,
            'tool_name': self.tool_name,
            'target': self.target,
            'status': self.status,
            'summary': self.summary,
            'refs': list(self.refs),
            'content_id': self.content_id,
            'chars': int(self.chars or 0),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkTraceStep':
        payload = data if isinstance(data, dict) else {}
        return cls(
            seq=int(payload.get('seq', 0) or 0),
            turn=int(payload.get('turn', 0) or 0),
            kind=str(payload.get('kind') or 'tool'),
            label=str(payload.get('label') or ''),
            tool_name=str(payload.get('tool_name') or ''),
            target=str(payload.get('target') or ''),
            status=str(payload.get('status') or 'completed'),
            summary=str(payload.get('summary') or ''),
            refs=[str(item) for item in (payload.get('refs') or []) if str(item).strip()],
            content_id=str(payload.get('content_id') or ''),
            chars=int(payload.get('chars', 0) or 0),
        )


@dataclass
class WorkTrace:
    """Short-lived runtime route for the current work.

    Summary is historical compression; memory is durable facts. WorkTrace is
    the live "how we got here" route assembled from tool events.
    """

    goal: str = ""
    phase: str = ""
    steps: List[WorkTraceStep] = field(default_factory=list)
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'goal': self.goal,
            'phase': self.phase,
            'steps': [step.to_dict() for step in self.steps[-WORK_TRACE_STEP_LIMIT:]],
            'updated_seq': int(self.updated_seq or 0),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkTrace':
        payload = data if isinstance(data, dict) else {}
        return cls(
            goal=str(payload.get('goal') or ''),
            phase=str(payload.get('phase') or ''),
            steps=[
                WorkTraceStep.from_dict(item)
                for item in (payload.get('steps') or [])
                if isinstance(item, dict)
            ][-WORK_TRACE_STEP_LIMIT:],
            updated_seq=int(payload.get('updated_seq', 0) or 0),
        )

    def record_step(self, step: WorkTraceStep, *, goal: str = "") -> None:
        if goal:
            self.goal = str(goal).strip()[:500]
        existing = [
            item for item in self.steps
            if not (item.seq == step.seq and item.tool_name == step.tool_name and item.target == step.target)
        ]
        existing.append(step)
        self.steps = existing[-WORK_TRACE_STEP_LIMIT:]
        self.updated_seq = max(int(self.updated_seq or 0), int(step.seq or 0))
        self.phase = str(step.kind or self.phase or "").strip()

    def archive_through(self, seq: int) -> None:
        cutoff = int(seq or 0)
        if cutoff <= 0:
            return
        self.steps = [step for step in self.steps if int(step.seq or 0) > cutoff][-WORK_TRACE_STEP_LIMIT:]
        if self.steps:
            self.updated_seq = max(int(step.seq or 0) for step in self.steps)
            self.phase = str(self.steps[-1].kind or "").strip()
        else:
            self.phase = ""

    def compact_route(self, *, max_steps: int = 12, through_seq: int = 0) -> str:
        labels: List[str] = []
        steps = self.steps
        cutoff = int(through_seq or 0)
        if cutoff > 0:
            steps = [step for step in steps if int(step.seq or 0) <= cutoff]
        for step in steps[-max_steps:]:
            label = step.label or step.kind or step.tool_name or "tool"
            target = step.target or step.content_id
            text = label
            if target:
                text += f"({target})"
            if not labels or labels[-1] != text:
                labels.append(text)
        return " -> ".join(labels)

    def evidence_refs(self, *, limit: int = 6, through_seq: int = 0) -> List[str]:
        refs: List[str] = []
        seen: Set[str] = set()
        steps = self.steps
        cutoff = int(through_seq or 0)
        if cutoff > 0:
            steps = [step for step in steps if int(step.seq or 0) <= cutoff]
        for step in reversed(steps):
            candidates = list(step.refs)
            if step.content_id:
                candidates.append(step.content_id)
            for ref in candidates:
                clean = str(ref or "").strip()
                if not clean or clean in seen:
                    continue
                seen.add(clean)
                refs.append(clean)
                if len(refs) >= limit:
                    return list(reversed(refs))
        return list(reversed(refs))

    def to_prompt_view(self, *, goal: str = "", max_chars: int = 1800) -> str:
        if not self.steps:
            return ""
        display_goal = (str(goal or "").strip() or self.goal).strip()
        lines = ["<work_trace>"]
        if display_goal:
            lines.append(f"goal: {display_goal[:500]}")
        route = self.compact_route(max_steps=12)
        if route:
            lines.append(f"route: {route}")
        latest = self.steps[-1]
        current = latest.summary or latest.label or latest.tool_name
        if current:
            lines.append(f"current: {current[:360]}")
        refs = self.evidence_refs(limit=6)
        if refs:
            lines.append("evidence: " + ", ".join(refs))
        lines.append("rules: Work trace is runtime-maintained progress, not memory or a report. Use artifacts for durable deliverables.")
        lines.append("</work_trace>")
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."


@dataclass
class SessionState:
    """
    The "brain" of a conversation - holds cognitive state separate from message history.
    
    This replaces the scattered condense_parent/summary fields with a unified state object.
    Key design decisions:
    - summary: Global rolling summary (replaces is_summary messages)
    - todos: Active model-managed todo list for current progress
    - recent_completed_todos: Short completion trace to prevent todo amnesia
    - memory: Key-value facts (user preferences, important paths, decisions)
    - artifacts: Explicit model-managed session outputs and working notes
    - work_trace: Runtime-maintained route through current tool work
    - last_updated_seq: Tracks when state was last modified for rollback
    
    The state is:
    1. Persisted as part of Conversation JSON
    2. Injected into System Prompt for LLM context
    3. Updated via explicit tool calls (StateManagerTool)
    """
    summary: str = ""
    todos: List[TodoItem] = field(default_factory=list)
    recent_completed_todos: List[TodoDigest] = field(default_factory=list)
    memory: Dict[str, MemoryRecord] = field(default_factory=dict)
    artifacts: Dict[str, SessionArtifact] = field(default_factory=dict)
    archive_index: Dict[str, ArchivedContentRecord] = field(default_factory=dict)
    work_trace: WorkTrace = field(default_factory=WorkTrace)
    archived_summaries: List[str] = field(default_factory=list)
    last_updated_seq: int = 0
    state_version: int = 0
    last_maintenance_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'summary': self.summary,
            'todos': [t.to_dict() for t in self.todos],
            'recent_completed_todos': [t.to_dict() for t in self.recent_completed_todos[-RECENT_COMPLETED_TODO_LIMIT:]],
            'memory': {k: v.to_dict() if isinstance(v, MemoryRecord) else MemoryRecord.from_dict(k, v).to_dict() for k, v in self.memory.items()},
            'artifacts': {k: v.to_dict() for k, v in self.artifacts.items()},
            'archive_index': {k: v.to_dict() for k, v in self.archive_index.items()},
            'work_trace': self.work_trace.to_dict(),
            'archived_summaries': self.archived_summaries,
            'last_updated_seq': self.last_updated_seq,
            'state_version': self.state_version,
            'last_maintenance_seq': self.last_maintenance_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionState':
        if not data:
            return cls()
        todos = [
            TodoItem.from_dict(t)
            for t in (data.get('todos') or [])
            if isinstance(t, dict)
        ]
        recent_completed_todos = [
            TodoDigest.from_dict(t)
            for t in data.get('recent_completed_todos', [])
            if isinstance(t, dict)
        ][-RECENT_COMPLETED_TODO_LIMIT:]
        artifacts_raw = data.get('artifacts', {})
        artifacts = {
            k: SessionArtifact.from_dict(v)
            for k, v in artifacts_raw.items()
        } if isinstance(artifacts_raw, dict) else {}
        memory_raw = data.get('memory', {})
        memory = {
            str(k): MemoryRecord.from_dict(str(k), v)
            for k, v in memory_raw.items()
        } if isinstance(memory_raw, dict) else {}
        archive_raw = data.get('archive_index', {})
        archive_index = {
            k: ArchivedContentRecord.from_dict(v)
            for k, v in archive_raw.items()
            if isinstance(v, dict)
        } if isinstance(archive_raw, dict) else {}
        work_trace = WorkTrace.from_dict(data.get('work_trace', {}))
        return cls(
            summary=data.get('summary', ''),
            todos=todos,
            recent_completed_todos=recent_completed_todos,
            memory=memory,
            artifacts=artifacts,
            archive_index=archive_index,
            work_trace=work_trace,
            archived_summaries=data.get('archived_summaries', []),
            last_updated_seq=data.get('last_updated_seq', 0),
            state_version=int(data.get('state_version', 0) or 0),
            last_maintenance_seq=int(data.get('last_maintenance_seq', 0) or 0),
        )

    def create_snapshot(self) -> 'SessionState':
        """Create a deep copy for rollback support"""
        return copy.deepcopy(self)

    def ensure_artifact(self, name: str, *, default_content: str = "") -> SessionArtifact:
        """Return an existing session artifact or create it lazily."""
        artifact = self.artifacts.get(name)
        if artifact is None:
            artifact = SessionArtifact(name=name, content=default_content)
            self.artifacts[name] = artifact
        return artifact

    def remember_archive(self, record: ArchivedContentRecord) -> None:
        """Register an archived content record in the session index."""
        if not record.id:
            return
        self.archive_index[record.id] = record
        self.last_updated_seq = max(self.last_updated_seq, int(record.updated_seq or record.created_seq or 0))
        self.state_version += 1

    def record_work_step(
        self,
        *,
        seq: int,
        turn: int = 0,
        kind: str,
        label: str = "",
        tool_name: str = "",
        target: str = "",
        status: str = "completed",
        summary: str = "",
        refs: Optional[List[str]] = None,
        content_id: str = "",
        chars: int = 0,
        goal: str = "",
    ) -> None:
        self.work_trace.record_step(
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
        self.last_updated_seq = max(self.last_updated_seq, int(seq or 0))
        self.state_version += 1

    def get_active_todos(self) -> List[TodoItem]:
        """Get non-terminal current todo items."""
        return [t for t in self.todos if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS, TodoStatus.BLOCKED)]

    def remember_completed_todo(self, todo: TodoItem, current_seq: int) -> None:
        """Compact a terminal todo into a short recent-completion trace."""
        title = str(todo.title or "").strip()
        if not title:
            return
        normalized = title.casefold()
        self.recent_completed_todos = [
            item for item in self.recent_completed_todos
            if str(item.title or "").strip().casefold() != normalized
        ]
        self.recent_completed_todos.append(
            TodoDigest(
                title=title,
                status=todo.status if isinstance(todo.status, TodoStatus) else TodoStatus(str(todo.status)),
                priority=todo.priority if isinstance(todo.priority, TodoPriority) else TodoPriority(str(todo.priority)),
                completed_seq=current_seq,
            )
        )
        self.recent_completed_todos = self.recent_completed_todos[-RECENT_COMPLETED_TODO_LIMIT:]

    def find_todo(self, todo_id: str) -> Optional[TodoItem]:
        """Find todo by ID."""
        return next((t for t in self.todos if t.id == todo_id), None)

    def to_prompt_view(
        self,
        *,
        include_artifacts: bool = True,
        include_memory_facts: bool = True,
        exclude_artifacts: Optional[Set[str]] = None,
    ) -> str:
        """
        Render state as Markdown for System Prompt injection.
        
        This provides the LLM with current cognitive context without
        including full message history.
        """
        blocks = []
        
        # NOTE: Summary is NOT rendered here. It is assembled separately
        # as a first-class context section alongside environment metadata
        # and recent full history.
        
        # 1. Current todo section
        active_todos = self.get_active_todos()
        if active_todos:
            task_lines = ["### Current Todo List"]
            for t in active_todos:
                # Format: - [pending] (high) Todo title #tag1 #tag2 [id:abc123]
                status = t.status.value if isinstance(t.status, TodoStatus) else str(t.status)
                priority_str = f"({t.priority.value})" if t.priority != TodoPriority.MEDIUM else ""
                tags_str = " ".join([f"#{tag}" for tag in t.tags]) if t.tags else ""
                description = f" - {t.description}" if t.description else ""
                acceptance = f" acceptance={t.acceptance}" if t.acceptance else ""
                blocked = f" blocked_reason={t.blocked_reason}" if t.blocked_reason else ""
                task_lines.append(f"- [{status}] {priority_str} {t.title}{description}{acceptance}{blocked} {tags_str} [id:{t.id}]")
            blocks.append("\n".join(task_lines))
        elif self.recent_completed_todos:
            recent_lines = ["### Current Todo List", "No active todos.", "Recently completed/cancelled todos:"]
            for item in self.recent_completed_todos[-3:]:
                status = item.status.value if isinstance(item.status, TodoStatus) else str(item.status)
                recent_lines.append(f"- [{status}] {item.title}")
            recent_lines.append("Do not recreate equivalent todos unless the user asks for new work or scope changes.")
            blocks.append("\n".join(recent_lines))
        
        # 2. Memory section (structured facts)
        if include_memory_facts and self.memory:
            mem_lines = ["### Memory Facts"]
            for key, record in self.memory.items():
                item = record if isinstance(record, MemoryRecord) else MemoryRecord.from_dict(str(key), record)
                # Truncate long values
                display_value = item.content[:100] + "..." if len(item.content) > 100 else item.content
                mem_lines.append(f"- **{key}** [{item.category}]: {display_value}")
            blocks.append("\n".join(mem_lines))

        # 3. Session artifacts (plan/report/notes/references)
        excluded = {str(name).strip().lower() for name in (exclude_artifacts or set()) if str(name).strip()}
        if include_artifacts and self.artifacts:
            artifact_lines = ["### Session Artifacts"]
            for name, doc in self.artifacts.items():
                if str(name).strip().lower() in excluded:
                    continue
                preview_source = doc.abstract or doc.content
                preview = (preview_source[:150] + "...") if len(preview_source) > 150 else preview_source
                refs = ", ".join(doc.references[:3]) if doc.references else ""
                line = f"\n**{name}**"
                if doc.kind:
                    line += f" [{doc.kind}]"
                if doc.status:
                    line += f" ({doc.status})"
                line += f":\n{preview or '-'}"
                if refs:
                    line += f"\nrefs: {refs}"
                if doc.related:
                    line += f"\nrelated: {', '.join(doc.related[:3])}"
                artifact_lines.append(line)
            if len(artifact_lines) > 1:
                blocks.append("\n".join(artifact_lines))
        
        if not blocks:
            return ""
        
        header = "## SESSION STATE\n_Use `state__todo`, `state__artifact`, and `state__memory` explicitly when the task needs structured state._\n"
        return header + "\n\n".join(blocks)
