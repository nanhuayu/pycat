"""Canonical reasoning modes and the small set of supported wire codecs.

OpenRouter uses the OpenAI-compatible Chat Completions envelope, but its
reasoning controls are nested under ``reasoning`` and its tool continuation is
carried in ``reasoning_details``.  That is a wire *variant*, not a second
reasoning owner.  The ``chat_reasoning`` codec below owns both shapes and gets
the route marker from the Provider at the narrow request boundary.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, MutableMapping

from pycat.models.model_profile import REASONING_CODEC_ALIASES, ModelProfile

logger = logging.getLogger(__name__)
CHAT_REASONING_CODEC = "chat_reasoning"
_BOOLEAN_MODES = {"on", "auto"}
_CONTROL_PATHS = (
    ("reasoning_effort",), ("reasoning", "effort"), ("reasoning", "enabled"), ("output_config", "effort"),
    ("thinking",), ("enable_thinking",), ("think",),
)


def capability_reasoning_mode(profile: ModelProfile, *, prefer_low: bool = False) -> str:
    """Prefer light reasoning for fixed transformations without guessing support."""
    if profile.supports_reasoning and normalize_reasoning_codec(profile.reasoning_codec) != "none":
        for mode in (("low", "off", "minimal") if prefer_low else ("off", "low", "minimal")):
            if mode in profile.reasoning_options:
                return mode
    # Explicit inherit bypasses a model's saved high/max default for this helper.
    # It does not claim that server-side thinking is disabled.
    return "inherit"


def rejected_reasoning_parameters(
    body: dict[str, Any], content: str, metadata: dict[str, Any],
) -> tuple[tuple[str, ...], ...]:
    """Identify controls explicitly rejected by HTTP validation, never by model prose."""
    if not metadata.get("runtime_error") or metadata.get("http_status") not in {400, 422}:
        return ()
    error: Any = None
    try:
        error = json.loads(content[content.index("{"):])
    except (ValueError, TypeError):
        pass
    if isinstance(error, dict):
        error = error.get("error") if isinstance(error.get("error"), dict) else error
        error = error.get("extError") if isinstance(error.get("extError"), dict) else error
        param = str(error.get("param") or "").casefold()
        detail = " ".join(str(error.get(key) or "") for key in ("code", "message", "msg"))
    else:
        param, detail = "", content
    if not re.search(r"unsupported|unknown|unrecogni[sz]ed|unexpected|not (?:supported|allowed|permitted)"
                     r"|does not support|invalid (?:parameter|argument|value)", detail, re.IGNORECASE):
        return ()
    if not param:
        # Require a named rejected field, not a mention of reasoning somewhere
        # in an unrelated validation error.
        for pattern in (
            r"(?:unsupported|unknown|unrecogni[sz]ed|unexpected|invalid)\s+(?:parameter|argument|field)\s*:?\s*[`'\"]?([\w.]+)",
            r"[`'\"]?([\w.]+)[`'\"]?\s+(?:is\s+)?(?:not supported|not allowed|not permitted|does not support|unsupported)",
        ):
            match = re.search(pattern, detail, re.IGNORECASE)
            if match:
                param = match[1].casefold()
                break
    rejected = []
    for path in _CONTROL_PATHS:
        parent = body if len(path) == 1 else body.get(path[0])
        if not isinstance(parent, dict) or path[-1] not in parent:
            continue
        if param in {".".join(path), path[0]}:
            rejected.append((path[0],) if param == path[0] else path)
    return tuple(dict.fromkeys(rejected))


def omit_reasoning_parameters(body: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
    """Drop only rejected fields, preserving other nested reasoning options."""
    if not paths:
        return body
    result = copy.deepcopy(body)
    for path in paths:
        parent = result if len(path) == 1 else result.get(path[0])
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
            if len(path) > 1 and not parent:
                result.pop(path[0], None)
    return result


def reasoning_parameters_signature(body: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """A rejected value such as 'none' must not suppress a supported 'low'."""
    return tuple((key, json.dumps(body[key], sort_keys=True))
                 for key in dict.fromkeys(path[0] for path in _CONTROL_PATHS) if key in body)


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
