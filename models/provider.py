"""LLM provider configuration model and canonical model identity helpers."""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple
import uuid
import json

OPENAI_COMPATIBLE = "openai"
ANTHROPIC_NATIVE = "anthropic"
SUPPORTED_API_TYPES = {OPENAI_COMPATIBLE, ANTHROPIC_NATIVE}


def normalize_provider_name(name: str) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return re.sub(r"-+", "-", normalized)


def split_model_ref(value: str) -> Tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    if "|" not in text:
        return "", text
    provider_name, model_name = text.split("|", 1)
    return normalize_provider_name(provider_name), str(model_name or "").strip()


def build_model_ref(provider_name: str, model_name: str) -> str:
    provider_token = normalize_provider_name(provider_name)
    model_token = str(model_name or "").strip()
    if provider_token and model_token:
        return f"{provider_token}|{model_token}"
    return model_token or provider_token


def provider_matches_name(provider: "Provider", provider_name: str) -> bool:
    return normalize_provider_name(getattr(provider, "name", "")) == normalize_provider_name(provider_name)


@dataclass
class Provider:
    """Represents an LLM provider configuration"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "New Provider"
    api_type: str = OPENAI_COMPATIBLE
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    models: List[str] = field(default_factory=list)
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
        api_type = str(getattr(self, "api_type", "") or "").strip().lower()
        self.api_type = api_type if api_type in SUPPORTED_API_TYPES else OPENAI_COMPATIBLE

    @property
    def canonical_name(self) -> str:
        return normalize_provider_name(self.name)

    @property
    def is_anthropic_native(self) -> bool:
        return self.api_type == ANTHROPIC_NATIVE

    def format_model_ref(self, model_name: str = "") -> str:
        return build_model_ref(self.name, model_name or self.default_model)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'id': self.id,
            'name': self.name,
            'api_type': self.api_type,
            'api_base': self.api_base,
            'api_key': self.api_key,
            'models': self.models,
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
            api_type=data.get('api_type', OPENAI_COMPATIBLE),
            api_base=data.get('api_base', 'https://api.openai.com/v1'),
            api_key=data.get('api_key', ''),
            models=data.get('models', []),
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
        return f"{base}/chat/completions"

    def get_models_endpoint(self) -> str:
        """Get the models list endpoint"""
        base = self.api_base.rstrip('/')
        return f"{base}/models"
