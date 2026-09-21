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
            "Work autonomously until the requested change is complete. Inspect the existing system before editing, "
            "keep changes scoped, and verify modifications with focused tests. Use todos only for meaningful "
            "multi-step progress."
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
            "Explore the workspace without editing. Return concrete paths, symbols, evidence, risks, and open questions."
        ),
        allowed_tool_categories=("read", "web", "state", "mcp"),
        profile_kind="subagent",
        completion_policy="explicit",
        max_turns=60,
        source="builtin",
    ),
    ModeConfig(
        slug="search",
        name="Search",
        purpose="多来源搜索、事实核查和时效性研究。",
        prompt=(
            "Research without modifying the workspace. Compare multiple sources, distinguish facts from uncertainty, "
            "and include source URLs or archive identifiers in the result."
        ),
        allowed_tool_categories=("read", "web", "state"),
        profile_kind="subagent",
        completion_policy="explicit",
        max_turns=80,
        source="builtin",
    ),
    ModeConfig(
        slug="read_analyze",
        name="Read Analyze",
        purpose="多文件、长文或多归档内容的只读综合分析。",
        prompt="Compare the supplied material, preserve provenance, and return a concise structured synthesis.",
        allowed_tool_categories=("read", "state", "capability"),
        profile_kind="subagent",
        completion_policy="explicit",
        max_turns=80,
        shared_context_policy="selected_artifacts",
        source="builtin",
    ),
    ModeConfig(
        slug="review",
        name="Review",
        purpose="审查计划、实现、差异、风险和测试缺口。",
        prompt=(
            "Review without making changes. Lead with concrete findings ordered by severity and support them with evidence."
        ),
        allowed_tool_categories=("read", "web", "state", "delegate", "capability", "mcp"),
        profile_kind="both",
        completion_policy="explicit",
        max_turns=60,
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
