"""
Storage service for persisting conversations and settings
"""

import json
import logging
import os
import tempfile
from typing import List, Optional, Dict, Any
from pathlib import Path

from core.config import get_global_data_dir
from models.conversation import Conversation
from models.provider import Provider
from models.mcp_server import McpServerConfig
from models.search_config import SearchConfig
from services.importers import parse_imported_data
from services.workspace_session_service import WorkspaceSessionService


logger = logging.getLogger(__name__)


class StorageService:
    """Handles local storage of conversations and providers"""
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = str(get_global_data_dir())
        
        self.data_dir = Path(data_dir)
        self.conversations_dir = self.data_dir / 'conversations'
        self.conversations_index_file = self.data_dir / 'conversations_index.json'
        self.providers_file = self.data_dir / 'providers.json'
        self.mcp_servers_file = self.data_dir / 'mcp_servers.json'
        self.search_config_file = self.data_dir / 'search_config.json'
        self.settings_file = self.data_dir / 'settings.json'
        self.workspace_sessions = WorkspaceSessionService()
        
        # Ensure directories exist
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)

    # ============ Conversation Operations ============
    
    def save_conversation(self, conversation: Conversation) -> bool:
        """Save a conversation to disk"""
        try:
            file_path = self.conversations_dir / f"{conversation.id}.json"
            self.conversations_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{conversation.id}.",
                suffix=".tmp",
                dir=str(self.conversations_dir),
                text=True,
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(conversation.to_json())
            os.replace(temp_name, file_path)
            self._upsert_conversation_index(conversation)
            self.workspace_sessions.save_snapshot(conversation)
            return True
        except Exception as e:
            logger.warning("Error saving conversation %s: %s", getattr(conversation, "id", ""), e)
            try:
                temp = locals().get("temp_name")
                if temp:
                    Path(temp).unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def load_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """Load a conversation by ID"""
        try:
            file_path = self.conversations_dir / f"{conversation_id}.json"
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return Conversation.from_json(f.read())
        except Exception as e:
            logger.warning("Error loading conversation %s: %s", conversation_id, e)
        return None

    def list_conversations(self) -> List[Dict[str, Any]]:
        """List all conversations (metadata only for performance)"""
        indexed = self._load_conversation_index()
        if indexed is not None:
            return indexed
        conversations = self._rebuild_conversation_index()
        self._save_conversation_index(conversations)
        return conversations

    def _conversation_metadata_from_dict(self, data: Dict[str, Any]) -> Dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        conversation_id = str(data.get('id') or '').strip()
        if not conversation_id:
            return None
        messages = data.get('messages', [])
        return {
            'id': conversation_id,
            'title': data.get('title', 'Untitled'),
            'created_at': data.get('created_at'),
            'updated_at': data.get('updated_at'),
            'work_dir': data.get('work_dir', ''),
            'model': data.get('model', ''),
            'message_count': len(messages) if isinstance(messages, list) else 0,
        }

    def _conversation_metadata(self, conversation: Conversation) -> Dict[str, Any]:
        return self._conversation_metadata_from_dict(conversation.to_dict()) or {
            'id': str(getattr(conversation, 'id', '') or ''),
            'title': str(getattr(conversation, 'title', '') or 'Untitled'),
            'created_at': getattr(getattr(conversation, 'created_at', None), 'isoformat', lambda: None)(),
            'updated_at': getattr(getattr(conversation, 'updated_at', None), 'isoformat', lambda: None)(),
            'work_dir': str(getattr(conversation, 'work_dir', '') or ''),
            'model': str(getattr(conversation, 'model', '') or ''),
            'message_count': len(getattr(conversation, 'messages', []) or []),
        }

    def _load_conversation_index(self) -> List[Dict[str, Any]] | None:
        try:
            if not self.conversations_index_file.exists():
                return None
            with open(self.conversations_index_file, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            rows = payload.get('conversations') if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return None
            ids = {
                str(item.get('id') or '').strip()
                for item in rows
                if isinstance(item, dict) and str(item.get('id') or '').strip()
            }
            disk_ids = {path.stem for path in self.conversations_dir.glob('*.json') if path.stat().st_size > 0}
            if ids != disk_ids:
                return None
            out = [dict(item) for item in rows if isinstance(item, dict)]
            out.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
            return out
        except Exception as e:
            logger.debug("Failed to load conversation index: %s", e)
            return None

    def _save_conversation_index(self, conversations: List[Dict[str, Any]]) -> None:
        try:
            payload = {'conversations': list(conversations or [])}
            with open(self.conversations_index_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug("Failed to save conversation index: %s", e)

    def _upsert_conversation_index(self, conversation: Conversation) -> None:
        rows = self._load_conversation_index()
        if rows is None:
            rows = self._rebuild_conversation_index()
        metadata = self._conversation_metadata(conversation)
        next_rows = [row for row in rows if str(row.get('id') or '') != metadata['id']]
        next_rows.append(metadata)
        next_rows.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
        self._save_conversation_index(next_rows)

    def _remove_conversation_index(self, conversation_id: str) -> None:
        rows = self._load_conversation_index()
        if rows is None:
            rows = self._rebuild_conversation_index()
        next_rows = [row for row in rows if str(row.get('id') or '') != str(conversation_id or '')]
        self._save_conversation_index(next_rows)

    def _rebuild_conversation_index(self) -> List[Dict[str, Any]]:
        """Rebuild the metadata index from conversation JSON files."""
        conversations = []
        try:
            for file_path in self.conversations_dir.glob('*.json'):
                try:
                    if file_path.stat().st_size <= 0:
                        logger.debug("Skipping empty conversation file: %s", file_path)
                        continue
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        logger.debug("Skipping non-object conversation file: %s", file_path)
                        continue
                    metadata = self._conversation_metadata_from_dict(data)
                    if metadata is None:
                        logger.debug("Skipping conversation file without id: %s", file_path)
                        continue
                    conversations.append(metadata)
                except Exception as e:
                    logger.debug("Skipping unreadable conversation file %s: %s", file_path, e)
        except Exception as e:
            logger.warning("Error listing conversations from %s: %s", self.conversations_dir, e)
        
        # Sort by updated_at descending
        conversations.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
        return conversations

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation"""
        try:
            file_path = self.conversations_dir / f"{conversation_id}.json"
            work_dir = None
            if file_path.exists():
                try:
                    conversation = self.load_conversation(conversation_id)
                    if conversation is not None:
                        work_dir = str(getattr(conversation, 'work_dir', '') or '.')
                except Exception:
                    work_dir = None
            if file_path.exists():
                file_path.unlink()
                self._remove_conversation_index(conversation_id)
                self.workspace_sessions.delete_snapshot(conversation_id, work_dir=work_dir)
                return True
        except Exception as e:
            logger.warning("Error deleting conversation %s: %s", conversation_id, e)
        return False

    def import_conversation(self, file_path: str) -> Optional[Conversation]:
        """Import a conversation from an external JSON file"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            # Handle different JSON formats
            conversation = parse_imported_data(data)
            if conversation:
                # Assign new ID to avoid conflicts
                import uuid
                conversation.id = str(uuid.uuid4())
                self.save_conversation(conversation)
                return conversation
        except Exception as e:
            logger.warning("Error importing conversation from %s: %s", file_path, e)
        return None

    # ============ Provider Operations ============
    
    def save_providers(self, providers: List[Provider]) -> bool:
        """Save all providers"""
        try:
            data = [p.to_dict() for p in providers]
            with open(self.providers_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning("Error saving providers: %s", e)
            return False

    def load_providers(self) -> List[Provider]:
        """Load all providers"""
        try:
            if self.providers_file.exists():
                with open(self.providers_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return [Provider.from_dict(p) for p in data]
        except Exception as e:
            logger.warning("Error loading providers: %s", e)
        return []

    # ============ Settings Operations ============
    
    def save_settings(self, settings: Dict[str, Any]) -> bool:
        """Save application settings"""
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning("Error saving settings: %s", e)
            return False

    def load_settings(self) -> Dict[str, Any]:
        """Load application settings"""
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("Error loading settings: %s", e)
        return {}  # Explicit return empty dict on failure/missing

    # ============ MCP Servers ============

    def save_mcp_servers(self, servers: List[McpServerConfig]) -> bool:
        """Save MCP servers configuration"""
        try:
            data = [s.to_dict() for s in servers]
            with open(self.mcp_servers_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning("Error saving MCP servers: %s", e)
            return False

    def load_mcp_servers(self) -> List[McpServerConfig]:
        """Load MCP servers configuration"""
        try:
            if not self.mcp_servers_file.exists():
                return []
            
            with open(self.mcp_servers_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            return [McpServerConfig.from_dict(d) for d in data if isinstance(d, dict)]
        except Exception as e:
            logger.warning("Error loading MCP servers: %s", e)
            return []

    # ============ Search Config ============

    def save_search_config(self, config: SearchConfig) -> bool:
        """Save search configuration"""
        try:
            with open(self.search_config_file, 'w', encoding='utf-8') as f:
                json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning("Error saving search config: %s", e)
            return False

    def load_search_config(self) -> SearchConfig:
        """Load search configuration"""
        try:
            if self.search_config_file.exists():
                with open(self.search_config_file, 'r', encoding='utf-8') as f:
                    return SearchConfig.from_dict(json.load(f))
        except Exception as e:
            logger.warning("Error loading search config: %s", e)
        return SearchConfig()
