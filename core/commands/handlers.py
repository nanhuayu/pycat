"""Built-in command handlers.

Extracted from ``CommandRegistry`` to keep it focused on
registration / lookup / dispatch.
"""
from __future__ import annotations

from typing import Any, Dict

from core.commands.types import CommandAction, CommandResult, PromptInvocation
from core.state.services.document_service import DocumentService


def cmd_help(args: str, ctx: Dict[str, Any], *, list_commands) -> str:
    """Show available commands."""
    lines = ["**Available commands** (`/` only):"]
    for cmd in list_commands():
        usage = (getattr(cmd.presentation, "usage", "") or f"/{cmd.name}").strip()
        lines.append(f"  `{usage}` — {cmd.description}")
    lines.append("")
    lines.append("Use `/{skill-name}` to run a skill directly. Use `!<shell command>` for explicit shell execution. Use `#path/to/file` for file references.")
    return "\n".join(lines)


def cmd_compact(args: str, ctx: Dict[str, Any]) -> CommandResult:
    return CommandResult(action=CommandAction.COMPACT)


def cmd_mode(args: str, ctx: Dict[str, Any]) -> CommandResult:
    if not args.strip():
        current = ctx.get("current_mode", "chat")
        return CommandResult(
            action=CommandAction.DISPLAY,
            display_text=f"Current mode: **{current}**. Usage: `/mode <slug>`.",
        )
    return CommandResult(action=CommandAction.MODE_SWITCH, data=args.strip())


def cmd_tools(args: str, ctx: Dict[str, Any]) -> str:
    tools = ctx.get("available_tools", [])
    if not tools:
        return "No tools available in current mode."
    lines = ["**Available tools:**"]
    for t in tools:
        fn = t.get("function", {})
        lines.append(f"  `{fn.get('name', '?')}` — {fn.get('description', '')[:80]}")
    return "\n".join(lines)


def cmd_clear(args: str, ctx: Dict[str, Any]) -> CommandResult:
    return CommandResult(action=CommandAction.CLEAR)


def cmd_skills(args: str, ctx: Dict[str, Any]) -> CommandResult:
    from core.skills import SkillsManager
    from core.config import get_global_subdir

    mgr = SkillsManager(str(ctx.get("work_dir") or "."))
    skills = mgr.list_skills()
    if not skills:
        return CommandResult(
            action=CommandAction.DISPLAY,
            display_text=(
                "No skills found. Add a skill directory with `SKILL.md` to "
                f"`{get_global_subdir('skills')}` or `.pycat/skills/`."
            ),
        )

    lines = ["**Available skills:**"]
    for s in skills:
        tags = f" [{', '.join(s.tags)}]" if s.tags else ""
        desc = f" — {s.description}" if s.description else ""
        lines.append(f"  `/{s.name}`{tags}{desc}")
    lines.append("")
    lines.append("Run a skill directly with `/{skill-name} <your request>`.")
    return CommandResult(action=CommandAction.DISPLAY, display_text="\n".join(lines))


def cmd_plan(args: str, ctx: Dict[str, Any]) -> CommandResult | str:
    if args.strip():
        plan_text = args.strip()
        return CommandResult(
            action=CommandAction.PROMPT_RUN,
            data=PromptInvocation(
                content=plan_text,
                mode_slug="plan",
                metadata={
                    "command_run": {
                        "name": "plan",
                        "source": "slash_command",
                    }
                },
                document_updates={
                    "plan": {
                        "content": plan_text,
                        "abstract": "当前请求的活动执行计划",
                        "kind": "plan",
                    }
                },
                source_prefix="/",
                original_text=f"/plan {plan_text}",
            ),
        )

    conv = ctx.get("conversation")
    if not conv:
        return "No active conversation."
    state = conv.get_state()
    doc = state.documents.get("plan")
    if doc and doc.content:
        return f"**Session Plan:**\n{doc.content}"
    return "No plan set. Usage: `/plan <content>` to set one."


def cmd_memory(args: str, ctx: Dict[str, Any]) -> str:
    conv = ctx.get("conversation")
    if not conv:
        return "No active conversation."
    state = conv.get_state()
    if args.strip():
        # Parse "key=value" or just append to memory doc
        if "=" in args:
            key, _, value = args.partition("=")
            state.memory[key.strip()] = value.strip()
            conv.set_state(state)
            return f"Memory '{key.strip()}' saved."
        else:
            DocumentService.append_document(
                state,
                name="memory",
                content=args.strip(),
                current_seq=int(conv.current_seq_id() or 0),
                kind="memory",
            )
            conv.set_state(state)
            return "Memory appended."
    # Show current memory
    lines = []
    if state.memory:
        lines.append("**Key-Value Memory:**")
        for k, v in state.memory.items():
            lines.append(f"  - **{k}**: {v}")
    doc = state.documents.get("memory")
    if doc and doc.content:
        lines.append(f"\n**Memory Document:**\n{doc.content}")
    if not lines:
        return "No memory stored. Usage: `/memory key=value` or `/memory <text>`"
    return "\n".join(lines)


def cmd_export(args: str, ctx: Dict[str, Any]) -> CommandResult:
    fmt = args.strip() or "markdown"
    return CommandResult(action=CommandAction.EXPORT, data=fmt)
