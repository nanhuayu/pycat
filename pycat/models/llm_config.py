from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Mapping

from pycat.models.provider import DEFAULT_API_TYPE, normalize_api_type

if TYPE_CHECKING:
    from pycat.models.provider import Provider


LLM_CONFIG_SCHEMA_VERSION = 3
_LEGACY_LLM_SETTING_KEYS = frozenset(
    {
        "api_type",
        "stream",
        "temperature",
        "top_p",
        "max_tokens",
        "reasoning_mode",
        "reasoning_enabled",
        "reasoning_effort",
        "system_prompt_override",
    }
)


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


def _coerce_positive_int(value: Any) -> int | None:
    number = _coerce_int(value)
    return number if number is not None and number > 0 else None


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

    Provider connectivity and model capabilities live outside this object.
    Reasoning codec/defaults belong to the selected ModelProfile.  This object
    only stores conversation request selection and the per-request output
    budget; old reasoning keys are read and discarded at the persistence edge.
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
    # Read-only migration value.  It is accepted in memory so old callers can
    # finish a run, but ``to_dict`` never writes it back to a conversation.
    reasoning_mode: str | None = field(default=None, repr=False, compare=False)
    system_prompt_override: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "LLMConfig":
        payload = dict(data or {})
        enabled = payload.get("reasoning_enabled")
        legacy_mode = str(payload.get("reasoning_mode") or payload.get("reasoning_effort") or "").strip().lower()
        if enabled is False:
            legacy_mode = "off"
        elif not legacy_mode and enabled is True:
            legacy_mode = "on"
        return cls(
            schema_version=LLM_CONFIG_SCHEMA_VERSION,
            provider_id=str(payload.get("provider_id") or "").strip(),
            provider_name=str(payload.get("provider_name") or "").strip(),
            api_type=_coerce_api_type(payload.get("api_type"), default=DEFAULT_API_TYPE),
            model=str(payload.get("model") or "").strip(),
            temperature=_coerce_float(payload.get("temperature")),
            top_p=_coerce_float(payload.get("top_p")),
            max_tokens=_coerce_positive_int(payload.get("max_tokens")),
            stream=_coerce_bool(payload.get("stream")),
            reasoning_mode=legacy_mode or None,
            system_prompt_override=str(payload.get("system_prompt_override") or "").strip(),
        )

    @classmethod
    def from_conversation(cls, conversation: Any) -> "LLMConfig":
        raw_llm_config = getattr(conversation, "llm_config", None)
        raw_config = raw_llm_config if isinstance(raw_llm_config, Mapping) else {}
        cfg = cls.from_dict(raw_config)
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
        if "api_type" not in raw_config and "api_type" in settings:
            updates["api_type"] = _coerce_api_type(settings.get("api_type"), default=cfg.api_type)
        if not cfg.model and model:
            updates["model"] = model
        if cfg.temperature is None and "temperature" in settings:
            updates["temperature"] = _coerce_float(settings.get("temperature"))
        if cfg.top_p is None and "top_p" in settings:
            updates["top_p"] = _coerce_float(settings.get("top_p"))
        if cfg.max_tokens is None and "max_tokens" in settings:
            updates["max_tokens"] = _coerce_positive_int(settings.get("max_tokens"))
        if cfg.stream is None and "stream" in settings:
            updates["stream"] = _coerce_bool(settings.get("stream"))
        if cfg.reasoning_mode is None:
            legacy_mode = settings.get("reasoning_mode") or settings.get("reasoning_effort")
            if legacy_mode not in (None, ""):
                updates["reasoning_mode"] = str(legacy_mode).strip().lower()
        if not cfg.system_prompt_override:
            override = str(settings.get("system_prompt_override") or "").strip()
            if override:
                updates["system_prompt_override"] = override

        return cfg.with_updates(**updates) if updates else cfg

    def with_updates(self, **updates: Any) -> "LLMConfig":
        allowed = {
            "provider_id",
            "provider_name",
            "api_type",
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "reasoning_mode",
            "system_prompt_override",
        }
        clean_updates = {key: value for key, value in updates.items() if key in allowed}
        if "max_tokens" in clean_updates:
            clean_updates["max_tokens"] = _coerce_positive_int(clean_updates["max_tokens"])
        if "reasoning_mode" in clean_updates:
            mode = str(clean_updates["reasoning_mode"] or "").strip().lower()
            clean_updates["reasoning_mode"] = mode or None
        return replace(self, **clean_updates)

    def resolved_model(self) -> str:
        return str(self.model or "").strip()

    def resolved_api_type(self, provider: "Provider | None" = None) -> str:
        api_type = _coerce_api_type(self.api_type, default="")
        if api_type:
            return api_type
        if provider is not None:
            return _coerce_api_type(getattr(provider, "api_type", DEFAULT_API_TYPE))
        return DEFAULT_API_TYPE

    def resolved_stream(self, default: bool = True) -> bool:
        return self.stream if isinstance(self.stream, bool) else bool(default)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": LLM_CONFIG_SCHEMA_VERSION,
            "api_type": self.resolved_api_type(),
        }
        for key in ("provider_id", "provider_name", "model"):
            value = str(getattr(self, key) or "").strip()
            if value:
                payload[key] = value
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
        return payload

    def apply_to_conversation(self, conversation: Any) -> None:
        conversation.llm_config = self.to_dict()
        conversation.provider_id = self.provider_id
        conversation.provider_name = self.provider_name
        conversation.model = self.model

        settings = dict(getattr(conversation, "settings", {}) or {})
        for key in _LEGACY_LLM_SETTING_KEYS:
            settings.pop(key, None)
        conversation.settings = settings
