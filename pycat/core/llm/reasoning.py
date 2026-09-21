"""Canonical reasoning modes and the small set of supported wire codecs.

OpenRouter uses the OpenAI-compatible Chat Completions envelope, but its
reasoning controls are nested under ``reasoning`` and its tool continuation is
carried in ``reasoning_details``.  That is a wire *variant*, not a second
reasoning owner.  The ``chat_reasoning`` codec below owns both shapes and gets
the route marker from the Provider at the narrow request boundary.
"""
from __future__ import annotations

import logging
from typing import Any, MutableMapping

from pycat.models.model_profile import REASONING_CODEC_ALIASES, ModelProfile

logger = logging.getLogger(__name__)
CHAT_REASONING_CODEC = "chat_reasoning"
_BOOLEAN_MODES = {"on", "auto"}


def normalize_reasoning_codec(value: Any) -> str:
    """Return a canonical codec id; unknown values fail closed to ``none``."""

    codec = str(value or "none").strip().lower()
    codec = REASONING_CODEC_ALIASES.get(codec, codec)
    return codec if codec in {
        "none",
        "responses_effort",
        CHAT_REASONING_CODEC,
        "chat_thinking_effort",
        "chat_toggle_budget",
        "anthropic_adaptive",
        "ollama_think",
    } else "none"


def resolve_reasoning_mode(profile: ModelProfile, override: str | None) -> str | None:
    """Resolve one valid mode; invalid or unsupported values fail closed."""

    codec = normalize_reasoning_codec(profile.reasoning_codec)
    if not profile.supports_reasoning or codec == "none":
        return None
    candidate = str(
        override if override not in (None, "") else profile.reasoning_default or "inherit"
    ).strip().lower()
    if candidate == "none":
        candidate = "off"
    if candidate in {"", "inherit", "default"}:
        return None
    if candidate not in set(profile.reasoning_options or ()):
        logger.warning(
            "忽略模型 %s 不支持的推理模式: %s",
            profile.model_id or "<unknown>",
            candidate,
        )
        return None
    return candidate


def apply_reasoning(
    body: MutableMapping[str, Any],
    *,
    profile: ModelProfile,
    mode: str | None,
    openrouter: bool = False,
) -> None:
    """Apply a profile's canonical mode to a request draft.

    ``openrouter`` is intentionally a boolean route marker supplied by the
    Provider adapter.  It keeps the OpenRouter nested wire shape without
    creating a Provider-specific codec or leaking provider-name checks through
    the rest of the request builder.
    """

    selected = resolve_reasoning_mode(profile, mode)
    if selected is None:
        return

    codec = normalize_reasoning_codec(profile.reasoning_codec)

    if codec == "responses_effort":
        if selected not in _BOOLEAN_MODES:
            body["reasoning"] = {"effort": "none" if selected == "off" else selected}
        return

    if codec == CHAT_REASONING_CODEC:
        if openrouter:
            if selected == "off":
                body["reasoning"] = {"enabled": False}
            elif selected in _BOOLEAN_MODES:
                body["reasoning"] = {"enabled": True}
            else:
                body["reasoning"] = {"effort": selected}
        elif selected not in _BOOLEAN_MODES:
            body["reasoning_effort"] = "none" if selected == "off" else selected
        return

    if codec == "chat_thinking_effort":
        body["thinking"] = {"type": "disabled" if selected == "off" else "enabled"}
        if selected not in {"off", *_BOOLEAN_MODES}:
            body["reasoning_effort"] = selected
        return

    if codec == "chat_toggle_budget":
        # Provider-private ``thinking_budget`` belongs in extra_body.  The
        # shared codec only emits the boolean switch.
        body["enable_thinking"] = selected != "off"
        return

    if codec == "anthropic_adaptive":
        body["thinking"] = {"type": "disabled" if selected == "off" else "adaptive"}
        if selected not in {"off", *_BOOLEAN_MODES}:
            body["output_config"] = {"effort": selected}
        return

    if codec == "ollama_think":
        if selected == "off":
            body["think"] = False
        elif selected in _BOOLEAN_MODES:
            body["think"] = True
        else:
            body["think"] = selected
