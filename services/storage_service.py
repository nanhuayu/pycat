"""
Storage service for persisting conversations and settings
"""

import os
import json
from typing import List, Optional, Dict, Any
from pathlib import Path

from core.config import get_global_data_dir
from models.conversation import Conversation
from models.provider import Provider
from models.mcp_server import McpServerConfig
from models.search_config import SearchConfig
from services.importers import parse_imported_data
from services.workspace_session_service import WorkspaceSessionService


class StorageService:
    """Handles local storage of conversations and providers"""
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = str(get_global_data_dir())
        
        self.data_dir = Path(data_dir)
        self.conversations_dir = self.data_dir / 'conversations'
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
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(conversation.to_json())
            self.workspace_sessions.save_snapshot(conversation)
            return True
        except Exception as e:
            print(f"Error saving conversation: {e}")
            return False

    def load_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """Load a conversation by ID"""
        try:
            file_path = self.conversations_dir / f"{conversation_id}.json"
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return Conversation.from_json(f.read())
        except Exception as e:
            print(f"Error loading conversation: {e}")
        return None

    def list_conversations(self) -> List[Dict[str, Any]]:
        """List all conversations (metadata only for performance)"""
        conversations = []
        try:
            for file_path in self.conversations_dir.glob('*.json'):
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Return only metadata, not full messages
                    conversations.append({
                        'id': data.get('id'),
                        'title': data.get('title', 'Untitled'),
                        'created_at': data.get('created_at'),
                        'updated_at': data.get('updated_at'),
                        'model': data.get('model', ''),
                        'message_count': len(data.get('messages', []))
                    })
        except Exception as e:
            print(f"Error listing conversations: {e}")
        
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
                self.workspace_sessions.delete_snapshot(conversation_id, work_dir=work_dir)
                return True
        except Exception as e:
            print(f"Error deleting conversation: {e}")
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
            print(f"Error importing conversation: {e}")
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
            print(f"Error saving providers: {e}")
            return False

    def load_providers(self) -> List[Provider]:
        """Load all providers"""
        try:
            if self.providers_file.exists():
                with open(self.providers_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return [Provider.from_dict(p) for p in data]
        except Exception as e:
            print(f"Error loading providers: {e}")
        return []

    # ============ Settings Operations ============
    
    def save_settings(self, settings: Dict[str, Any]) -> bool:
        """Save application settings"""
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"Error saving settings: {e}")
            return False

    def load_settings(self) -> Dict[str, Any]:
        """Load application settings"""
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading settings: {e}")
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
            print(f"Error saving MCP servers: {e}")
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
            print(f"Error loading MCP servers: {e}")
            return []

    # ============ Search Config ============

    def save_search_config(self, config: SearchConfig) -> bool:
        """Save search configuration"""
        try:
            with open(self.search_config_file, 'w', encoding='utf-8') as f:
                json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"Error saving search config: {e}")
            return False

    def load_search_config(self) -> SearchConfig:
        """Load search configuration"""
        try:
            if self.search_config_file.exists():
                with open(self.search_config_file, 'r', encoding='utf-8') as f:
                    return SearchConfig.from_dict(json.load(f))
        except Exception as e:
            print(f"Error loading search config: {e}")
        return SearchConfig()
