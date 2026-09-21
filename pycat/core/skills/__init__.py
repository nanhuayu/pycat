"""Skill system public API.

Skills are reusable instruction/resource packages discovered from project,
global and read-only external locations. Contracts live in
``pycat.models.contracts.skill``; this package contains discovery, routing, resource
access and prompt-section construction.
"""
from __future__ import annotations

from pycat.models.contracts.skill import Skill, SkillExecutionCheck, SkillInvocationSpec

from .discovery import SkillsManager
from .prompt import build_skill_prompt_section
from .routing import check_skill_execution_availability, resolve_skill_invocation_spec

__all__ = [
    "Skill",
    "SkillExecutionCheck",
    "SkillInvocationSpec",
    "SkillsManager",
    "build_skill_prompt_section",
    "check_skill_execution_availability",
    "resolve_skill_invocation_spec",
]
