from __future__ import annotations

import json
from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult


class AskQuestionsTool(BaseTool):
    @property
    def name(self) -> str:
        return "askQuestions"

    @property
    def description(self) -> str:
        return (
            "Ask the user one or more follow-up questions with selectable options "
            "and optional freeform input, then return the structured answers."
        )

    @property
    def group(self) -> str:
        return "read"

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "header": {"type": "string", "description": "Unique short identifier for the question."},
                            "question": {"type": "string", "description": "Question shown to the user."},
                            "multiSelect": {"type": "boolean", "description": "Allow multiple selections."},
                            "allowFreeformInput": {"type": "boolean", "description": "Allow custom text input."},
                            "message": {"type": "string", "description": "Optional supporting context shown under the question."},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                        "recommended": {"type": "boolean"},
                                    },
                                    "required": ["label"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["header", "question"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        raw_questions = arguments.get("questions")
        if not isinstance(raw_questions, list) or not raw_questions:
            return ToolResult("Missing 'questions'", is_error=True)

        answers: dict[str, Any] = {}
        for index, raw_question in enumerate(raw_questions):
            question = self._normalize_question(raw_question, index)
            if question is None:
                return ToolResult(f"Invalid question at index {index}", is_error=True)
            answers[question["header"]] = await context.ask_question(question)

        return ToolResult(json.dumps({"answers": answers}, ensure_ascii=False))

    @staticmethod
    def _normalize_question(raw_question: Any, index: int) -> dict[str, Any] | None:
        if not isinstance(raw_question, dict):
            return None

        header = str(raw_question.get("header") or "").strip() or f"question_{index + 1}"
        question_text = str(raw_question.get("question") or "").strip()
        if not question_text:
            return None

        options = []
        for option in raw_question.get("options") or []:
            if isinstance(option, dict):
                label = str(option.get("label") or "").strip()
                if not label:
                    continue
                options.append(
                    {
                        "label": label,
                        "description": str(option.get("description") or "").strip(),
                        "recommended": bool(option.get("recommended", False)),
                    }
                )
            else:
                label = str(option or "").strip()
                if label:
                    options.append({"label": label, "description": "", "recommended": False})

        allow_freeform = bool(raw_question.get("allowFreeformInput", True))
        if not options and not allow_freeform:
            return None

        return {
            "header": header,
            "question": question_text,
            "multiSelect": bool(raw_question.get("multiSelect", False)),
            "allowFreeformInput": allow_freeform,
            "message": str(raw_question.get("message") or "").strip(),
            "options": options,
        }
