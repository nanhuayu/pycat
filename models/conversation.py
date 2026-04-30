
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any, TYPE_CHECKING
import uuid
import json

if TYPE_CHECKING:
    from models.state import SessionState


logger = logging.getLogger(__name__)


@dataclass
class Message:
    """Represents a single message in a conversation"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "user"  # "user", "assistant", "system"
    content: str = ""
    images: List[str] = field(default_factory=list)  # Base64 or file paths
    tool_calls: Optional[List[Dict[str, Any]]] = None  # [{id, type, function: {name, arguments}}]
    tool_call_id: Optional[str] = None  # For role="tool" messages
    thinking: Optional[str] = None
    tokens: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    response_time_ms: Optional[int] = None  # Response time in milliseconds
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # === Event Sourcing: Global sequence ID for time-travel/rollback ===
    seq_id: int = 0  # Assigned by Conversation.next_seq_id()
    
    # === State Snapshot (for rollback) ===
    # Attached at key points (after tool execution, assistant response complete)
    # When rolling back, restore state from the last message with a snapshot
    state_snapshot: Optional[Dict[str, Any]] = None  # Serialized SessionState
    
    # === Legacy: Non-destructive history fields (kept for backward compatibility) ===
    condense_parent: Optional[str] = None  # ID of the summary message that "condensed" this message
    truncation_parent: Optional[str] = None # ID of the truncation marker (future use)
    
    # Per-message condensation (Agent Mode optimization)
    summary: Optional[str] = None # Concise summary of this message (for token saving in future turns)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        result = {
            'id': self.id,
            'role': self.role,
            'content': self.content,
            'images': self.images,
            'tool_calls': self.tool_calls,
            'tool_call_id': self.tool_call_id,
            'thinking': self.thinking,
            'tokens': self.tokens,
            'created_at': self.created_at.isoformat(),
            'response_time_ms': self.response_time_ms,
            'metadata': self.metadata,
            'seq_id': self.seq_id,
            'condense_parent': self.condense_parent,
            'truncation_parent': self.truncation_parent,
            'summary': self.summary
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
            
        return cls(
            id=data.get('id', str(uuid.uuid4())),
            role=data.get('role', 'user'),
            content=content,
            images=images,
            tool_calls=data.get('tool_calls'),
            tool_call_id=data.get('tool_call_id'),
            thinking=data.get('thinking'),
            tokens=data.get('tokens'),
            created_at=created_at,
            response_time_ms=data.get('response_time_ms'),
            metadata=metadata,
            seq_id=data.get('seq_id', 0),
            state_snapshot=data.get('state_snapshot'),
            condense_parent=data.get('condense_parent'),
            truncation_parent=data.get('truncation_parent'),
            summary=data.get('summary')
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
    
    # === Schema version for migration (v1=legacy condense_parent, v2=state-based) ===
    version: int = 2
    
    # === SessionState: Centralized state management ===
    # Lazy-loaded to avoid circular import; use get_state() method
    _state_dict: Dict[str, Any] = field(default_factory=dict)
    
    # === Sequence counter for time-travel/rollback ===
    _seq_counter: int = 0

    def __post_init__(self) -> None:
        self._sync_llm_config_projection()

    def _sync_llm_config_projection(self) -> None:
        try:
            from core.llm.llm_config import LLMConfig

            cfg = LLMConfig.from_conversation(self)
            cfg.apply_to_conversation(self)
        except Exception as exc:
            logger.debug("Failed to synchronize llm_config projection: %s", exc)
            if not isinstance(self.llm_config, dict):
                self.llm_config = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'version': self.version,
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
        """Create from dictionary with backward compatibility"""
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
        
        # Version detection: missing version field means legacy v1 format
        version = data.get('version', 1)
        
        # Load state dict (empty for v1 legacy data)
        state_dict = data.get('state', {})
        
        # Load or compute seq_counter
        seq_counter = data.get('_seq_counter', 0)
        if seq_counter == 0 and messages:
            # Migration: assign seq_id to messages that don't have one
            max_seq = max((m.seq_id for m in messages), default=0)
            if max_seq == 0:
                # All messages lack seq_id, assign sequentially
                for i, msg in enumerate(messages, start=1):
                    msg.seq_id = i
                seq_counter = len(messages)
            else:
                seq_counter = max_seq
        
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
            settings=data.get('settings', {}),
            mode=data.get('mode', 'chat'),
            llm_config=data.get('llm_config', {}),
            version=version,
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
        """Add a message to the conversation.
           If the message is a tool result (role='tool'), try to merge it into the existing assistant message.
        """
        if message.role == 'tool' and message.tool_call_id:
            if self._try_merge_tool_result(message):
                return

        # Normal append
        self.messages.append(message)
        if message.tokens:
            self.total_tokens += message.tokens
        self.updated_at = datetime.now()

    def _try_merge_tool_result(self, message: Message) -> bool:
        """Try to find the assistant message that triggered this tool result and merge it."""
        # Search backwards for the assistant message containing the tool call
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.role == 'assistant' and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.get('id') == message.tool_call_id:
                        tc['result'] = message.content
                        summary = str(getattr(message, 'summary', '') or '').strip()
                        if summary:
                            tc['result_summary'] = summary
                        metadata = getattr(message, 'metadata', {}) or {}
                        if isinstance(metadata, dict) and metadata:
                            tc['result_metadata'] = dict(metadata)
                        if message.images:
                            tc['result_images'] = list(message.images)
                        # If the tool execution updated SessionState, apply it immediately.
                        # This keeps UI panels (e.g., tasks) consistent even when tool messages are merged.
                        if message.state_snapshot and isinstance(message.state_snapshot, dict):
                            try:
                                self._state_dict = message.state_snapshot.copy()
                            except Exception as exc:
                                logger.debug("Failed to copy merged tool state snapshot: %s", exc)
                            # Attach checkpoint for rollback on the triggering assistant message.
                            try:
                                msg.state_snapshot = message.state_snapshot
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
        """Add a message with automatic seq_id assignment"""
        if message.seq_id == 0:
            message.seq_id = self.next_seq_id()
        self.add_message(message)
        return message

    def get_llm_config(self):
        """Return the normalized LLM request config for this conversation."""
        from core.llm.llm_config import LLMConfig

        cfg = LLMConfig.from_conversation(self)
        try:
            self.llm_config = cfg.to_dict()
        except Exception as exc:
            logger.debug("Failed to cache llm_config on conversation: %s", exc)
        return cfg

    def set_llm_config(self, config):
        """Persist a normalized LLM request config onto this conversation."""
        from core.llm.llm_config import LLMConfig

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
        from models.state import SessionState
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
        from models.state import SessionState
        restored = False
        for msg in reversed(self.messages):
            if msg.state_snapshot:
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
                msg.state_snapshot = self._state_dict.copy()
                self.updated_at = datetime.now()
                return True
        return False

    def get_last_message_with_snapshot(self) -> Optional[Message]:
        """Find the most recent message that has a state snapshot"""
        for msg in reversed(self.messages):
            if msg.state_snapshot:
                return msg
        return None