"""LLM provider/service connection configuration model."""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Iterable
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

ANTHROPIC_NATIVE = ANTHROPIC_MESSAGES
DEFAULT_API_TYPE = OPENAI_COMPATIBLE
PROVIDER_SCHEMA_VERSION = 3

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
    models: List[ModelProfile] = field(default_factory=list)
    custom_headers: Dict[str, str] = field(default_factory=dict)
    supports_reasoning: bool = False
    supports_vision: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        self.normalize_inplace()

    def normalize_inplace(self) -> None:
        self.name = normalize_provider_name(self.name)
        self.api_type = normalize_api_type(getattr(self, "api_type", DEFAULT_API_TYPE))
        self.models = self._normalize_models(getattr(self, "models", []) or [])

    def _normalize_models(self, values: Iterable[Any]) -> List[ModelProfile]:
        out: List[ModelProfile] = []
        seen: set[str] = set()
        for value in values or []:
            if isinstance(value, ModelProfile):
                profile = ModelProfile.from_dict(value.to_dict())
            elif isinstance(value, dict):
                profile = ModelProfile.from_dict(value)
            else:
                profile = ModelProfile.from_model_id(
                    str(value or "").strip(),
                    supports_vision=bool(self.supports_vision),
                    supports_reasoning=bool(self.supports_reasoning),
                )
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
        return build_model_ref(self.name, model_name)

    def get_models(self) -> List[ModelProfile]:
        return [ModelProfile.from_dict(profile.to_dict()) for profile in self.models]

    def model_ids(self) -> List[str]:
        return [profile.model_id for profile in self.models if profile.model_id]

    def find_model_profile(self, model_id: str = "") -> ModelProfile | None:
        target = str(model_id or "").strip()
        if not target:
            return None
        for profile in self.models:
            if profile.model_id == target:
                return profile
        return None

    def effective_model_profile(self, model_id: str = "") -> ModelProfile:
        """Return explicit model metadata or a provider-backed default profile."""

        target = str(model_id or "").strip()
        explicit = self.find_model_profile(target)
        if explicit is not None:
            return ModelProfile.from_dict(explicit.to_dict())
        return ModelProfile.from_model_id(
            target,
            supports_vision=bool(self.supports_vision),
            supports_reasoning=bool(self.supports_reasoning),
        )

    def upsert_model(self, profile: ModelProfile) -> None:
        normalized = ModelProfile.from_dict(profile.to_dict())
        if not normalized.model_id:
            raise ValueError("Model ID is required")
        for index, existing in enumerate(self.models):
            if existing.model_id == normalized.model_id:
                self.models[index] = normalized
                return
        self.models.append(normalized)

    def remove_model(self, model_id: str) -> None:
        target = str(model_id or "").strip()
        self.models = [profile for profile in self.models if profile.model_id != target]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'schema_version': PROVIDER_SCHEMA_VERSION,
            'id': self.id,
            'name': self.name,
            'api_type': self.api_type,
            'api_base': self.api_base,
            'api_key': self.api_key,
            'models': [profile.to_dict() for profile in self.models],
            'custom_headers': self.custom_headers,
            'supports_reasoning': self.supports_reasoning,
            'supports_vision': self.supports_vision,
            'enabled': self.enabled
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Provider':
        """Create from dictionary"""
        supports_reasoning = data.get('supports_reasoning', data.get('supports_thinking', False))
        supports_vision = data.get('supports_vision', True)
        raw_models = list(data.get('models', []) or [])
        schema_version = int(data.get('schema_version', 1) or 1)
        if schema_version < PROVIDER_SCHEMA_VERSION:
            explicit = {
                profile.model_id: profile
                for profile in (
                    ModelProfile.from_dict(item)
                    for item in (data.get('model_profiles', []) or [])
                )
                if profile.model_id
            }
            legacy_request = data.get('request_format') if isinstance(data.get('request_format'), dict) else {}
            selected_ids: list[str] = []

            def select(model_id: object) -> None:
                value = str(model_id or "").strip()
                if value and value not in selected_ids:
                    selected_ids.append(value)

            select(data.get('default_model'))
            for item in raw_models:
                model_id = str(item or "").strip() if not isinstance(item, dict) else str(item.get('model_id') or "").strip()
                if model_id in explicit:
                    select(model_id)
            for model_id in explicit:
                select(model_id)
            if not selected_ids and raw_models:
                first = raw_models[0]
                select(first.get('model_id') if isinstance(first, dict) else first)

            migrated_models: list[ModelProfile] = []
            for model_id in selected_ids:
                profile = explicit.get(model_id) or ModelProfile.from_model_id(
                    model_id,
                    supports_vision=bool(supports_vision),
                    supports_reasoning=bool(supports_reasoning),
                )
                if legacy_request:
                    payload = profile.to_dict()
                    payload['request_overrides'] = {
                        **legacy_request,
                        **dict(payload.get('request_overrides') or {}),
                    }
                    profile = ModelProfile.from_dict(payload)
                migrated_models.append(profile)
            raw_models = migrated_models
        return cls(
            id=data.get('id', str(uuid.uuid4())),
            name=data.get('name', 'Provider'),
            api_type=data.get('api_type', DEFAULT_API_TYPE),
            api_base=data.get('api_base', 'https://api.openai.com/v1'),
            api_key=data.get('api_key', ''),
            models=raw_models,
            custom_headers=data.get('custom_headers', {}),
            supports_reasoning=supports_reasoning,
            supports_vision=supports_vision,
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
