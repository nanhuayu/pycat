"""One-way persisted configuration migration to the v5 runtime contracts."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from models.contracts.config import DEFAULT_ACCENT, SUPPORTED_ACCENTS
from models.contracts.tooling import TOOL_CATEGORIES


SCHEMA_VERSION = 5

_CATEGORY_MAP = {
    "read": "read",
    "search": "web",
    "web": "web",
    "edit": "edit",
    "command": "execute",
    "execute": "execute",
    "manage": "state",
    "state": "state",
    "mode": "state",
    "control": "state",
    "workflow": "state",
    "delegate": "delegate",
    "extension": "capability",
    "misc": "capability",
    "capability": "capability",
    "mcp": "mcp",
}

_TOOL_NAME_MAP = {
    "content__list": "archive__list",
    "content__read": "archive__read",
    "shell__status": "shell__read",
    "shell__logs": "shell__read",
    "shell__wait": "shell__read",
}

_REMOVED_TOOLS = {
    "agent__switch",
    "capability__translate",
    "capability__extract_facts",
    "capability__classify_risk",
    "capability__rewrite_query",
}

_REMOVED_CAPABILITIES = {
    "translate",
    "extract_facts",
    "classify_risk",
    "rewrite_query",
}

_SUBAGENT_MODE_SLUGS = {"explore", "search", "read_analyze"}
_BOTH_MODE_SLUGS = {"review"}
_SELECTED_ARTIFACT_MODE_SLUGS = {"read_analyze", "review"}


def migrate_category(value: Any) -> str:
    return _CATEGORY_MAP.get(str(value or "").strip().lower(), "capability")


def migrate_tool_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or name in _REMOVED_TOOLS:
        return ""
    return _TOOL_NAME_MAP.get(name, name)


def migrate_tool_selection_payload(data: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(data) if isinstance(data, Mapping) else {}
    categories = payload.get("allowed_categories")
    tools = payload.get("allowed_tools")
    sources = payload.get("allowed_sources")
    return {
        "allowed_categories": sorted({migrate_category(item) for item in categories or ()})
        if categories is not None
        else None,
        "allowed_tools": sorted({name for item in tools or () if (name := migrate_tool_name(item))})
        if tools is not None
        else None,
        "allowed_sources": sorted({str(item).strip() for item in sources or () if str(item).strip()})
        if sources is not None
        else None,
        "require_available": bool(payload.get("require_available", True)),
    }


def _policy(data: Any) -> dict[str, bool]:
    payload = dict(data) if isinstance(data, Mapping) else {}
    if "action" in payload:
        # Already-migrated tri-state payload; keeps re-migration idempotent.
        return {
            "deny": {"enabled": False, "auto_approve": False},
            "allow": {"enabled": True, "auto_approve": True},
        }.get(str(payload.get("action") or "").strip().lower(), {"enabled": True, "auto_approve": False})
    return {
        "enabled": bool(payload.get("enabled", True)),
        "auto_approve": bool(payload.get("auto_approve", False)),
    }


def _most_restrictive(policies: list[dict[str, bool]]) -> dict[str, bool]:
    if not policies:
        return {"enabled": True, "auto_approve": False}
    return {
        "enabled": all(item["enabled"] for item in policies),
        "auto_approve": all(item["auto_approve"] for item in policies),
    }


def _bool_policy_to_action(policy: Mapping[str, Any]) -> str:
    if not bool(policy.get("enabled", True)):
        return "deny"
    return "allow" if bool(policy.get("auto_approve", False)) else "ask"


def migrate_permissions_payload(data: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(data) if isinstance(data, Mapping) else {}
    defaults: dict[str, dict[str, bool]] = {}
    raw_defaults = payload.get("category_defaults")
    if isinstance(raw_defaults, Mapping):
        grouped: dict[str, list[dict[str, bool]]] = {}
        for category, value in raw_defaults.items():
            grouped.setdefault(migrate_category(category), []).append(_policy(value))
        defaults = {category: _most_restrictive(values) for category, values in grouped.items()}

    grouped_tools: dict[str, list[dict[str, bool]]] = {}
    raw_tools = payload.get("tools")
    if isinstance(raw_tools, Mapping):
        for tool_name, value in raw_tools.items():
            migrated_name = migrate_tool_name(tool_name)
            if migrated_name:
                grouped_tools.setdefault(migrated_name, []).append(_policy(value))
    tools = {name: _most_restrictive(values) for name, values in grouped_tools.items()}
    raw_mode = str(payload.get("approval_mode") or "").strip().lower()
    if raw_mode in {"all_confirm", "confirm_all", "manual", "ask", "always_ask"}:
        # Legacy "confirm everything" intent materializes as an all-ask table.
        raw_mode = "custom"
        for category in TOOL_CATEGORIES:
            existing = defaults.get(category) or {}
            defaults[category] = {"enabled": bool(existing.get("enabled", True)), "auto_approve": False}
    if raw_mode not in {"standard", "developer_trust", "allow_all", "custom"}:
        values = list(defaults.values()) + list(tools.values())
        if values and all(item["enabled"] and item["auto_approve"] for item in values):
            raw_mode = "developer_trust"
        elif values and all(item["enabled"] and not item["auto_approve"] for item in values):
            raw_mode = "custom"
        else:
            raw_mode = "standard" if not tools else "custom"
    if raw_mode in {"developer_trust", "allow_all"}:
        # Materialize the legacy preset labels into concrete rules so the
        # tri-state custom table keeps the same behavior.
        for category in TOOL_CATEGORIES:
            existing = defaults.get(category) or {}
            defaults[category] = {"enabled": bool(existing.get("enabled", True)), "auto_approve": True}
    if raw_mode == "allow_all":
        # Under the legacy runtime allow_all short-circuited every approval, so
        # per-tool auto_approve=False entries are dead config that would now
        # re-introduce prompts. Drop them; keep real disables (enabled=False).
        tools = {name: policy for name, policy in tools.items() if not policy["enabled"]}
    return {
        "category_defaults": {
            category: {"action": _bool_policy_to_action(policy)} for category, policy in defaults.items()
        },
        "tools": {name: {"action": _bool_policy_to_action(policy)} for name, policy in tools.items()},
    }


def migrate_capabilities_payload(data: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = dict(data) if isinstance(data, Mapping) else {}
    capabilities: list[dict[str, Any]] = []
    for raw in payload.get("capabilities") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        capability_id = str(item.get("id") or item.get("slug") or item.get("kind") or "").strip().lower()
        aliases = {"context_compress": "compress", "title_extract": "title", "summarize_text": "summarize"}
        capability_id = aliases.get(capability_id, capability_id)
        if not capability_id or capability_id in _REMOVED_CAPABILITIES:
            continue

        categories = [migrate_category(value) for value in item.get("allowed_tool_categories") or []]
        raw_runtime = str(
            item.get("runtime")
            or item.get("execution_mode")
            or item.get("executionMode")
            or "single_turn"
        ).strip().lower()
        runtime = {
            "direct_llm": "single_turn",
            "tool_limited_loop": "agent_loop",
        }.get(raw_runtime, raw_runtime)
        if runtime not in {"single_turn", "agent_loop"}:
            runtime = "agent_loop" if categories else "single_turn"
        options = item.get("options") if isinstance(item.get("options"), Mapping) else {}

        visibility = str(item.get("visibility") or "").strip().lower()
        exposure = str(item.get("exposure") or "").strip().lower()
        if exposure not in {"internal", "tool"}:
            exposure = "internal" if visibility == "internal" or capability_id in {
                "prompt_optimize", "title", "compress", "memory_advise", "memory_curate",
            } else "tool"
        enabled = bool(item.get("enabled", visibility != "hidden"))
        capabilities.append(
            {
                "id": capability_id,
                "name": str(item.get("name") or capability_id),
                "enabled": enabled,
                "exposure": exposure,
                "runtime": runtime,
                "model_target": item.get("model_target") or item.get("modelTarget") or {},
                "description": str(item.get("description") or ""),
                "prompt": str(item.get("prompt") or item.get("system_prompt") or item.get("systemPrompt") or ""),
                "input_schema": item.get("input_schema") or item.get("inputSchema") or {},
                "output_schema": item.get("output_schema") or item.get("outputSchema") or {},
                "allowed_tool_categories": sorted(set(categories)),
                "max_turns": item.get("max_turns") or item.get("maxTurns") or options.get("max_turns"),
            }
        )

    return {"schema_version": SCHEMA_VERSION, "capabilities": capabilities}, []


def migrate_mode_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    prompt = str(payload.get("prompt") or payload.get("roleDefinition") or payload.get("role_definition") or "").strip()
    extra = str(payload.get("customInstructions") or payload.get("custom_instructions") or "").strip()
    if extra:
        prompt = "\n\n".join(part for part in (prompt, extra) if part)
    categories = payload.get("allowed_tool_categories") or []
    normalized_categories = []
    for item in categories:
        value = item[0] if isinstance(item, list) and item else item
        category = migrate_category(value)
        if category not in normalized_categories:
            normalized_categories.append(category)

    slug = str(payload.get("slug") or "").strip().lower()
    default_profile_kind = (
        "both" if slug in _BOTH_MODE_SLUGS else "subagent" if slug in _SUBAGENT_MODE_SLUGS else "primary"
    )
    profile_kind = str(
        payload.get("profile_kind")
        or payload.get("agentKind")
        or payload.get("agent_kind")
        or default_profile_kind
    )
    if profile_kind not in {"primary", "subagent", "both"}:
        profile_kind = "primary"
    default_shared = "selected_artifacts" if slug in _SELECTED_ARTIFACT_MODE_SLUGS else "indexes_only"
    shared = str(payload.get("shared_context_policy") or payload.get("sharedContextPolicy") or default_shared)
    if shared not in {"indexes_only", "selected_artifacts", "full_session_readonly"}:
        shared = "indexes_only"
    completion_policy = str(payload.get("completion_policy") or "").strip().lower()
    if completion_policy not in {"text", "explicit"}:
        completion_policy = "explicit" if profile_kind in {"subagent", "both"} or slug in {"agent", "review"} else "text"
    result = {
        "slug": slug,
        "name": str(payload.get("name") or payload.get("slug") or "").strip(),
        "purpose": str(
            payload.get("purpose")
            or payload.get("whenToUse")
            or payload.get("when_to_use")
            or payload.get("description")
            or ""
        ).strip(),
        "prompt": prompt,
        "allowed_tool_categories": normalized_categories,
        "profile_kind": profile_kind,
        "completion_policy": completion_policy,
        "source": payload.get("source"),
    }
    if profile_kind in {"subagent", "both"}:
        result.update(
            {
                "model_target": payload.get("model_target")
                or payload.get("delegatedModelTarget")
                or payload.get("delegated_model_target")
                or {},
                "max_turns": payload.get("max_turns")
                or payload.get("maxTurns")
                or payload.get("defaultMaxTurns")
                or payload.get("default_max_turns"),
                "shared_context_policy": shared,
            }
        )
    return result


def migrate_modes_payload(data: Any, *, extra_profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = dict(data) if isinstance(data, Mapping) else {}
    raw_modes = payload.get("modes") if isinstance(payload.get("modes"), list) else (data if isinstance(data, list) else [])
    modes: dict[str, dict[str, Any]] = {}
    for raw in list(raw_modes or []) + list(extra_profiles or []):
        if not isinstance(raw, Mapping):
            continue
        mode = migrate_mode_payload(raw)
        if mode["slug"]:
            modes[mode["slug"]] = mode
    return {"schema_version": SCHEMA_VERSION, "modes": list(modes.values())}


def restore_migrated_capabilities_from_modes(
    settings: Mapping[str, Any],
    modes: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    settings_payload = deepcopy(dict(settings or {}))
    modes_payload = migrate_modes_payload(modes)
    capabilities_payload, _ = migrate_capabilities_payload(settings_payload.get("capabilities"))
    capabilities = {
        str(item.get("id") or "").strip().lower(): item
        for item in capabilities_payload.get("capabilities") or []
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }
    retained_modes: list[dict[str, Any]] = []
    for raw in modes_payload.get("modes") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if str(item.get("purpose") or "") != "Migrated tool-using capability":
            retained_modes.append(item)
            continue
        capability_id = str(item.get("slug") or "").strip().lower()
        if not capability_id or capability_id in capabilities:
            retained_modes.append(item)
            continue
        capabilities[capability_id] = {
            "id": capability_id,
            "name": str(item.get("name") or capability_id),
            "enabled": True,
            "exposure": "tool",
            "runtime": "agent_loop",
            "model_target": item.get("model_target") or {},
            "description": "",
            "prompt": str(item.get("prompt") or ""),
            "input_schema": {},
            "output_schema": {},
            "allowed_tool_categories": list(item.get("allowed_tool_categories") or []),
            "max_turns": item.get("max_turns") or 20,
        }
    settings_payload["capabilities"] = {
        "schema_version": SCHEMA_VERSION,
        "capabilities": list(capabilities.values()),
    }
    return settings_payload, {"schema_version": SCHEMA_VERSION, "modes": retained_modes}


def migrate_settings_payload(data: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = deepcopy(dict(data) if isinstance(data, Mapping) else {})
    theme = str(payload.get("theme") or "light").strip().lower()
    payload["theme"] = theme if theme in {"light", "dark"} else "light"
    accent = str(
        payload.get("accent") or payload.get("accent_color") or DEFAULT_ACCENT
    ).strip().lower()
    payload["accent"] = accent if accent in SUPPORTED_ACCENTS else DEFAULT_ACCENT
    payload.pop("accent_color", None)
    payload["close_to_tray"] = bool(payload.get("close_to_tray", True))
    payload["permissions"] = migrate_permissions_payload(payload.get("permissions"))
    capabilities, profiles = migrate_capabilities_payload(payload.get("capabilities"))

    legacy_optimizer = payload.get("prompt_optimizer")
    legacy_model = str(payload.get("prompt_optimizer_model") or "").strip()
    legacy_prompt = ""
    if isinstance(legacy_optimizer, Mapping):
        templates = legacy_optimizer.get("templates")
        selected = str(legacy_optimizer.get("selected_template") or "default")
        if isinstance(templates, Mapping):
            legacy_prompt = str(templates.get(selected) or "").strip()
    if legacy_prompt or legacy_model:
        existing = next(
            (item for item in capabilities["capabilities"] if item.get("id") == "prompt_optimize"),
            None,
        )
        override = existing or {
            "id": "prompt_optimize",
            "name": "Prompt Optimize",
            "enabled": True,
            "exposure": "internal",
            "description": "",
            "input_schema": {},
        }
        if legacy_prompt and not override.get("prompt"):
            override["prompt"] = legacy_prompt
        if legacy_model and not override.get("model_target"):
            override["model_target"] = {"source": "explicit", "model_ref": legacy_model}
        if existing is None:
            capabilities["capabilities"].append(override)
    payload["capabilities"] = capabilities
    agent = dict(payload.get("agent")) if isinstance(payload.get("agent"), Mapping) else {}
    payload["agent"] = {"max_turns": int(agent.get("max_turns") or agent.get("maxTurns") or 20)}

    prompts = dict(payload.get("prompts")) if isinstance(payload.get("prompts"), Mapping) else {}
    global_instructions = str(
        prompts.get("global_instructions")
        or prompts.get("agent_tool_guidelines")
        or prompts.get("default_system_prompt")
        or prompts.get("base_role_definition")
        or ""
    ).strip()
    payload["prompts"] = {
        "global_instructions": global_instructions,
        "include_environment": bool(prompts.get("include_environment", True)),
        "file_tree_max_depth": int(prompts.get("file_tree_max_depth") or 2),
    }
    payload.pop("prompt_optimizer", None)
    payload.pop("prompt_optimizer_model", None)
    payload.pop("shell_backend", None)
    payload["schema_version"] = SCHEMA_VERSION
    return payload, profiles
