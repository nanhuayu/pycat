from __future__ import annotations

import json
from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult


class AskQuestionsTool(BaseTool):
    @property
    def name(self) -> str:
        return "user__ask"

    @property
    def display_name(self) -> str:
        return "询问用户"

    @property
    def description(self) -> str:
        return "Ask the user up to three concise questions with optional choices and free-text input."

    @property
    def category(self) -> str:
        return "state"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Short unique answer key."},
                            "text": {"type": "string", "description": "Question shown to the user."},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
                                    "additionalProperties": False,
                                },
                            },
                            "multiple": {"type": "boolean", "description": "Allow multiple choices."},
                        },
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        raw_questions = arguments.get("questions")
        if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 3:
            return ToolResult("questions must contain 1 to 3 items.", is_error=True)
        answers: dict[str, Any] = {}
        seen: set[str] = set()
        for index, raw in enumerate(raw_questions):
            if not isinstance(raw, dict):
                return ToolResult(f"Invalid question at index {index}.", is_error=True)
            question_id = str(raw.get("id") or "").strip()
            text = str(raw.get("text") or "").strip()
            if not question_id or not text or question_id in seen:
                return ToolResult(f"Question {index + 1} requires a unique id and text.", is_error=True)
            seen.add(question_id)
            options = []
            for option in raw.get("options") or []:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                if label:
                    options.append({"label": label, "description": str(option.get("description") or "").strip()})
            answer = await context.ask_question({
                "id": question_id,
                "text": text,
                "options": options,
                "multiple": bool(raw.get("multiple")),
            })
            answers[question_id] = answer
        return ToolResult(json.dumps({"answers": answers}, ensure_ascii=False))
