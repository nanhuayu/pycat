"""LLM provider/service connection configuration model."""

from dataclasses import dataclass, field
from typing import List, Dict, Any
import uuid
import json

from models.model_profile import ModelProfile
from models.model_ref import (
    build_model_ref,
    normalize_provider_name,
    provider_matches_name,
    split_model_ref,
)

OPENAI_COMPATIBLE = "openai_compatible"
OPENAI_RESPONSES = "openai_responses"
ANTHROPIC_MESSAGES = "anthropic_messages"
OLLAMA_CHAT = "ollama_chat"

# Backward-compatible Anthropic symbol name kept for older imports/configs.
ANTHROPIC_NATIVE = ANTHROPIC_MESSAGES
DEFAULT_API_TYPE = OPENAI_COMPATIBLE

SUPPORTED_API_TYPES = {
    OPENAI_COMPATIBLE,
    OPENAI_RESPONSES,
    ANTHROPIC_MESSAGES,
    OLLAMA_CHAT,
}
LEGACY_API_TYPE_ALIASES = {
    "openai": OPENAI_COMPATIBLE,
    "chat_completions": OPENAI_COMPATIBLE,
    "chat-completions": OPENAI_COMPATIBLE,
    "openai_compatible_chat": OPENAI_COMPATIBLE,
    "openai-compatible-chat": OPENAI_COMPATIBLE,
    "responses": OPENAI_RESPONSES,
    "openai-response": OPENAI_RESPONSES,
    "openai_responses_api": OPENAI_RESPONSES,
    "openai-responses-api": OPENAI_RESPONSES,
    "openai-compatible": OPENAI_COMPATIBLE,
    "compatible": OPENAI_COMPATIBLE,
    "anthropic": ANTHROPIC_MESSAGES,
    "anthropic_native": ANTHROPIC_MESSAGES,
    "anthropic-native": ANTHROPIC_MESSAGES,
    "anthropic_messages": ANTHROPIC_MESSAGES,
    "anthropic-messages": ANTHROPIC_MESSAGES,
    "ollama": OLLAMA_CHAT,
    "ollama-chat": OLLAMA_CHAT,
}


def normalize_api_type(value: Any, default: str = DEFAULT_API_TYPE) -> str:
    text = str(value or "").strip().lower()
    if text in SUPPORTED_API_TYPES:
        return text
    text = text.replace(" ", "_")
    if text in SUPPORTED_API_TYPES:
        return text
    alias = LEGACY_API_TYPE_ALIASES.get(text) or LEGACY_API_TYPE_ALIASES.get(text.replace("_", "-"))
    if alias:
        return alias
    fallback = str(default or "").strip().lower()
    if fallback in SUPPORTED_API_TYPES:
        return fallback
    return DEFAULT_API_TYPE


def api_type_label(value: Any) -> str:
    api_type = normalize_api_type(value)
    labels = {
        OPENAI_COMPATIBLE: "OpenAI 兼容 / Chat Completions",
        OPENAI_RESPONSES: "OpenAI Responses",
        ANTHROPIC_MESSAGES: "Anthropic Messages",
        OLLAMA_CHAT: "Ollama Chat",
    }
    return labels.get(api_type, labels[DEFAULT_API_TYPE])


def _strip_api_version_suffix(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    lower = base.lower()
    for suffix in ("/v1", "/api"):
        if lower.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


@dataclass
class Provider:
    """Represents an LLM provider configuration"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "New Provider"
    api_type: str = DEFAULT_API_TYPE
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    models: List[str] = field(default_factory=list)
    model_profiles: List[ModelProfile] = field(default_factory=list)
    default_model: str = ""
    custom_headers: Dict[str, str] = field(default_factory=dict)
    request_format: Dict[str, Any] = field(default_factory=dict)
    supports_thinking: bool = False
    supports_vision: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        self.normalize_inplace()

    def normalize_inplace(self) -> None:
        self.name = normalize_provider_name(self.name)
        self.api_type = normalize_api_type(getattr(self, "api_type", DEFAULT_API_TYPE))
        self.models = self._normalize_model_ids(getattr(self, "models", []) or [])
        self.model_profiles = self._normalize_model_profiles(getattr(self, "model_profiles", []) or [])

    @staticmethod
    def _normalize_model_ids(values: Any) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for value in values or []:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            out.append(text)
            seen.add(text)
        return out

    @staticmethod
    def _normalize_model_profiles(values: Any) -> List[ModelProfile]:
        out: List[ModelProfile] = []
        seen: set[str] = set()
        for value in values or []:
            profile = value if isinstance(value, ModelProfile) else ModelProfile.from_dict(value)
            if not profile.model_id or profile.model_id in seen:
                continue
            out.append(profile)
            seen.add(profile.model_id)
        return out

    @property
    def canonical_name(self) -> str:
        return normalize_provider_name(self.name)

    @property
    def is_anthropic_native(self) -> bool:
        return self.api_type == ANTHROPIC_MESSAGES

    @property
    def is_openai_responses(self) -> bool:
        return self.api_type == OPENAI_RESPONSES

    @property
    def is_ollama_chat(self) -> bool:
        return self.api_type == OLLAMA_CHAT

    @property
    def is_chat_completions_like(self) -> bool:
        return self.api_type == OPENAI_COMPATIBLE

    @property
    def requires_api_key(self) -> bool:
        return self.api_type != OLLAMA_CHAT

    def format_model_ref(self, model_name: str = "") -> str:
        return build_model_ref(self.name, model_name or self.default_model)

    def get_model_profiles(self) -> List[ModelProfile]:
        """Return explicit profiles plus generated legacy profiles."""
        out: List[ModelProfile] = []
        seen: set[str] = set()
        for profile in self.model_profiles:
            if profile.model_id and profile.model_id not in seen:
                out.append(profile)
                seen.add(profile.model_id)

        legacy_ids = []
        if self.default_model:
            legacy_ids.append(self.default_model)
        legacy_ids.extend(self.models)
        for model_id in legacy_ids:
            if not model_id or model_id in seen:
                continue
            out.append(
                ModelProfile.from_model_id(
                    model_id,
                    supports_vision=bool(self.supports_vision),
                    supports_reasoning=bool(self.supports_thinking),
                )
            )
            seen.add(model_id)
        return out

    def find_model_profile(self, model_id: str = "") -> ModelProfile | None:
        target = str(model_id or self.default_model or "").strip()
        if not target:
            return None
        for profile in self.get_model_profiles():
            if profile.model_id == target:
                return profile
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'id': self.id,
            'name': self.name,
            'api_type': self.api_type,
            'api_base': self.api_base,
            'api_key': self.api_key,
            'models': self.models,
            'model_profiles': [profile.to_dict() for profile in self.model_profiles],
            'default_model': self.default_model,
            'custom_headers': self.custom_headers,
            'request_format': self.request_format,
            'supports_thinking': self.supports_thinking,
            'supports_vision': self.supports_vision,
            'enabled': self.enabled
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Provider':
        """Create from dictionary"""
        return cls(
            id=data.get('id', str(uuid.uuid4())),
            name=data.get('name', 'Provider'),
            api_type=data.get('api_type', DEFAULT_API_TYPE),
            api_base=data.get('api_base', 'https://api.openai.com/v1'),
            api_key=data.get('api_key', ''),
            models=data.get('models', []),
            model_profiles=data.get('model_profiles', []),
            default_model=data.get('default_model', ''),
            custom_headers=data.get('custom_headers', {}),
            request_format=data.get('request_format', {}),
            supports_thinking=data.get('supports_thinking', False),
            supports_vision=data.get('supports_vision', True),
            enabled=data.get('enabled', True)
        )

    def to_json(self) -> str:
        """Serialize to JSON string"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> 'Provider':
        """Create from JSON string"""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def get_headers(self) -> Dict[str, str]:
        """Get complete headers for API requests"""
        headers = {
            'Content-Type': 'application/json',
        }
        if self.is_anthropic_native:
            if self.api_key:
                headers['x-api-key'] = self.api_key
            headers.setdefault('anthropic-version', '2023-06-01')
        elif self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        headers.update(self.custom_headers)
        return headers

    def get_chat_endpoint(self) -> str:
        """Get the chat endpoint for this provider interface."""
        base = self.api_base.rstrip('/')
        if self.is_anthropic_native:
            return f"{base}/messages"
        if self.is_openai_responses:
            return f"{base}/responses"
        if self.is_ollama_chat:
            return f"{_strip_api_version_suffix(base)}/api/chat"
        return f"{base}/chat/completions"

    def get_models_endpoint(self) -> str:
        """Get the models list endpoint"""
        base = self.api_base.rstrip('/')
        if self.is_ollama_chat:
            return f"{_strip_api_version_suffix(base)}/api/tags"
        return f"{base}/models"
