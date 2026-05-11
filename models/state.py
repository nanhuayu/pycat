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


class TaskStatus(str, Enum):
    """Task lifecycle states"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    """Task priority levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


RECENT_COMPLETED_TODO_LIMIT = 5


@dataclass
class Task:
    """
    A structured todo item tracked inside the current task/session scope.
    
    Attributes:
        id: Unique identifier (auto-generated short UUID)
        content: Task description
        status: Current lifecycle state
        priority: Importance level
        tags: Categorization labels
        created_seq: The seq_id when this task was created
        updated_seq: The seq_id when this task was last modified
    """
    content: str
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    tags: List[str] = field(default_factory=list)
    created_seq: int = 0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'content': self.content,
            'status': self.status.value,
            'priority': self.priority.value,
            'tags': self.tags,
            'created_seq': self.created_seq,
            'updated_seq': self.updated_seq
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        return cls(
            id=data.get('id', str(uuid.uuid4())[:8]),
            content=data.get('content', ''),
            status=TaskStatus(data.get('status', 'pending')),
            priority=TaskPriority(data.get('priority', 'medium')),
            tags=data.get('tags', []),
            created_seq=data.get('created_seq', 0),
            updated_seq=data.get('updated_seq', 0)
        )

    def update(self, current_seq: int, **kwargs):
        """Update task fields and bump updated_seq"""
        for key, value in kwargs.items():
            if value is None:
                continue
            if key == 'status':
                self.status = TaskStatus(value) if isinstance(value, str) else value
            elif key == 'priority':
                self.priority = TaskPriority(value) if isinstance(value, str) else value
            elif hasattr(self, key):
                setattr(self, key, value)
        self.updated_seq = current_seq


@dataclass
class TodoDigest:
    """Compact trace for recently completed/cancelled todos.

    Active todos stay in ``SessionState.tasks`` for the live progress UI. Terminal
    todos are compacted here so the next prompt can tell completion from absence
    and avoid recreating equivalent milestones.
    """
    content: str
    status: TaskStatus = TaskStatus.COMPLETED
    priority: TaskPriority = TaskPriority.MEDIUM
    completed_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'content': self.content,
            'status': self.status.value,
            'priority': self.priority.value,
            'completed_seq': self.completed_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TodoDigest':
        return cls(
            content=data.get('content', ''),
            status=TaskStatus(data.get('status', 'completed')),
            priority=TaskPriority(data.get('priority', 'medium')),
            completed_seq=int(data.get('completed_seq', 0) or 0),
        )


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
class SessionState:
    """
    The "brain" of a conversation - holds cognitive state separate from message history.
    
    This replaces the scattered condense_parent/summary fields with a unified state object.
    Key design decisions:
    - summary: Global rolling summary (replaces is_summary messages)
    - tasks: Active model-managed todo list for current progress
    - recent_completed_todos: Short completion trace to prevent todo amnesia
    - memory: Key-value facts (user preferences, important paths, decisions)
    - artifacts: Explicit model-managed session outputs and working notes
    - last_updated_seq: Tracks when state was last modified for rollback
    
    The state is:
    1. Persisted as part of Conversation JSON
    2. Injected into System Prompt for LLM context
    3. Updated via explicit tool calls (StateManagerTool)
    """
    summary: str = ""
    tasks: List[Task] = field(default_factory=list)
    recent_completed_todos: List[TodoDigest] = field(default_factory=list)
    memory: Dict[str, str] = field(default_factory=dict)
    artifacts: Dict[str, SessionArtifact] = field(default_factory=dict)
    archived_summaries: List[str] = field(default_factory=list)
    last_updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'summary': self.summary,
            'tasks': [t.to_dict() for t in self.tasks],
            'recent_completed_todos': [t.to_dict() for t in self.recent_completed_todos[-RECENT_COMPLETED_TODO_LIMIT:]],
            'memory': self.memory,
            'artifacts': {k: v.to_dict() for k, v in self.artifacts.items()},
            'archived_summaries': self.archived_summaries,
            'last_updated_seq': self.last_updated_seq
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionState':
        if not data:
            return cls()
        tasks = [Task.from_dict(t) for t in data.get('tasks', [])]
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
        return cls(
            summary=data.get('summary', ''),
            tasks=tasks,
            recent_completed_todos=recent_completed_todos,
            memory=data.get('memory', {}),
            artifacts=artifacts,
            archived_summaries=data.get('archived_summaries', []),
            last_updated_seq=data.get('last_updated_seq', 0)
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

    def get_active_tasks(self) -> List[Task]:
        """Get non-completed/cancelled todo items."""
        return [t for t in self.tasks if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)]

    def get_active_todos(self) -> List[Task]:
        """Alias used by the new concept model: current task todo list."""
        return self.get_active_tasks()

    def remember_completed_todo(self, task: Task, current_seq: int) -> None:
        """Compact a terminal todo into a short recent-completion trace."""
        content = str(task.content or "").strip()
        if not content:
            return
        normalized = content.casefold()
        self.recent_completed_todos = [
            item for item in self.recent_completed_todos
            if str(item.content or "").strip().casefold() != normalized
        ]
        self.recent_completed_todos.append(
            TodoDigest(
                content=content,
                status=task.status if isinstance(task.status, TaskStatus) else TaskStatus(str(task.status)),
                priority=task.priority if isinstance(task.priority, TaskPriority) else TaskPriority(str(task.priority)),
                completed_seq=current_seq,
            )
        )
        self.recent_completed_todos = self.recent_completed_todos[-RECENT_COMPLETED_TODO_LIMIT:]

    def find_task(self, task_id: str) -> Optional[Task]:
        """Find task by ID"""
        return next((t for t in self.tasks if t.id == task_id), None)

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
        active_tasks = self.get_active_todos()
        if active_tasks:
            task_lines = ["### Current Todo List"]
            for t in active_tasks:
                # Format: - [pending] (high) Task content #tag1 #tag2 [id:abc123]
                status_icon = "⏳" if t.status == TaskStatus.IN_PROGRESS else "⬜"
                priority_str = f"({t.priority.value})" if t.priority != TaskPriority.MEDIUM else ""
                tags_str = " ".join([f"#{tag}" for tag in t.tags]) if t.tags else ""
                task_lines.append(f"- {status_icon} {priority_str} {t.content} {tags_str} [id:{t.id}]")
            blocks.append("\n".join(task_lines))
        elif self.recent_completed_todos:
            recent_lines = ["### Current Todo List", "No active todos.", "Recently completed/cancelled todos:"]
            for item in self.recent_completed_todos[-3:]:
                status = item.status.value if isinstance(item.status, TaskStatus) else str(item.status)
                recent_lines.append(f"- [{status}] {item.content}")
            recent_lines.append("Do not recreate equivalent todos unless the user asks for new work or scope changes.")
            blocks.append("\n".join(recent_lines))
        
        # 2. Memory section (structured facts)
        if include_memory_facts and self.memory:
            mem_lines = ["### Memory Facts"]
            for key, value in self.memory.items():
                # Truncate long values
                display_value = value[:100] + "..." if len(value) > 100 else value
                mem_lines.append(f"- **{key}**: {display_value}")
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
        
        header = "## SESSION STATE\n_Use `manage_todo`, `manage_state`, and `manage_artifact` explicitly when the task needs structured state._\n"
        return header + "\n\n".join(blocks)
