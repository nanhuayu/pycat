"""LLM provider/service connection configuration model."""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from models.model_profile import ModelProfile
from models.model_ref import (
    build_model_ref,
    normalize_provider_name,
)

logger = logging.getLogger(__name__)

# Model profiles may add endpoint-specific headers, but must not replace the
# transport/authentication headers assembled by Provider.  Provider-level
# custom headers remain the explicit gateway escape hatch.
_MODEL_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "anthropic-version",
    }
)

OPENAI_COMPATIBLE = "openai_compatible"
OPENAI_RESPONSES = "openai_responses"
ANTHROPIC_MESSAGES = "anthropic_messages"
OLLAMA_CHAT = "ollama_chat"

ANTHROPIC_NATIVE = ANTHROPIC_MESSAGES
DEFAULT_API_TYPE = OPENAI_COMPATIBLE
PROVIDER_SCHEMA_VERSION = 6

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
    catalog_key: str = ""
    name: str = "New Provider"
    api_type: str = DEFAULT_API_TYPE
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    models: List[ModelProfile] = field(default_factory=list)
    custom_headers: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        self.normalize_inplace()

    def normalize_inplace(self) -> None:
        self.catalog_key = str(getattr(self, "catalog_key", "") or "").strip().lower()
        self.name = normalize_provider_name(self.name)
        self.api_type = normalize_api_type(getattr(self, "api_type", DEFAULT_API_TYPE))
        self.custom_headers = self._normalize_headers(getattr(self, "custom_headers", {}) or {})
        self.models = self._normalize_models(getattr(self, "models", []) or [])

    @staticmethod
    def _normalize_headers(value: Any) -> Dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key).strip(): str(item or "").strip()
            for key, item in value.items()
            if str(key or "").strip()
        }

    def _normalize_models(self, values: Iterable[Any]) -> List[ModelProfile]:
        out: List[ModelProfile] = []
        seen: set[str] = set()
        for value in values or []:
            if isinstance(value, ModelProfile):
                profile = ModelProfile.from_dict(value.to_dict())
            elif isinstance(value, dict):
                profile = ModelProfile.from_dict(value)
            else:
                profile = ModelProfile.from_model_id(str(value or "").strip())
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
    def is_openrouter_route(self) -> bool:
        """Whether this Provider uses OpenRouter's Chat wire variant.

        OpenRouter is a route marker, not an API type.  Keep the envelope
        check here so discovery, request construction and GUI codec filtering
        cannot silently disagree about a provider named ``openrouter``.
        """

        return self.is_chat_completions_like and (
            self.catalog_key == "openrouter" or self.canonical_name == "openrouter"
        )

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
        return ModelProfile.from_model_id(target)

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
            'catalog_key': self.catalog_key,
            'name': self.name,
            'api_type': self.api_type,
            'api_base': self.api_base,
            'api_key': self.api_key,
            'models': [profile.to_dict() for profile in self.models],
            'custom_headers': self.custom_headers,
            'enabled': self.enabled
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Provider':
        """Create from dictionary"""
        raw_models = list(data.get('models', []) or [])
        return cls(
            id=data.get('id', str(uuid.uuid4())),
            catalog_key=data.get('catalog_key', ''),
            name=data.get('name', 'Provider'),
            api_type=data.get('api_type', DEFAULT_API_TYPE),
            api_base=data.get('api_base', 'https://api.openai.com/v1'),
            api_key=data.get('api_key', ''),
            models=raw_models,
            custom_headers=data.get('custom_headers', {}),
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

    def get_headers(self, model_id: str = "") -> Dict[str, str]:
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
        profile = self.find_model_profile(model_id)
        if profile is not None:
            for key, value in profile.custom_headers.items():
                if str(key).strip().lower() in _MODEL_RESERVED_HEADERS:
                    logger.warning("忽略模型级保留请求头: %s", key)
                    continue
                headers[key] = value
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
