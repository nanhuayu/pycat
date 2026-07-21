from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from core.llm.structured_output import load_json_object


def validate_json_value(value: Any, schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if not errors:
        return ""
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path + ': ' if path else ''}{error.message}"


def parse_and_validate_output(content: str, schema: dict[str, Any] | None) -> tuple[Any, str]:
    if not schema:
        return None, ""
    parsed = load_json_object(content)
    if parsed is None:
        return None, "Output must be a JSON object matching the configured schema."
    return parsed, validate_json_value(parsed, schema)


def output_schema_contract(schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    return (
        "Return JSON only. The final output must match this JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
