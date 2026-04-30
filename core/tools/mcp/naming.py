"""Helpers for building and parsing external MCP tool names."""
from __future__ import annotations

import re
from typing import Tuple


MCP_TOOL_PREFIX = "mcp"
MCP_TOOL_SEPARATOR = "__"
MCP_TOOL_PUBLIC_PREFIX = f"{MCP_TOOL_PREFIX}{MCP_TOOL_SEPARATOR}"


def sanitize_mcp_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip())
    text = re.sub(r"_{2,}", "_", text).strip("_")
    return text


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    server = sanitize_mcp_name(server_name)
    tool = sanitize_mcp_name(tool_name)
    if not server or not tool:
        return ""
    return f"{MCP_TOOL_PREFIX}{MCP_TOOL_SEPARATOR}{server}{MCP_TOOL_SEPARATOR}{tool}"


def is_mcp_tool_name(name: str) -> bool:
    parsed = parse_mcp_tool_name(name)
    return bool(parsed[0] and parsed[1])


def normalize_mcp_tool_name(name: str) -> str:
    server_name, tool_name = parse_mcp_tool_name(name)
    if not server_name or not tool_name:
        return ""
    return build_mcp_tool_name(server_name, tool_name)


def parse_mcp_tool_name(name: str) -> Tuple[str, str]:
    raw = str(name or "").strip()
    if not raw:
        return "", ""

    if raw.startswith("mcp--"):
        raw = raw.replace("mcp--", "mcp__", 1)
        raw = raw.replace("--", "__")

    if not raw.startswith(MCP_TOOL_PUBLIC_PREFIX):
        return "", ""

    parts = raw.split(MCP_TOOL_SEPARATOR)
    if len(parts) < 3:
        return "", ""

    server_name = sanitize_mcp_name(parts[1])
    tool_name = sanitize_mcp_name("_".join(parts[2:]))
    if not server_name or not tool_name:
        return "", ""
    return server_name, tool_name


def tool_names_match(left: str, right: str) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False

    left_mcp = normalize_mcp_tool_name(left_text)
    right_mcp = normalize_mcp_tool_name(right_text)
    if left_mcp and right_mcp:
        return left_mcp == right_mcp

    left_norm = re.sub(r"[-\s]+", "_", left_text).lower()
    right_norm = re.sub(r"[-\s]+", "_", right_text).lower()
    return left_norm == right_norm