
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any, TYPE_CHECKING
import uuid
import json
import hashlib

if TYPE_CHECKING:
    from models.contracts.session_state import SessionState


logger = logging.getLogger(__name__)


def tool_call_name(tool_call: Dict[str, Any] | None) -> str:
    data = tool_call or {}
    func = data.get('function', {}) if isinstance(data.get('function'), dict) else {}
    return str(func.get('name') or data.get('name') or data.get('tool_name') or '').strip()


def is_subtask_tool_call(tool_call: Dict[str, Any] | None) -> bool:
    name = tool_call_name(tool_call)
    return name == 'agent__run' or name.startswith('subagent__') or name.startswith('capability__')


def normalize_tool_result(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        result = dict(value)
        result_type = str(result.get('type') or '').strip()
        if not result_type:
            result['type'] = 'subtask_run' if isinstance(result.get('run'), dict) else 'tool_result'
        elif result_type == 'simple':
            result['type'] = 'tool_result'
        result.setdefault('content', '')
        result.setdefault('summary', '')
        metadata = result.get('metadata')
        result['metadata'] = dict(metadata) if isinstance(metadata, dict) else {}
        if isinstance(result.get('run'), dict):
            result['type'] = 'subtask_run'
            result['run'] = normalize_subtask_run(result.get('run'))
        return result
    return {
        'type': 'tool_result',
        'content': '' if value is None else str(value),
        'summary': '',
        'metadata': {},
    }


def normalize_subtask_run(value: Any) -> Dict[str, Any]:
    run = dict(value) if isinstance(value, dict) else {}
    messages: List[Dict[str, Any]] = []
    for item in run.get('messages') or []:
        if isinstance(item, Message):
            payload = item.to_dict()
        elif isinstance(item, dict):
            payload = dict(item)
        else:
            continue
        payload = normalize_message_payload(payload)
        if payload.get('role') == 'tool' and payload.get('tool_call_id'):
            tool_call_id = str(payload.get('tool_call_id') or '')
            for previous in reversed(messages):
                if previous.get('role') != 'assistant':
                    continue
                for tool_call in previous.get('tool_calls') or []:
                    if str(tool_call.get('id') or '') != tool_call_id:
                        continue
                    result_metadata = dict(payload.get('metadata') or {})
                    set_tool_call_result(
                        tool_call,
                        {
                            'type': 'tool_result',
                            'content': str(payload.get('content') or ''),
                            'summary': str(payload.get('content') or '')[:220],
                            'metadata': result_metadata,
                        },
                    )
                    break
                else:
                    continue
                break
            continue
        messages.append(payload)
    run['messages'] = messages
    metadata = run.get('metadata')
    run['metadata'] = dict(metadata) if isinstance(metadata, dict) else {}
    return run


def normalize_tool_call(tool_call: Any) -> Dict[str, Any]:
    original = tool_call if isinstance(tool_call, dict) else {}
    tc = dict(original)
    if 'result' in original:
        result = normalize_tool_result(original.get('result'))
        if is_subtask_tool_call(tc) and not isinstance(result.get('run'), dict):
            result['type'] = 'subtask_run'
        tc['result'] = result
    return tc


def normalize_tool_calls(tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tool_calls, list):
        return None
    normalized = [normalize_tool_call(tc) for tc in tool_calls if isinstance(tc, dict)]
    return normalized or None


def normalize_message_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    if 'tool_calls' in data:
        data['tool_calls'] = normalize_tool_calls(data.get('tool_calls'))
    metadata = data.get('metadata')
    if isinstance(metadata, dict):
        clean_metadata = dict(metadata)
        clean_metadata.pop('subtasks', None)
        clean_metadata.pop('subtasks_by_call', None)
        data['metadata'] = clean_metadata
    return data


def get_tool_call_result(tool_call: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(tool_call, dict):
        return normalize_tool_result('')
    result = normalize_tool_result(tool_call.get('result'))
    tool_call['result'] = result
    return result


def set_tool_call_result(tool_call: Dict[str, Any], result_payload: Any) -> Dict[str, Any]:
    result = normalize_tool_result(result_payload)
    tool_call['result'] = result
    return result


def is_state_checkpoint_snapshot(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("_snapshot_kind") or "") == "checkpoint"


def state_checkpoint_from_dict(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(state or {})
    archive_index = payload.get("archive_index") if isinstance(payload.get("archive_index"), dict) else {}
    archive_digest = hashlib.sha1(
        json.dumps(
            [
                {
                    "id": str((record or {}).get("id") or key),
                    "digest": str((record or {}).get("digest") or ""),
                    "updated_seq": int((record or {}).get("updated_seq", 0) or 0),
                }
                for key, record in archive_index.items()
                if isinstance(record, dict)
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    work_trace = payload.get("work_trace") if isinstance(payload.get("work_trace"), dict) else {}
    return {
        "_snapshot_kind": "checkpoint",
        "state_version": int(payload.get("state_version", 0) or 0),
        "last_updated_seq": int(payload.get("last_updated_seq", 0) or 0),
        "last_maintenance_seq": int(payload.get("last_maintenance_seq", 0) or 0),
        "summary_digest": hashlib.sha1(str(payload.get("summary") or "").encode("utf-8", errors="replace")).hexdigest()[:16],
        "archive_count": len(archive_index),
        "archive_digest": archive_digest,
        "work_trace_updated_seq": int(work_trace.get("updated_seq", 0) or 0),
    }


@dataclass
class Message:
    """Represents a single message in a conversation"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "user"  # "user", "assistant", "system"
    content: str = ""
    images: List[str] = field(default_factory=list)  # Base64 or file paths
    tool_calls: Optional[List[Dict[str, Any]]] = None  # [{id, type, function: {name, arguments}}]
    tool_call_id: Optional[str] = None  # Provider/API boundary only; not persisted in Conversation.messages
    thinking: Optional[str] = None
    tokens: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    response_time_ms: Optional[int] = None  # Response time in milliseconds
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # === Event Sourcing: Global sequence ID for time-travel/rollback ===
    seq_id: int = 0  # Assigned when Conversation.add_message() persists the message.
    
    # === State Snapshot (for rollback) ===
    # Attached at key points (after tool execution, assistant response complete)
    # When rolling back, restore state from the last message with a snapshot
    state_snapshot: Optional[Dict[str, Any]] = None  # Serialized SessionState
    
    # ID of the archived content record that contains this message's exact prior context.
    archived_content_id: Optional[str] = None
    
    # Per-message condensation (Agent Mode optimization)
    summary: Optional[str] = None # Concise summary of this message (for token saving in future turns)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        metadata = dict(self.metadata or {})
        metadata.pop('subtasks', None)
        metadata.pop('subtasks_by_call', None)
        result = {
            'id': self.id,
            'role': self.role,
            'content': self.content,
            'images': self.images,
            'tool_calls': normalize_tool_calls(self.tool_calls),
            'tool_call_id': self.tool_call_id,
            'thinking': self.thinking,
            'tokens': self.tokens,
            'created_at': self.created_at.isoformat(),
            'response_time_ms': self.response_time_ms,
            'metadata': metadata,
            'seq_id': self.seq_id,
            'archived_content_id': self.archived_content_id,
            'summary': self.summary,
        }
        # Only serialize state_snapshot if present (to save space)
        if self.state_snapshot:
            result['state_snapshot'] = self.state_snapshot
        return result

    @staticmethod
    def _normalize_content_and_images(
        raw_content: Any,
        existing_images: Any,
        existing_metadata: Any
    ) -> tuple[str, List[str], Dict[str, Any]]:
        images: List[str] = []
        if isinstance(existing_images, list):
            images.extend([i for i in existing_images if isinstance(i, str) and i])
        elif isinstance(existing_images, str) and existing_images:
            images.append(existing_images)

        metadata: Dict[str, Any] = {}
        if isinstance(existing_metadata, dict):
            metadata.update(existing_metadata)

        # OpenAI/多模态格式：content=[{"type":"text","text":"..."},{"type":"image_url","image_url":{"url":"data:..."}}]
        if isinstance(raw_content, list):
            text_parts: List[str] = []
            for part in raw_content:
                if isinstance(part, str):
                    if part:
                        text_parts.append(part)
                    continue

                if not isinstance(part, dict):
                    continue

                part_type = part.get('type')
                if part_type == 'text':
                    text = part.get('text')
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                elif part_type == 'image_url':
                    image_url = part.get('image_url', {})
                    url = None
                    if isinstance(image_url, dict):
                        url = image_url.get('url')
                    if isinstance(url, str) and url:
                        images.append(url)

            # 保留原始结构，方便将来再导出为 payload
            metadata.setdefault('raw_content', raw_content)
            return ('\n'.join(text_parts)).strip(), images, metadata

        # ChatGPT 导出格式：content={"parts":["..."]}
        if isinstance(raw_content, dict):
            parts = raw_content.get('parts')
            if isinstance(parts, list):
                text = '\n'.join(str(p) for p in parts if isinstance(p, (str, int, float)))
                metadata.setdefault('raw_content', raw_content)
                return text.strip(), images, metadata

        # 兜底：保证 content 为字符串
        if raw_content is None:
            return '', images, metadata
        if isinstance(raw_content, str):
            return raw_content, images, metadata
        return str(raw_content), images, metadata

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Message':
        """Create from dictionary"""
        created_at = data.get('created_at')
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now()

        content, images, metadata = cls._normalize_content_and_images(
            data.get('content', ''),
            data.get('images', []),
            data.get('metadata', {})
        )
        metadata.pop('subtasks', None)
        metadata.pop('subtasks_by_call', None)
            
        return cls(
            id=data.get('id', str(uuid.uuid4())),
            role=data.get('role', 'user'),
            content=content,
            images=images,
            tool_calls=normalize_tool_calls(data.get('tool_calls')),
            tool_call_id=data.get('tool_call_id'),
            thinking=data.get('thinking'),
            tokens=data.get('tokens'),
            created_at=created_at,
            response_time_ms=data.get('response_time_ms'),
            metadata=metadata,
            seq_id=data.get('seq_id', 0),
            state_snapshot=data.get('state_snapshot'),
            archived_content_id=data.get('archived_content_id'),
            summary=data.get('summary'),
        )


@dataclass
class Conversation:
    """Represents a chat conversation"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "New Chat"
    messages: List[Message] = field(default_factory=list)
    provider_id: str = ""
    provider_name: str = ""
    model: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    total_tokens: int = 0
    work_dir: str = ""  # Associated workspace directory
    settings: Dict[str, Any] = field(default_factory=dict)
    mode: str = "chat" # "chat" or "agent"
    llm_config: Dict[str, Any] = field(default_factory=dict)
    
    # === SessionState: Centralized state management ===
    # Lazy-loaded to avoid circular import; use get_state() method
    _state_dict: Dict[str, Any] = field(default_factory=dict)
    
    # === Sequence counter for time-travel/rollback ===
    _seq_counter: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.settings, dict):
            self.settings = {}
        self.settings.pop("secondary_model_ref", None)
        self.settings.pop("fallback_model_ref", None)
        self._normalize_message_sequences()
        self._sync_llm_config_projection()

    def _normalize_message_sequences(self) -> None:
        try:
            stored_seq_counter = max(0, int(self._seq_counter or 0))
        except Exception:
            stored_seq_counter = 0
        last_message_seq = 0
        for message in self.messages:
            try:
                incoming_seq = int(message.seq_id or 0)
            except Exception:
                incoming_seq = 0
            if incoming_seq <= last_message_seq:
                incoming_seq = last_message_seq + 1
                message.seq_id = incoming_seq
            last_message_seq = incoming_seq
        self._seq_counter = max(stored_seq_counter, last_message_seq)

    def _sync_llm_config_projection(self) -> None:
        try:
            from models.llm_config import LLMConfig

            cfg = LLMConfig.from_conversation(self)
            cfg.apply_to_conversation(self)
        except Exception as exc:
            logger.debug("Failed to synchronize llm_config projection: %s", exc)
            if not isinstance(self.llm_config, dict):
                self.llm_config = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'id': self.id,
            'title': self.title,
            'messages': [msg.to_dict() for msg in self.messages],
            'provider_id': self.provider_id,
            'provider_name': self.provider_name,
            'model': self.model,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'total_tokens': self.total_tokens,
            'work_dir': self.work_dir,
            'settings': self.settings,
            'mode': self.mode,
            'llm_config': self.get_llm_config().to_dict(),
            'state': self._state_dict,
            '_seq_counter': self._seq_counter
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Conversation':
        """Create from current dictionary schema."""
        messages = [Message.from_dict(m) for m in data.get('messages', [])]
        
        created_at = data.get('created_at')
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now()
            
        updated_at = data.get('updated_at')
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        elif updated_at is None:
            updated_at = datetime.now()
        
        state_dict = data.get('state', {})
        
        try:
            seq_counter = max(0, int(data.get('_seq_counter', 0) or 0))
        except Exception:
            seq_counter = 0
        
        settings = dict(data.get('settings') or {})
        legacy_instructions = str(
            settings.pop('system_prompt', '')
            or settings.pop('custom_instructions', '')
            or settings.pop('system_prompt_override', '')
            or ''
        ).strip()
        if legacy_instructions and not str(settings.get('session_instructions') or '').strip():
            settings['session_instructions'] = legacy_instructions

        llm_config = dict(data.get('llm_config') or {})
        legacy_override = str(llm_config.pop('system_prompt_override', '') or '').strip()
        if legacy_override and not str(settings.get('session_instructions') or '').strip():
            settings['session_instructions'] = legacy_override

        conv = cls(
            id=data.get('id', str(uuid.uuid4())),
            title=data.get('title', 'Imported Chat'),
            messages=messages,
            provider_id=data.get('provider_id', ''),
            provider_name=data.get('provider_name', ''),
            model=data.get('model', ''),
            created_at=created_at,
            updated_at=updated_at,
            total_tokens=data.get('total_tokens', 0),
            work_dir=data.get('work_dir', ''),
            settings=settings,
            mode=data.get('mode', 'chat'),
            llm_config=llm_config,
            _state_dict=state_dict,
            _seq_counter=seq_counter
        )
        return conv

    def to_json(self) -> str:
        """Serialize to JSON string"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> 'Conversation':
        """Create from JSON string"""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def add_message(self, message: Message):
        """Add a visible message to the conversation.

        Persisted conversation transcripts contain only system/user/assistant
        messages. Tool results must be written through attach_tool_result().
        """
        if message.role == 'tool':
            logger.debug("Ignoring transient tool message; use attach_tool_result() instead")
            return

        incoming_seq = int(getattr(message, 'seq_id', 0) or 0)
        if incoming_seq <= self._seq_counter:
            message.seq_id = self.next_seq_id()
        else:
            self._seq_counter = incoming_seq

        self.messages.append(message)
        if message.tokens:
            self.total_tokens += message.tokens
        self.updated_at = datetime.now()

    def attach_tool_result(
        self,
        tool_call_id: str | None,
        result_payload: Any,
        *,
        summary: str = '',
        metadata: Dict[str, Any] | None = None,
        images: List[str] | None = None,
        state_snapshot: Dict[str, Any] | None = None,
    ) -> bool:
        """Attach a tool result to the assistant tool_call that owns it.

        This is the single persisted RunTree write path for ordinary tool results
        and subtask final summaries. Transient role='tool' messages should be
        converted into this call instead of being appended to Conversation.
        """
        call_id = str(tool_call_id or '').strip()
        if not call_id:
            return False
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.role != 'assistant' or not msg.tool_calls:
                continue
            for tc in msg.tool_calls:
                if str(tc.get('id') or '') != call_id:
                    continue
                clean_metadata = dict(metadata or {})
                clean_metadata.pop('subtasks', None)
                clean_metadata.pop('subtasks_by_call', None)
                normalized = normalize_tool_result(result_payload)
                existing_result = normalize_tool_result(tc.get('result'))
                normalized_metadata = dict(normalized.get('metadata') or {}) if isinstance(normalized.get('metadata'), dict) else {}
                if existing_result.get('type') == 'subtask_run' or is_subtask_tool_call(tc) or normalized.get('type') == 'subtask_run':
                    result = existing_result if existing_result.get('type') == 'subtask_run' else normalized
                    result['type'] = 'subtask_run'
                    if normalized.get('content') or 'content' in normalized:
                        result['content'] = str(normalized.get('content') or '')
                    if summary:
                        result['summary'] = summary
                    elif normalized.get('summary'):
                        result['summary'] = str(normalized.get('summary') or '')
                    if isinstance(normalized.get('run'), dict):
                        result['run'] = normalized['run']
                    merged_metadata = dict(result.get('metadata') or {})
                    merged_metadata.update(normalized_metadata)
                    merged_metadata.update(clean_metadata)
                    result['metadata'] = merged_metadata
                else:
                    merged_metadata = dict(normalized_metadata)
                    merged_metadata.update(clean_metadata)
                    result = {
                        'type': 'tool_result',
                        'content': str(normalized.get('content') or ''),
                        'summary': summary or str(normalized.get('summary') or ''),
                        'metadata': merged_metadata,
                    }
                if images:
                    result['images'] = list(images)
                set_tool_call_result(tc, result)
                if result.get('summary'):
                    tc['result_summary'] = str(result.get('summary') or '')
                tc['result_metadata'] = dict(result.get('metadata') or {})
                if images:
                    tc['result_images'] = list(images)
                if state_snapshot and isinstance(state_snapshot, dict) and not is_state_checkpoint_snapshot(state_snapshot):
                    try:
                        self._state_dict = state_snapshot.copy()
                    except Exception as exc:
                        logger.debug("Failed to copy merged tool state snapshot: %s", exc)
                if state_snapshot and isinstance(state_snapshot, dict):
                    try:
                        msg.state_snapshot = state_snapshot
                    except Exception as exc:
                        logger.debug("Failed to attach merged tool state snapshot to assistant message: %s", exc)
                self.updated_at = datetime.now()
                return True
        return False

    def update_message(self, message_id: str, content: str = None, 
                       images: List[str] = None):
        """Update an existing message"""
        for msg in self.messages:
            if msg.id == message_id:
                if content is not None:
                    msg.content = content
                if images is not None:
                    msg.images = images
                self.updated_at = datetime.now()
                break

    def delete_message(self, message_id: str) -> List[str]:
        """Delete a message from the conversation.
           Returns a list containing the deleted message ID.
           Note: If we were storing tool results as separate messages, we would need to cascade delete them here.
           But since we merge tool results into the assistant message, deleting the assistant message
           implicitly deletes the results.
        """
        original_count = len(self.messages)
        self.messages = [m for m in self.messages if m.id != message_id]
        
        if len(self.messages) < original_count:
            self.updated_at = datetime.now()
            return [message_id]
        return []

    def get_tokens_per_minute(self) -> float:
        """Calculate average tokens per minute for assistant responses"""
        total_tokens = 0
        total_time_ms = 0
        
        for msg in self.messages:
            if msg.role == 'assistant' and msg.tokens and msg.response_time_ms:
                total_tokens += msg.tokens
                total_time_ms += msg.response_time_ms
        
        if total_time_ms > 0:
            return (total_tokens / total_time_ms) * 60000  # Convert to per minute
        return 0.0

    def generate_title_from_first_message(self):
        """Generate title from first user message"""
        for msg in self.messages:
            if msg.role == 'user' and msg.content:
                # Take first 50 characters
                title = msg.content[:50]
                if len(msg.content) > 50:
                    title += "..."
                self.title = title
                break
    # ============ Sequence ID Management ============
    
    def next_seq_id(self) -> int:
        """Get next sequence ID and increment counter"""
        self._seq_counter += 1
        return self._seq_counter

    def current_seq_id(self) -> int:
        """Get current sequence ID without incrementing"""
        return self._seq_counter

    def add_message_with_seq(self, message: Message) -> Message:
        """Compatibility wrapper for the canonical add_message path."""
        self.add_message(message)
        return message

    def get_llm_config(self):
        """Return the normalized LLM request config for this conversation."""
        from models.llm_config import LLMConfig

        cfg = LLMConfig.from_conversation(self)
        try:
            self.llm_config = cfg.to_dict()
        except Exception as exc:
            logger.debug("Failed to cache llm_config on conversation: %s", exc)
        return cfg

    def set_llm_config(self, config):
        """Persist a normalized LLM request config onto this conversation."""
        from models.llm_config import LLMConfig

        if isinstance(config, LLMConfig):
            cfg = config
        elif isinstance(config, dict):
            cfg = LLMConfig.from_dict(config)
        else:
            raise TypeError("config must be an LLMConfig or dict")

        cfg.apply_to_conversation(self)
        self.updated_at = datetime.now()
        return cfg

    # ============ SessionState Management ============
    
    def get_state(self) -> 'SessionState':
        """Get the SessionState object (lazy-loaded to avoid circular import)"""
        from models.contracts.session_state import SessionState
        return SessionState.from_dict(self._state_dict)

    def set_state(self, state: 'SessionState'):
        """Update the internal state dictionary from a SessionState object"""
        self._state_dict = state.to_dict()
        self.updated_at = datetime.now()

    def update_state_dict(self, updates: Dict[str, Any]):
        """Directly update state dictionary fields"""
        self._state_dict.update(updates)
        self.updated_at = datetime.now()

    # ============ Rollback / Time-Travel ============
    
    def rollback_to_seq(self, target_seq_id: int) -> bool:
        """
        Rollback conversation to a specific seq_id.
        
        This will:
        1. Remove all messages with seq_id > target_seq_id
        2. Restore state from the last message with a state_snapshot
        
        Returns True if rollback was successful.
        """
        if target_seq_id <= 0:
            return False
        
        # 1. Filter messages
        original_count = len(self.messages)
        self.messages = [m for m in self.messages if m.seq_id <= target_seq_id]
        
        if len(self.messages) == original_count:
            # No messages removed, target_seq_id might be current or future
            return False
        
        # 2. Reset seq_counter
        self._seq_counter = target_seq_id
        
        # 3. Find and restore the latest state snapshot
        from models.contracts.session_state import SessionState
        restored = False
        for msg in reversed(self.messages):
            if msg.state_snapshot and not is_state_checkpoint_snapshot(msg.state_snapshot):
                self._state_dict = msg.state_snapshot.copy()
                restored = True
                break
        
        if not restored:
            # No snapshot found, reset to empty state
            self._state_dict = {}
        
        self.updated_at = datetime.now()
        return True

    def attach_state_snapshot(self, message_id: str):
        """
        Attach current state as a snapshot to a specific message.
        
        Call this after tool execution or at key checkpoints
        to enable rollback to that point.
        """
        for msg in self.messages:
            if msg.id == message_id:
                try:
                    msg.state_snapshot = state_checkpoint_from_dict(self._state_dict)
                except Exception:
                    msg.state_snapshot = {
                        "_snapshot_kind": "checkpoint",
                        "state_version": 0,
                        "last_updated_seq": 0,
                    }
                self.updated_at = datetime.now()
                return True
        return False

    def get_last_message_with_snapshot(self) -> Optional[Message]:
        """Find the most recent message that has a state snapshot"""
        for msg in reversed(self.messages):
            if msg.state_snapshot:
                return msg
        return None
