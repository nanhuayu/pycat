"""The small, provider-scoped model profile used by the runtime and GUI.

The profile deliberately describes the *actual* model id accepted by the
selected endpoint.  It is not a second model registry: discovery metadata that
is not consumed by the request/response pipeline is discarded at this boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from pycat.models.coercion import as_bool, optional_bool

MODEL_INPUT_MODALITIES = frozenset({"text", "image", "audio"})
BUNDLED_MODEL_TAG = "bundled"
REASONING_CODECS = frozenset(
    {
        "none",
        "responses_effort",
        "chat_reasoning",
        "chat_thinking_effort",
        "chat_toggle_budget",
        "anthropic_adaptive",
        "ollama_think",
    }
)
# These values were written by the first model-profile prototype.  Read them
# once and immediately normalize them; no new profile is allowed to serialize
# a provider-specific OpenRouter codec or the generic reasoning budget codec.
REASONING_CODEC_ALIASES = {
    "chat_effort": "chat_reasoning",
    "openrouter_reasoning": "chat_reasoning",
    "anthropic_budget": "none",
}
REASONING_MODES = (
    "inherit",
    "off",
    "on",
    "auto",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
DEFAULT_REASONING_OPTIONS = {
    "responses_effort": ("inherit", "off", "low", "medium", "high", "xhigh", "max", "ultra"),
    "chat_reasoning": ("inherit", "off", "low", "medium", "high", "xhigh", "max", "ultra"),
    "chat_thinking_effort": ("inherit", "off", "low", "medium", "high", "max"),
    "chat_toggle_budget": ("inherit", "off", "on"),
    "anthropic_adaptive": ("inherit", "off", "low", "medium", "high", "max"),
    "ollama_think": ("inherit", "off", "on", "low", "medium", "high"),
}


def reasoning_codecs_for_provider(api_type: Any, catalog_key: Any = "") -> tuple[str, ...]:
    """Return the protocol codec whitelist for an endpoint envelope.

    ``openrouter`` is a route/catalog marker over the OpenAI-compatible Chat
    envelope, not another API type.  Keeping this table in the model contract
    lets GUI, catalog validation and future CLI editors share one source.
    """

    normalized_api = str(api_type or "openai_compatible").strip().lower()
    normalized_catalog = str(catalog_key or "").strip().lower()
    if normalized_api == "openai_compatible" and normalized_catalog == "openrouter":
        return ("none", "chat_reasoning")
    return {
        "openai_responses": ("none", "responses_effort"),
        "anthropic_messages": ("none", "anthropic_adaptive"),
        "openai_compatible": (
            "none",
            "chat_reasoning",
            "chat_thinking_effort",
            "chat_toggle_budget",
        ),
        "ollama_chat": ("none", "ollama_think"),
    }.get(normalized_api, ("none",))


def recommended_reasoning_codec(api_type: str, catalog_key: str = '') -> str:
    """Editor recommendation only; never rewrites an existing model profile."""
    if api_type == 'openai_compatible':
        return 'chat_reasoning' if catalog_key == 'openrouter' else 'chat_thinking_effort'
    return {'openai_responses': 'responses_effort', 'anthropic_messages': 'anthropic_adaptive',
            'ollama_chat': 'ollama_think'}.get(api_type, 'none')


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _coerce_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _coerce_headers(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    headers: Dict[str, str] = {}
    for key, item in value.items():
        name = str(key or "").strip()
        content = str(item or "").strip()
        if not name:
            continue
        if "\r" in name or "\n" in name or "\r" in content or "\n" in content:
            raise ValueError("Header names and values cannot contain CR or LF")
        headers[name] = content
    return headers


def _coerce_string_list(values: Any, *, allowed: Iterable[str] | None = None) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    allowed_values = set(allowed or ())
    result: List[str] = []
    for value in values:
        item = str(value or "").strip().lower()
        if not item or (allowed_values and item not in allowed_values) or item in result:
            continue
        result.append(item)
    return result


def _coerce_json_object(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"extra_body must be JSON serializable: {exc}") from exc
    return dict(value)


def _legacy_reasoning_mode(enabled: Any, effort: Any) -> str:
    normalized_enabled = optional_bool(enabled)
    normalized_effort = str(effort or "").strip().lower()
    if normalized_effort == "none":
        normalized_effort = "off"
    if normalized_enabled is False:
        return "off"
    if normalized_effort in REASONING_MODES and normalized_effort != "inherit":
        return normalized_effort
    if normalized_enabled is True:
        return "on"
    return "inherit"


@dataclass
class ModelProfile:
    """One provider-scoped model profile used by both GUI and runtime."""

    model_id: str = ""
    model_type: str = ""
    display_name: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    supports_reasoning: bool = True
    input_modalities: List[str] = field(default_factory=lambda: ["text"])
    reasoning_codec: str = "none"
    reasoning_options: List[str] = field(default_factory=lambda: ["inherit"])
    reasoning_default: str = "inherit"
    default_temperature: float | None = None
    default_top_p: float | None = None
    custom_headers: Dict[str, str] = field(default_factory=dict)
    extra_body: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    source_url: str = ""
    verified_at: str = ""

    def __post_init__(self) -> None:
        self.model_id = str(self.model_id or "").strip()
        self.model_type = str(self.model_type or ("image" if self.model_id.startswith(("gpt-image-", "chatgpt-image-")) else "chat"))
        if self.model_type not in {"chat", "image"}:
            self.model_type = "chat"
        if self.model_type == "image":
            self.supports_tools = False
            self.supports_reasoning = False
            self.input_modalities = ["text", "image"]
        self.display_name = str(self.display_name or self.model_id).strip()
        self.context_window = _coerce_optional_int(self.context_window)
        self.max_output_tokens = _coerce_optional_int(self.max_output_tokens)
        self.supports_tools = as_bool(self.supports_tools, True)
        self.supports_reasoning = as_bool(self.supports_reasoning, True)

        self.input_modalities = _coerce_string_list(
            self.input_modalities,
            allowed=MODEL_INPUT_MODALITIES,
        ) or ["text"]

        codec = str(self.reasoning_codec or "none").strip().lower()
        codec = REASONING_CODEC_ALIASES.get(codec, codec)
        self.reasoning_codec = codec if codec in REASONING_CODECS else "none"
        raw_options = self.reasoning_options
        if isinstance(raw_options, str):
            raw_options = [raw_options]
        if isinstance(raw_options, (list, tuple, set)):
            raw_options = ["off" if str(item or "").strip().lower() == "none" else item for item in raw_options]
        options = _coerce_string_list(raw_options, allowed=REASONING_MODES)
        if not options:
            options = ["inherit"]
        if "inherit" not in options:
            options.insert(0, "inherit")

        explicit_default = str(self.reasoning_default or "").strip().lower()
        if explicit_default == "none":
            explicit_default = "off"
        self.reasoning_default = explicit_default if explicit_default in REASONING_MODES else "inherit"
        if self.reasoning_default not in options:
            self.reasoning_default = "inherit"
        if not self.supports_reasoning or self.reasoning_codec == "none":
            self.reasoning_codec = "none"
            options = ["inherit"]
            self.reasoning_default = "inherit"
        self.reasoning_options = options

        self.default_temperature = _coerce_optional_float(self.default_temperature)
        self.default_top_p = _coerce_optional_float(self.default_top_p)
        self.custom_headers = _coerce_headers(self.custom_headers)
        self.extra_body = _coerce_json_object(self.extra_body)
        self.tags = _coerce_string_list(self.tags)
        self.notes = str(self.notes or "").strip()
        self.source_url = str(self.source_url or "").strip()
        self.verified_at = str(self.verified_at or "").strip()

    @classmethod
    def from_model_id(
        cls,
        model_id: str,
        *,
        supports_tools: bool = True,
        supports_reasoning: bool = True,
        input_modalities: Iterable[str] | None = None,
    ) -> "ModelProfile":
        return cls(
            model_id=model_id,
            display_name=model_id,
            supports_tools=supports_tools,
            supports_reasoning=supports_reasoning,
            input_modalities=list(input_modalities or ["text"]),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any] | str | None) -> "ModelProfile":
        if isinstance(data, str):
            return cls.from_model_id(data)
        payload = dict(data or {})
        has_modalities = "input_modalities" in payload
        modalities = list(payload.get("input_modalities") or ["text"]) if has_modalities else ["text"]
        if not has_modalities and optional_bool(payload.get("supports_vision")) is True:
            modalities.append("image")
        reasoning_default = str(payload.get("reasoning_default") or "").strip().lower()
        if reasoning_default == "none":
            reasoning_default = "off"
        if reasoning_default not in REASONING_MODES:
            reasoning_default = _legacy_reasoning_mode(
                payload.get("reasoning_enabled"),
                payload.get("reasoning_effort") or payload.get("effort"),
            )
        if "extra_body" in payload:
            extra_body = payload.get("extra_body")
        else:
            extra_body = payload.get("request_overrides") or payload.get("request_format") or {}
        # ``wire_model_id`` was the old logical/transport split.  A one-time
        # read migration prefers the actual transport id and then drops the
        # legacy key from the serialized profile.
        legacy_wire_id = str(payload.get("wire_model_id") or "").strip()
        model_id = legacy_wire_id or payload.get("model_id") or payload.get("id") or payload.get("name") or ""
        legacy_codec = str(payload.get("reasoning_codec") or "none").strip().lower()
        codec = REASONING_CODEC_ALIASES.get(legacy_codec, legacy_codec)
        return cls(
            model_id=model_id,
            display_name=payload.get("display_name") or payload.get("label") or "",
            model_type=str(payload.get("model_type") or ""),
            context_window=payload.get("context_window"),
            max_output_tokens=payload.get("max_output_tokens"),
            supports_tools=payload.get("supports_tools", True),
            supports_reasoning=payload.get("supports_reasoning", True),
            input_modalities=modalities,
            reasoning_codec=codec,
            reasoning_options=payload.get("reasoning_options") or ["inherit"],
            reasoning_default=reasoning_default,
            default_temperature=payload.get("default_temperature"),
            default_top_p=payload.get("default_top_p"),
            custom_headers=payload.get("custom_headers") or {},
            extra_body=extra_body or {},
            tags=payload.get("tags") or [],
            notes=payload.get("notes") or "",
            source_url=payload.get("source_url") or "",
            verified_at=payload.get("verified_at") or "",
        )

    def supports_input(self, modality: str) -> bool:
        return str(modality or "").strip().lower() in self.input_modalities

    def as_user_managed(self) -> "ModelProfile":
        """Return a copy that remote discovery must no longer overwrite."""

        payload = self.to_dict()
        payload["tags"] = [tag for tag in self.tags if tag != BUNDLED_MODEL_TAG]
        return ModelProfile.from_dict(payload)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_type": self.model_type,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "input_modalities": list(self.input_modalities),
            "reasoning_codec": self.reasoning_codec,
            "reasoning_options": list(self.reasoning_options),
            "reasoning_default": self.reasoning_default,
            "default_temperature": self.default_temperature,
            "default_top_p": self.default_top_p,
            "custom_headers": dict(self.custom_headers),
            "extra_body": dict(self.extra_body),
            "tags": list(self.tags),
            "notes": self.notes,
            "source_url": self.source_url,
            "verified_at": self.verified_at,
        }
