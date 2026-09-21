"""Ollama ``/api/chat`` request and response normalization."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def _arguments_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def stable_tool_call_id(name: str, arguments: Any, index: int) -> str:
    raw = f"{index}\0{name}\0{_arguments_text(arguments)}".encode("utf-8", errors="replace")
    return f"ollama_{hashlib.sha1(raw).hexdigest()[:16]}"


def normalize_tool_calls(values: Iterable[Any] | None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, value in enumerate(values or ()):
        if not isinstance(value, dict):
            continue
        function = value.get("function") if isinstance(value.get("function"), dict) else {}
        name = str(function.get("name") or value.get("name") or "").strip()
        if not name:
            continue
        arguments = function.get("arguments", value.get("arguments", {}))
        calls.append(
            {
                "id": str(value.get("id") or stable_tool_call_id(name, arguments, index)),
                "type": "function",
                "function": {"name": name, "arguments": _arguments_text(arguments)},
            }
        )
    return calls


def _content_and_images(content: Any) -> tuple[str, list[str]]:
    if not isinstance(content, list):
        return str(content or ""), []
    text: list[str] = []
    images: list[str] = []
    for item in content:
        if isinstance(item, str):
            if item:
                text.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "text":
            value = str(item.get("text") or "")
            if value:
                text.append(value)
        elif item_type == "image_url":
            image = item.get("image_url") if isinstance(item.get("image_url"), dict) else {}
            value = str(image.get("url") or "")
            if value.startswith("data:") and ";base64," in value:
                value = value.split(";base64,", 1)[1]
            if value:
                images.append(value)
    return "\n".join(text).strip(), images


def messages_from_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content, images = _content_and_images(message.get("content", ""))
        payload: dict[str, Any] = {"role": role, "content": content}
        if images and role == "user":
            payload["images"] = images

        if role == "assistant":
            thinking = message.get("thinking")
            if thinking is not None:
                payload["thinking"] = str(thinking)
            calls = normalize_tool_calls(message.get("tool_calls"))
            if calls:
                payload["tool_calls"] = [
                    {
                        "function": {
                            "name": call["function"]["name"],
                            "arguments": _arguments_object(call["function"]["arguments"]),
                        }
                    }
                    for call in calls
                ]
                for call in calls:
                    tool_names[str(call.get("id") or "")] = str(call["function"]["name"])
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            tool_name = str(message.get("tool_name") or tool_names.get(call_id) or "").strip()
            if tool_name:
                payload["tool_name"] = tool_name
        result.append(payload)
    return result


def _arguments_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_message(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], int]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    content = str(message.get("content") or "")
    thinking = str(message.get("thinking") or "")
    calls = normalize_tool_calls(message.get("tool_calls"))
    tokens = 0
    for key in ("prompt_eval_count", "eval_count"):
        try:
            tokens += int(payload.get(key) or 0)
        except Exception:
            continue
    return content, thinking, calls, tokens


__all__ = ["messages_from_openai", "normalize_tool_calls", "parse_message", "stable_tool_call_id"]
