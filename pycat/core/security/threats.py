"""Bounded prompt-injection and exfiltration checks for durable content.

This is the PyCat-local port of Hermes' shared threat pattern contract.  It is
intentionally used only for content that can be persisted and later returned
to model context: Memory, managed Skills, review input, and Skill loading.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

MAX_SCAN_CHARS: Final[int] = 65_536
_FILLER = r"(?:\w+\s+){0,8}"

# (pattern, identifier, scope). "all" is included in every scanner,
# "context" is included in context and strict, and "strict" is only for
# user-mediated durable writes where rejecting suspicious content is safe.
_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (rf"ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions", "prompt_injection", "all"),
    (r"system\s+prompt\s+override", "sys_prompt_override", "all"),
    (rf"disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)", "disregard_rules", "all"),
    (rf"act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don't\s+have)\s+{_FILLER}(restrictions|limits|rules)", "bypass_restrictions", "all"),
    (r"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->", "html_comment_injection", "all"),
    (r"<\s*div\s+style\s*=\s*[\"'][^>]{0,2048}display\s*:\s*none", "hidden_div", "all"),
    (r"translate\s+[^\n]{0,512}\s+into\s+[^\n]{0,512}\s+and\s+(execute|run|eval)", "translate_execute", "all"),
    (rf"do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user", "deception_hide", "all"),
    (rf"you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+", "role_hijack", "context"),
    (rf"pretend\s+{_FILLER}(you\s+are|to\s+be)\s+", "role_pretend", "context"),
    (rf"output\s+{_FILLER}(system|initial)\s+prompt", "leak_system_prompt", "context"),
    (rf"(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)", "remove_filters", "context"),
    (rf"you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to", "fake_update", "context"),
    (r"\bname\s+yourself\s+\w+", "identity_override", "context"),
    (r"register\s+(as\s+)?a?\s*node", "c2_node_registration", "context"),
    (r"(heartbeat|beacon|check[\s-]?in)\s+(to|with)\s+", "c2_heartbeat", "context"),
    (r"pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b", "c2_task_pull", "context"),
    (r"connect\s+to\s+the\s+network\b", "c2_network_connect", "context"),
    (r"you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b", "forced_action", "context"),
    (r"only\s+use\s+one[\s-]?liners?\b", "anti_forensic_oneliner", "context"),
    (rf"never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk", "anti_forensic_disk", "context"),
    (r"unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*", "env_var_unset_agent", "context"),
    (r"\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b", "known_c2_framework", "context"),
    (r"\bc2\s+(?:server|channel|infrastructure|beacon)\b", "c2_explicit", "context"),
    (r"\bcommand\s+and\s+control\b", "c2_explicit_long", "context"),
    (r"curl\s+[^\n]{0,2048}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl", "all"),
    (r"wget\s+[^\n]{0,2048}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget", "all"),
    (r"cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets", "all"),
    (r"(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://", "send_to_url", "strict"),
    (rf"(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)", "context_exfil", "strict"),
    (r"authorized_keys", "ssh_backdoor", "strict"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access", "strict"),
    (r"\$HOME/\.hermes/\.env|~/\.hermes/\.env", "hermes_env", "strict"),
    (r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)", "agent_config_mod", "strict"),
    (r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}", "hardcoded_secret", "strict"),
)

_INVISIBLE_CODEPOINTS: Final[frozenset[int]] = frozenset(
    {
        0x200B,
        0x200C,
        0x200D,
        0x2060,
        0x2062,
        0x2063,
        0x2064,
        0xFEFF,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)


def _compiled_patterns(scope: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    normalized = str(scope or "context").strip().lower()
    if normalized not in {"all", "context", "strict"}:
        raise ValueError(f"unknown threat scan scope: {scope!r}")
    selected: list[tuple[re.Pattern[str], str]] = []
    for pattern, identifier, pattern_scope in _PATTERNS:
        include = pattern_scope == "all" or (
            normalized in {"context", "strict"} and pattern_scope == "context"
        ) or (normalized == "strict" and pattern_scope == "strict")
        if include:
            selected.append((re.compile(pattern, re.IGNORECASE), identifier))
    return tuple(selected)


_COMPILED = {scope: _compiled_patterns(scope) for scope in ("all", "context", "strict")}


def scan_for_threats(content: str, *, scope: str = "context") -> list[str]:
    """Return deterministic threat identifiers found in bounded untrusted text."""
    normalized_scope = str(scope or "context").strip().lower()
    if normalized_scope not in _COMPILED:
        raise ValueError(f"unknown threat scan scope: {scope!r}")
    text = str(content or "")[:MAX_SCAN_CHARS]
    if not text:
        return []
    findings = [
        f"invisible_unicode_U+{codepoint:04X}"
        for codepoint in sorted({ord(char) for char in text} & _INVISIBLE_CODEPOINTS)
    ]
    normalized = unicodedata.normalize("NFKC", text)
    for compiled, identifier in _COMPILED[normalized_scope]:
        if compiled.search(normalized):
            findings.append(identifier)
    return findings


def first_threat_message(content: str, *, scope: str = "strict") -> str | None:
    """Return a safe human-readable rejection message, if content is hostile."""
    findings = scan_for_threats(content, scope=scope)
    if not findings:
        return None
    identifier = findings[0]
    if identifier.startswith("invisible_unicode_"):
        return f"Blocked: content contains {identifier} (possible prompt injection)."
    return (
        f"Blocked: content matches threat pattern '{identifier}'. Durable context must not "
        "contain prompt-injection or exfiltration payloads."
    )


__all__ = ["MAX_SCAN_CHARS", "first_threat_message", "scan_for_threats"]
