from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, TYPE_CHECKING

from models.provider import DEFAULT_API_TYPE, normalize_api_type

if TYPE_CHECKING:
    from models.provider import Provider


LLM_CONFIG_SCHEMA_VERSION = 1
_LEGACY_LLM_SETTING_KEYS = frozenset({
    "api_type",
    "stream",
    "temperature",
    "top_p",
    "max_tokens",
    "system_prompt_override",
})


def _coerce_api_type(value: Any, default: str = DEFAULT_API_TYPE) -> str:
    if default == "" and (value is None or str(value or "").strip() == ""):
        return ""
    return normalize_api_type(value, default=default)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    try:
        return bool(value)
    except Exception:
        return None


@dataclass(frozen=True)
class LLMConfig:
    """Normalized per-conversation LLM request configuration.

    This is the new runtime-facing source of truth for provider/model and
    generation options. Legacy `Conversation.settings` keys can still be
    projected from it during the transition.
    """

    schema_version: int = LLM_CONFIG_SCHEMA_VERSION
    provider_id: str = ""
    provider_name: str = ""
    api_type: str = DEFAULT_API_TYPE
    model: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool | None = None
    system_prompt_override: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "LLMConfig":
        payload = dict(data or {})
        extras = payload.get("extras") if isinstance(payload.get("extras"), Mapping) else {}
        recognized = {
            "schema_version",
            "provider_id",
            "provider_name",
            "api_type",
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "system_prompt_override",
            "extras",
        }
        merged_extras = dict(extras)
        for key, value in payload.items():
            if key not in recognized:
                merged_extras.setdefault(str(key), value)

        schema_version = _coerce_int(payload.get("schema_version")) or LLM_CONFIG_SCHEMA_VERSION
        return cls(
            schema_version=max(1, schema_version),
            provider_id=str(payload.get("provider_id") or "").strip(),
            provider_name=str(payload.get("provider_name") or "").strip(),
            api_type=_coerce_api_type(payload.get("api_type"), default=DEFAULT_API_TYPE),
            model=str(payload.get("model") or "").strip(),
            temperature=_coerce_float(payload.get("temperature")),
            top_p=_coerce_float(payload.get("top_p")),
            max_tokens=_coerce_int(payload.get("max_tokens")),
            stream=_coerce_bool(payload.get("stream")),
            system_prompt_override=str(payload.get("system_prompt_override") or "").strip(),
            extras=merged_extras,
        )

    @classmethod
    def from_conversation(cls, conversation: Any) -> "LLMConfig":
        raw_llm_config = getattr(conversation, "llm_config", None)
        cfg = cls.from_dict(raw_llm_config if isinstance(raw_llm_config, Mapping) else None)
        raw_has_api_type = isinstance(raw_llm_config, Mapping) and "api_type" in raw_llm_config
        settings = getattr(conversation, "settings", {}) or {}
        if not isinstance(settings, Mapping):
            settings = {}

        updates: dict[str, Any] = {}
        provider_id = str(getattr(conversation, "provider_id", "") or "").strip()
        provider_name = str(getattr(conversation, "provider_name", "") or "").strip()
        model = str(getattr(conversation, "model", "") or "").strip()

        if not cfg.provider_id and provider_id:
            updates["provider_id"] = provider_id
        if not cfg.provider_name and provider_name:
            updates["provider_name"] = provider_name
        if not raw_has_api_type and "api_type" in settings:
            updates["api_type"] = _coerce_api_type(settings.get("api_type"), default=cfg.api_type)
        if not cfg.model and model:
            updates["model"] = model
        if cfg.temperature is None and "temperature" in settings:
            updates["temperature"] = _coerce_float(settings.get("temperature"))
        if cfg.top_p is None and "top_p" in settings:
            updates["top_p"] = _coerce_float(settings.get("top_p"))
        if cfg.max_tokens is None and "max_tokens" in settings:
            updates["max_tokens"] = _coerce_int(settings.get("max_tokens"))
        if cfg.stream is None and "stream" in settings:
            updates["stream"] = _coerce_bool(settings.get("stream"))
        if not cfg.system_prompt_override:
            override = str(settings.get("system_prompt_override") or "").strip()
            if override:
                updates["system_prompt_override"] = override

        if updates:
            cfg = cfg.with_updates(**updates)
        return cfg

    def with_updates(self, **updates: Any) -> "LLMConfig":
        clean_updates = {key: value for key, value in updates.items() if key in {
            "schema_version",
            "provider_id",
            "provider_name",
            "api_type",
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "system_prompt_override",
            "extras",
        }}
        return replace(self, **clean_updates)

    def resolved_model(self, provider: "Provider | None" = None) -> str:
        if self.model:
            return self.model
        if provider is not None:
            return str(getattr(provider, "default_model", "") or "").strip()
        return ""

    def resolved_api_type(self, provider: "Provider | None" = None) -> str:
        api_type = _coerce_api_type(self.api_type, default="")
        if api_type:
            return api_type
        if provider is not None:
            return _coerce_api_type(getattr(provider, "api_type", DEFAULT_API_TYPE))
        return DEFAULT_API_TYPE

    def resolved_stream(self, default: bool = True) -> bool:
        if isinstance(self.stream, bool):
            return self.stream
        return bool(default)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": max(1, int(self.schema_version or LLM_CONFIG_SCHEMA_VERSION)),
        }
        if self.provider_id:
            payload["provider_id"] = self.provider_id
        if self.provider_name:
            payload["provider_name"] = self.provider_name
        payload["api_type"] = self.resolved_api_type()
        if self.model:
            payload["model"] = self.model
        if self.temperature is not None:
            payload["temperature"] = float(self.temperature)
        if self.top_p is not None:
            payload["top_p"] = float(self.top_p)
        if self.max_tokens is not None:
            payload["max_tokens"] = int(self.max_tokens)
        if self.stream is not None:
            payload["stream"] = bool(self.stream)
        if self.system_prompt_override:
            payload["system_prompt_override"] = self.system_prompt_override
        if self.extras:
            payload["extras"] = dict(self.extras)
        return payload

    def apply_to_conversation(self, conversation: Any) -> None:
        conversation.llm_config = self.to_dict()
        conversation.provider_id = self.provider_id
        conversation.provider_name = self.provider_name
        conversation.model = self.model

        settings = dict(getattr(conversation, "settings", {}) or {})
        for key in _LEGACY_LLM_SETTING_KEYS:
            settings.pop(key, None)

        if self.temperature is not None:
            settings["temperature"] = float(self.temperature)
        if self.top_p is not None:
            settings["top_p"] = float(self.top_p)
        if self.max_tokens is not None and int(self.max_tokens) > 0:
            settings["max_tokens"] = int(self.max_tokens)
        if self.stream is not None:
            settings["stream"] = bool(self.stream)
        if self.system_prompt_override:
            settings["system_prompt_override"] = self.system_prompt_override
        settings["api_type"] = self.resolved_api_type()

        conversation.settings = settings
