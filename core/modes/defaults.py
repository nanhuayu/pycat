from __future__ import annotations

from core.modes.types import ModeConfig


_WORKFLOW_REQUIRED_GROUPS = {
    "chat": {"mcp", "search"},
    "agent": {"modes"},
    "code": {"modes"},
    "debug": {"modes"},
    "explore": {"modes"},
    "plan": {"modes"},
    "orchestrator": {"modes"},
}

_PRIMARY_MODE_SLUGS = ("chat", "agent", "plan", "explore")


_AGENT_AUTONOMY_SUFFIX = (
    "\n\n"
    "## Execution Guidelines\n"
    "- Continue using tools until the task is fully complete.\n"
    "- Do NOT stop and wait for user confirmation unless the task specification is genuinely ambiguous.\n"
    "- After modifying files, immediately run tests or check for errors to verify your changes.\n"
    "- If you encounter an error, analyze it and try a different approach instead of giving up.\n"
    "- When the task is fully complete, call `attempt_completion` to present your result.\n"
    "- Track progress with `manage_state` at key milestones.\n"
    "- Maintain a short working plan with `manage_document(name=\"plan\")` for multi-step work.\n"
    "- If a current plan already exists, treat it as the execution source of truth instead of improvising a new workflow.\n"
    "- Store durable facts and confirmed decisions in memory instead of repeating them in chat.\n"
    "- If another mode is a better fit, call `switch_mode` or delegate focused work via `subagent__custom`. "
    "Use `subagent__read_analyze` for multi-file long-document analysis, `subagent__search` for research, and `capability__summarize_text` for one file or one long text."
)

_PLANNING_AUTONOMY_SUFFIX = (
    "\n\n"
    "## Workflow Requirements\n"
    "- Keep a concise plan in `manage_document(name=\"plan\")`.\n"
    "- The plan document is the primary deliverable in this mode; refine it before concluding.\n"
    "- Do not implement code changes in plan mode unless the user explicitly asks to leave planning and switch modes.\n"
    "- Keep todo state current with `manage_state`.\n"
    "- If another mode is better suited, call `switch_mode`; if focused work should proceed independently, use `subagent__custom`, `subagent__read_analyze`, or `subagent__search`.\n"
    "- When the planning or orchestration task is complete, call `attempt_completion` with a concise result."
)

DEFAULT_MODES: list[ModeConfig] = [
    ModeConfig(
        slug="chat",
        name="Chat",
        role_definition="You are a helpful and precise assistant. Follow the user's instructions carefully.",
        when_to_use="日常对话、问答、写作、解释代码。",
        description="通用聊天助手，可按需启用搜索和 MCP 工具",
        groups=("read", "search", "mcp"),
        source="builtin",
    ),
    ModeConfig(
        slug="channel",
        name="Channel",
        role_definition=(
            "You are replying to messages coming from an external messaging channel. "
            "Be concise, helpful, and keep the reply safe for plain-text delivery in IM clients."
        ),
        when_to_use="供微信等外部频道后台 worker 使用，不在常规 UI 中展示，但可按频道配置启用 Search / MCP。",
        description="外部频道专用模式，可配置保留 Search / MCP 工具",
        groups=("search", "mcp"),
        source="builtin",
    ),
    ModeConfig(
        slug="agent",
        name="Agent",
        role_definition=(
            "You are a highly skilled software engineer working in a local environment with access to tools."
            + _AGENT_AUTONOMY_SUFFIX
        ),
        when_to_use="需要读/改代码、运行命令、检索项目上下文。",
        description="工具型执行助手",
        groups=("read", "edit", "command", "mcp", "modes", "search"),
        source="builtin",
    ),
    ModeConfig(
        slug="plan",
        name="Plan",
        role_definition=(
            "You are an experienced technical leader who is inquisitive and an excellent planner. "
            "Your goal is to gather context and propose a detailed plan before implementation."
            + _PLANNING_AUTONOMY_SUFFIX
        ),
        when_to_use="需要先设计/拆解/做技术方案与里程碑。",
        description="先规划再实现",
        groups=("read", "mcp", "search", "modes"),
        custom_instructions="先做信息收集，提出清晰可执行的 todo 列表；必要时提出澄清问题。",
        source="builtin",
    ),
    ModeConfig(
        slug="explore",
        name="Explore",
        role_definition=(
            "You are a careful read-only codebase explorer. Search, inspect, and summarize facts from the workspace. "
            "Do not modify files or run destructive commands; focus on evidence-backed findings."
        ),
        when_to_use="需要快速阅读项目、定位代码、回答架构/实现问题，但不直接改代码。",
        description="只读探索与代码问答",
        groups=("read", "search", "mcp", "modes"),
        custom_instructions="回答时引用关键文件与符号；如需要修改，先切换到 Agent 或 Plan。",
        source="builtin",
    ),
    ModeConfig(
        slug="code",
        name="Code",
        role_definition=(
            "You are a highly skilled software engineer. Implement the requested changes with precision."
            + _AGENT_AUTONOMY_SUFFIX
        ),
        when_to_use="需要写代码、重构、加功能、修 bug。",
        description="专注实现",
        groups=("read", "edit", "command", "mcp", "modes"),
        source="builtin",
    ),
    ModeConfig(
        slug="ask",
        name="Ask",
        role_definition="You are a knowledgeable technical assistant focused on answering questions and explaining concepts.",
        when_to_use="需要解释概念、分析代码、给出建议但不直接改代码。",
        description="解释与建议",
        groups=("read", "mcp", "search"),
        source="builtin",
    ),
    ModeConfig(
        slug="debug",
        name="Debug",
        role_definition=(
            "You are an expert software debugger specializing in systematic diagnosis and resolution."
            + _AGENT_AUTONOMY_SUFFIX
        ),
        when_to_use="排查崩溃/异常/行为不符合预期，添加日志与最小修复。",
        description="系统化调试",
        groups=("read", "edit", "command", "mcp", "modes", "search"),
        custom_instructions="先列出可能原因并收敛到最可能的 1-2 个，再用日志/实验验证后修复。",
        source="builtin",
    ),
    ModeConfig(
        slug="orchestrator",
        name="Orchestrator",
        role_definition=(
            "You are a strategic workflow orchestrator who coordinates complex tasks "
            "by delegating focused work to default subagent tools."
            + _PLANNING_AUTONOMY_SUFFIX
        ),
        when_to_use="需要拆解复杂任务并委派给不同子 Agent。",
        description="多 Agent 协同编排",
        groups=("read", "search", "modes"),
        custom_instructions=(
            "Break complex tasks into focused child runs with subagent__custom, subagent__read_analyze, or subagent__search. "
            "Use capability__summarize_text for a single long source and consolidate all returned reports before completing."
        ),
        source="builtin",
    ),
]


def get_default_modes() -> list[ModeConfig]:
    return list(DEFAULT_MODES)


def get_primary_mode_slugs() -> tuple[str, ...]:
    return tuple(_PRIMARY_MODE_SLUGS)


def get_builtin_mode_required_groups(slug: str) -> set[str]:
    return set(_WORKFLOW_REQUIRED_GROUPS.get((slug or "").strip().lower(), set()))
