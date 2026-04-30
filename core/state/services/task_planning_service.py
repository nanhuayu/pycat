from __future__ import annotations

import re
from typing import List

from models.state import Task, TaskPriority


_BULLET_PREFIX = re.compile(r"^(?:[-*+]|\d+[.)]|[一二三四五六七八九十]+[、.])\s*")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


class TaskPlanningService:
    """Heuristic request decomposition for bootstrapping active todos."""

    @staticmethod
    def build_bootstrap_tasks(*, request_text: str, mode_slug: str, current_seq: int) -> List[Task]:
        text = str(request_text or "").strip()
        steps = TaskPlanningService._extract_explicit_steps(text)

        if not steps:
            steps = TaskPlanningService._default_steps(text=text, mode_slug=mode_slug)

        if TaskPlanningService.needs_clarification(text):
            steps.insert(0, "先向用户确认缺失的目标、范围或约束，再继续执行")

        unique_steps: List[str] = []
        for step in steps:
            normalized = TaskPlanningService._clean_step(step)
            if normalized and normalized not in unique_steps:
                unique_steps.append(normalized)

        planned: List[Task] = []
        for index, step in enumerate(unique_steps[:5]):
            planned.append(
                Task(
                    content=step,
                    priority=TaskPriority.HIGH if index == 0 else TaskPriority.MEDIUM,
                    tags=[mode_slug or "chat", "bootstrap"],
                    created_seq=int(current_seq),
                    updated_seq=int(current_seq),
                )
            )
        return planned

    @staticmethod
    def needs_clarification(request_text: str) -> bool:
        text = str(request_text or "").strip()
        if not text:
            return True
        tokens = _TOKEN_PATTERN.findall(text)
        if len(tokens) < 3:
            return True
        if len(text) < 16 and not any(char in text for char in ("/", "\\", "_", "-", "(")):
            return True
        return False

    @staticmethod
    def _extract_explicit_steps(request_text: str) -> List[str]:
        steps: List[str] = []
        for raw_line in str(request_text or "").splitlines():
            line = TaskPlanningService._clean_step(raw_line)
            if not line:
                continue
            if _BULLET_PREFIX.match(raw_line.strip()):
                steps.append(line)

        if len(steps) >= 2:
            return steps

        text = str(request_text or "")
        if any(sep in text for sep in ("；", ";")):
            for part in re.split(r"[；;]", text):
                line = TaskPlanningService._clean_step(part)
                if line:
                    steps.append(line)

        return steps if len(steps) >= 2 else []

    @staticmethod
    def _default_steps(*, text: str, mode_slug: str) -> List[str]:
        summary = TaskPlanningService._summarize_request(text)
        if mode_slug == "plan":
            return [
                f"梳理目标与约束：{summary}",
                "检查相关代码、文档和状态链路，定位真实约束",
                "形成分阶段执行方案，并标记风险、依赖和验证步骤",
            ]

        return [
            f"确认目标并收集上下文：{summary}",
            "定位相关代码或配置，确认需要修改的调用链",
            "实施最小必要改动，并同步更新会话文档或状态",
            "运行回归验证，确认行为与 UI 状态一致",
        ]

    @staticmethod
    def _summarize_request(request_text: str, limit: int = 72) -> str:
        text = re.sub(r"\s+", " ", str(request_text or "").strip())
        if len(text) <= limit:
            return text or "当前请求"
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _clean_step(value: str) -> str:
        text = _BULLET_PREFIX.sub("", str(value or "").strip())
        text = re.sub(r"\s+", " ", text)
        return text.strip("-:： ")