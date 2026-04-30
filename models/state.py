"""
SessionState: Centralized state management for conversations.

This module implements the "State-Driven + Event-Sourcing Lite" architecture:
- SessionState holds summary/tasks/memory as structured data (not scattered in messages)
- All state changes are tracked via seq_id for rollback/time-travel
- Tools write to state explicitly via StateManagerTool
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
class SessionDocument:
    """A session-level persistent document (plan, memory, notes, etc.).
    
    Documents survive context condensation and are always available
    in the system prompt, providing persistent memory across turns.
    """
    name: str
    content: str = ""
    abstract: str = ""
    kind: str = ""
    references: List[str] = field(default_factory=list)
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'content': self.content,
            'abstract': self.abstract,
            'kind': self.kind,
            'references': list(self.references),
            'updated_seq': self.updated_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionDocument':
        return cls(
            name=data.get('name', ''),
            content=data.get('content', ''),
            abstract=data.get('abstract', ''),
            kind=data.get('kind', ''),
            references=[str(item) for item in (data.get('references', []) or []) if str(item).strip()],
            updated_seq=data.get('updated_seq', 0),
        )


@dataclass
class SessionState:
    """
    The "brain" of a conversation - holds cognitive state separate from message history.
    
    This replaces the scattered condense_parent/summary fields with a unified state object.
    Key design decisions:
    - summary: Global rolling summary (replaces is_summary messages)
    - tasks: Structured todo list with full lifecycle (replaces markdown parsing)
    - memory: Key-value long-term facts (user preferences, important paths, etc.)
    - last_updated_seq: Tracks when state was last modified for rollback
    
    The state is:
    1. Persisted as part of Conversation JSON
    2. Injected into System Prompt for LLM context
    3. Updated via explicit tool calls (StateManagerTool)
    """
    summary: str = ""
    tasks: List[Task] = field(default_factory=list)
    memory: Dict[str, str] = field(default_factory=dict)
    documents: Dict[str, SessionDocument] = field(default_factory=dict)
    archived_summaries: List[str] = field(default_factory=list)
    last_updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'summary': self.summary,
            'tasks': [t.to_dict() for t in self.tasks],
            'memory': self.memory,
            'documents': {k: v.to_dict() for k, v in self.documents.items()},
            'archived_summaries': self.archived_summaries,
            'last_updated_seq': self.last_updated_seq
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionState':
        if not data:
            return cls()
        tasks = [Task.from_dict(t) for t in data.get('tasks', [])]
        docs_raw = data.get('documents', {})
        documents = {k: SessionDocument.from_dict(v) for k, v in docs_raw.items()} if isinstance(docs_raw, dict) else {}
        return cls(
            summary=data.get('summary', ''),
            tasks=tasks,
            memory=data.get('memory', {}),
            documents=documents,
            archived_summaries=data.get('archived_summaries', []),
            last_updated_seq=data.get('last_updated_seq', 0)
        )

    def create_snapshot(self) -> 'SessionState':
        """Create a deep copy for rollback support"""
        return copy.deepcopy(self)

    def ensure_document(self, name: str, *, default_content: str = "") -> SessionDocument:
        """Return an existing session document or create it lazily."""
        doc = self.documents.get(name)
        if doc is None:
            doc = SessionDocument(name=name, content=default_content)
            self.documents[name] = doc
        return doc

    def get_plan_document(self) -> SessionDocument:
        """Return the canonical session plan document."""
        return self.ensure_document("plan")

    def get_memory_document(self) -> SessionDocument:
        """Return the canonical long-form memory document."""
        return self.ensure_document("memory")

    def get_active_tasks(self) -> List[Task]:
        """Get non-completed/cancelled todo items."""
        return [t for t in self.tasks if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)]

    def get_active_todos(self) -> List[Task]:
        """Alias used by the new concept model: current task todo list."""
        return self.get_active_tasks()

    def find_task(self, task_id: str) -> Optional[Task]:
        """Find task by ID"""
        return next((t for t in self.tasks if t.id == task_id), None)

    def to_prompt_view(
        self,
        *,
        include_documents: bool = True,
        exclude_documents: Optional[Set[str]] = None,
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
        
        # 2. Memory section (structured facts)
        if self.memory:
            mem_lines = ["### Memory Facts"]
            for key, value in self.memory.items():
                # Truncate long values
                display_value = value[:100] + "..." if len(value) > 100 else value
                mem_lines.append(f"- **{key}**: {display_value}")
            blocks.append("\n".join(mem_lines))

        # 3. Session documents (plan/report/memory notes)
        excluded = {str(name).strip().lower() for name in (exclude_documents or set()) if str(name).strip()}
        if include_documents and self.documents:
            doc_lines = ["### Session Documents"]
            for name, doc in self.documents.items():
                if str(name).strip().lower() in excluded:
                    continue
                preview_source = doc.abstract or doc.content
                preview = (preview_source[:150] + "...") if len(preview_source) > 150 else preview_source
                refs = ", ".join(doc.references[:3]) if doc.references else ""
                line = f"\n**{name}**"
                if doc.kind:
                    line += f" [{doc.kind}]"
                line += f":\n{preview or '-'}"
                if refs:
                    line += f"\nrefs: {refs}"
                doc_lines.append(line)
            if len(doc_lines) > 1:
                blocks.append("\n".join(doc_lines))
        
        if not blocks:
            return ""
        
        header = "## SESSION STATE\n_Use `manage_state` and `manage_document` to keep todos, memory, plan and reports current._\n"
        return header + "\n\n".join(blocks)
