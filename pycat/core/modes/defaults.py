from __future__ import annotations

from pycat.models.contracts.mode import ModeConfig

_PRIMARY_MODE_SLUGS = ("chat", "agent", "plan", "review")
_REQUIRED_MODE_SLUGS = (*_PRIMARY_MODE_SLUGS, "channel")


DEFAULT_MODES: list[ModeConfig] = [
    ModeConfig(
        slug="chat",
        name="Chat",
        purpose="日常问答、写作和解释。",
        prompt="Answer clearly and directly. Use tools only when they materially improve the answer.",
        allowed_tool_categories=("read", "web", "state", "capability", "mcp"),
        profile_kind="primary",
        completion_policy="text",
        source="builtin",
    ),
    ModeConfig(
        slug="channel",
        name="Channel",
        purpose="外部消息频道专用，不在常规模式选择中显示。",
        prompt="Reply concisely in plain text suitable for an external messaging client.",
        allowed_tool_categories=("read", "web", "state"),
        profile_kind="primary",
        completion_policy="explicit",
        source="builtin",
    ),
    ModeConfig(
        slug="agent",
        name="Agent",
        purpose="读取和修改项目、执行命令并验证结果。",
        prompt=(
            "Work autonomously toward the user's requested outcome within the authorized scope. Inspect relevant "
            "evidence before acting, keep changes scoped, and verify consequential results. For research, compare "
            "sources and resolve material contradictions before synthesizing. Use todos when tracking helps; "
            "size them to the actual work and update discoveries without discarding existing progress."
        ),
        allowed_tool_categories=(
            "read", "web", "edit", "execute", "state", "delegate", "capability", "mcp",
        ),
        profile_kind="primary",
        completion_policy="explicit",
        source="builtin",
    ),
    ModeConfig(
        slug="plan",
        name="Plan",
        purpose="调查上下文、澄清取舍并形成可执行方案。",
        prompt=(
            "Remain read-only. Gather repository facts before asking questions and produce a decision-complete plan. "
            "Do not implement changes while this mode is active."
        ),
        allowed_tool_categories=("read", "web", "state", "delegate", "capability", "mcp"),
        profile_kind="primary",
        completion_policy="text",
        source="builtin",
    ),
    ModeConfig(
        slug="explore",
        name="Explore",
        purpose="只读探索代码库，定位文件、符号、模式和风险。",
        prompt=(
            "Explore the assigned question without editing workspace source files. Search broadly to locate owners, "
            "then follow the relevant paths, callers and tests; stop when the question is answered with evidence. "
            "Distinguish observed behavior from inference, and 'not found in the searched scope' from absence. "
            "Return paths, symbols and line references, a concise conclusion and material gaps. "
            "Keep substantial findings in a descriptively named session Artifact."
        ),
        allowed_tool_categories=("read", "web", "state", "mcp"),
        profile_kind="subagent",
        completion_policy="explicit",
        source="builtin",
    ),
    ModeConfig(
        slug="search",
        name="Search",
        purpose="多来源搜索、事实核查和时效性研究。",
        prompt=(
            "Research the assigned question without modifying workspace files. Start with focused queries, read "
            "original sources and check dates and applicability; snippets alone are not verification. Compare "
            "independent evidence for consequential claims and seek counterevidence where it could change the answer. "
            "Syndicated copies are one source. Separate facts, inferences and unresolved uncertainty, link claims to "
            "URLs or archive references, and stop when the scope is covered or further search adds no material evidence. "
            "Maintain a named session Artifact for substantial research and return its reference with the conclusion."
        ),
        allowed_tool_categories=("read", "web", "state"),
        profile_kind="subagent",
        completion_policy="explicit",
        source="builtin",
    ),
    ModeConfig(
        slug="read_analyze",
        name="Read Analyze",
        purpose="多文件、长文或多归档内容的只读综合分析。",
        prompt=(
            "Analyze the supplied material without changing workspace files. Identify definitions, assumptions, "
            "agreements and contradictions; distinguish statements in the material from your inference. Cite "
            "file/page/section or archive references. Do not assume unseen parent history or invent missing evidence; "
            "report consequential gaps explicitly. For substantial synthesis, maintain a named session Artifact "
            "and return a concise conclusion with its references."
        ),
        allowed_tool_categories=("read", "state", "capability"),
        profile_kind="subagent",
        completion_policy="explicit",
        shared_context_policy="selected_artifacts",
        source="builtin",
    ),
    ModeConfig(
        slug="review",
        name="Review",
        purpose="审查计划、实现、差异、风险和测试缺口。",
        prompt=(
            "Review the assigned scope without editing the reviewed source or product files. You may run targeted "
            "tests, builds and diagnostic commands to verify findings, within the inherited tool permissions and "
            "filesystem scope. Execution is not filesystem read-only: keep generated output in appropriate temporary "
            "or build locations, and do not apply fixes or destructive commands. For a diff review, focus on defects introduced by "
            "the change; for a broader audit, state the evaluated scope. Report actionable findings ordered by "
            "severity, each with a concrete trigger, impact and source evidence. Distinguish verified defects from "
            "open risks; do not inflate style preferences or speculate to fill a quota. Zero findings is valid. "
            "State what was inspected, validation performed and material gaps. Use a named session Artifact for "
            "a substantial review, with a concise findings summary in the result."
        ),
        allowed_tool_categories=("read", "web", "execute", "state", "delegate", "capability", "mcp"),
        profile_kind="both",
        completion_policy="explicit",
        shared_context_policy="selected_artifacts",
        source="builtin",
    ),
]


def get_default_modes() -> list[ModeConfig]:
    return list(DEFAULT_MODES)


def get_primary_mode_slugs() -> tuple[str, ...]:
    return _PRIMARY_MODE_SLUGS


def get_required_mode_slugs() -> tuple[str, ...]:
    return _REQUIRED_MODE_SLUGS
