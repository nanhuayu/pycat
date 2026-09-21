from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSections:
    channel: str = ""
    project_instructions: str = ""
    skills: str = ""


__all__ = ["PromptSections"]
