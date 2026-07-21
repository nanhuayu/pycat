from __future__ import annotations

from typing import List, Set

from models.contracts.session_state import WorkTrace


def compact_work_route(work_trace: WorkTrace, *, max_steps: int = 12, through_seq: int = 0) -> str:
    labels: List[str] = []
    steps = work_trace.steps
    cutoff = int(through_seq or 0)
    if cutoff > 0:
        steps = [step for step in steps if int(step.seq or 0) <= cutoff]
    for step in steps[-max_steps:]:
        label = step.label or step.kind or step.tool_name or "tool"
        target = step.target or step.content_id
        text = label
        if target:
            text += f"({target})"
        if not labels or labels[-1] != text:
            labels.append(text)
    return " -> ".join(labels)


def work_trace_refs(work_trace: WorkTrace, *, limit: int = 6, through_seq: int = 0) -> list[str]:
    refs: list[str] = []
    seen: Set[str] = set()
    steps = work_trace.steps
    cutoff = int(through_seq or 0)
    if cutoff > 0:
        steps = [step for step in steps if int(step.seq or 0) <= cutoff]
    for step in reversed(steps):
        candidates = list(step.refs)
        if step.content_id:
            candidates.append(step.content_id)
        for ref in candidates:
            clean = str(ref or "").strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            refs.append(clean)
            if len(refs) >= limit:
                return list(reversed(refs))
    return list(reversed(refs))


def build_work_trace_prompt_view(work_trace: WorkTrace, *, goal: str = "", max_chars: int = 1800) -> str:
    if not work_trace.steps:
        return ""
    display_goal = (str(goal or "").strip() or work_trace.goal).strip()
    lines = ["<work_trace>"]
    if display_goal:
        lines.append(f"goal: {display_goal[:500]}")
    route = compact_work_route(work_trace, max_steps=12)
    if route:
        lines.append(f"route: {route}")
    latest = work_trace.steps[-1]
    current = latest.summary or latest.label or latest.tool_name
    if current:
        lines.append(f"current: {current[:360]}")
    refs = work_trace_refs(work_trace, limit=6)
    if refs:
        lines.append("evidence: " + ", ".join(refs))
    lines.append("rules: Work trace is runtime-maintained progress, not memory or a report. Use artifacts for durable deliverables.")
    lines.append("</work_trace>")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."
