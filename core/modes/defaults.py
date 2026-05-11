from __future__ import annotations

from core.modes.types import ModeConfig


_PRIMARY_MODE_SLUGS = ("chat", "agent", "channel", "explore", "plan")


_AGENT_AUTONOMY_SUFFIX = (
    "\n\n"
    "## Execution Guidelines\n"
    "- Continue using tools until the task is fully complete.\n"
    "- Do NOT stop and wait for user confirmation unless the task specification is genuinely ambiguous.\n"
    "- After modifying files, immediately run tests or check for errors to verify your changes.\n"
    "- If you encounter an error, analyze it and try a different approach instead of giving up.\n"
    "- When the task is fully complete, call `attempt_completion` to present your result.\n"
    "- Track progress with `manage_todo` at key milestones: create todos only for multi-step work, keep exactly one `in_progress`, and mark each item complete immediately.\n"
    "- Maintain a short working plan with `manage_artifact(name=\"plan\", kind=\"plan\", status=\"draft\")` for multi-step work.\n"
    "- If a current plan already exists, treat it as the execution source of truth instead of improvising a new workflow.\n"
    "- Store durable facts and confirmed decisions with `manage_memory`; do not store long plans or temporary reports as memory.\n"
    "- Use `manage_artifact(name=\"report\", kind=\"report\", status=\"final\")` for substantial final verification notes.\n"
    "- If another mode is a better fit, call `switch_mode` or delegate focused work via `subagent__custom`. "
    "Use `subagent__read_analyze` for multi-file long-document analysis, `subagent__search` for research, and `capability__summarize_text` for one file or one long text."
)

_PLANNING_AUTONOMY_SUFFIX = (
    "\n\n"
    "## Workflow Requirements\n"
    "- Keep a concise plan in `manage_artifact(name=\"plan\", kind=\"plan\", status=\"draft\")`.\n"
    "- The plan artifact is the primary deliverable in this mode; refine it before concluding.\n"
    "- Do not implement code changes or run implementation commands in plan mode unless the user explicitly asks to leave planning and switch modes.\n"
    "- Keep todo state current with `manage_todo` for complex planning checkpoints.\n"
    "- Mark the plan status as `approved` only after user alignment; use `related`/`references` for important files and symbols.\n"
    "- If another mode is better suited, call `switch_mode`; if focused work should proceed independently, use `subagent__custom`, `subagent__read_analyze`, or `subagent__search`.\n"
    "- When the planning or orchestration task is complete, call `attempt_completion` with a concise result."
)

DEFAULT_MODES: list[ModeConfig] = [
    ModeConfig(
        slug="chat",
        name="Chat",
        role_definition="You are a helpful and precise assistant. Follow the user's instructions carefully.",
        when_to_use="日常对话、问答、写作、解释代码。",
        description="通用聊天助手，可使用模式允许的搜索和 MCP 工具",
        allowed_tool_categories=("read", "search", "mcp"),
        source="builtin",
    ),
    ModeConfig(
        slug="channel",
        name="Channel",
        role_definition=(
            "You are replying to messages coming from an external messaging channel. "
            "Be concise, helpful, and keep the reply safe for plain-text delivery in IM clients."
        ),
        when_to_use="供微信等外部频道后台 worker 使用，不在常规 UI 中展示。",
        description="外部频道专用模式",
        allowed_tool_categories=("read", "search", "manage"),
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
        allowed_tool_categories=("read", "search", "edit", "execute", "manage", "delegate", "extension", "mcp"),
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
        allowed_tool_categories=("read", "mcp", "search", "manage"),
        custom_instructions="先做信息收集，提出清晰可执行的 todo 列表；必要时提出澄清问题。",
        source="builtin",
    ),
    ModeConfig(
        slug="explore",
        name="Explore",
        role_definition=(
            "You are a careful read-only codebase explorer. Search, inspect, and summarize facts from the workspace. "
            "Do not modify files or run destructive commands; focus on evidence-backed findings. "
            "For multi-file exploration, save reusable findings in `manage_artifact(name=\"exploration\", kind=\"exploration\", status=\"draft\")` with related files and symbols."
        ),
        when_to_use="需要快速阅读项目、定位代码、回答架构/实现问题，但不直接改代码。",
        description="只读探索与代码问答",
        allowed_tool_categories=("read", "search", "mcp", "manage"),
        custom_instructions="回答时引用关键文件与符号；采用 broad-to-narrow 搜索；如需要修改，先切换到 Agent 或 Plan。",
        source="builtin",
    ),
]


def get_default_modes() -> list[ModeConfig]:
    return list(DEFAULT_MODES)


def get_primary_mode_slugs() -> tuple[str, ...]:
    return tuple(_PRIMARY_MODE_SLUGS)
