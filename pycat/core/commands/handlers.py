"""Built-in command handlers.

Extracted from ``CommandRegistry`` to keep it focused on
registration / lookup / dispatch.
"""
from __future__ import annotations

from typing import Any, Dict

from pycat.core.commands.types import CommandAction, CommandResult, PromptInvocation
from pycat.core.config import get_global_subdir
from pycat.core.memory.service import MemoryService
from pycat.core.skills import SkillsManager


def cmd_help(args: str, ctx: Dict[str, Any], *, list_commands) -> str:
    """Show available commands."""
    lines = ["**Available commands** (`/` only):"]
    for cmd in list_commands():
        usage = (getattr(cmd.presentation, "usage", "") or f"/{cmd.name}").strip()
        lines.append(f"  `{usage}` — {cmd.description}")
    lines.append("")
    lines.append("Use `/{skill-name}` to run a skill. Use `@` to select a file, agent or channel.")
    lines.append(
        "`!<command>` is controlled by Settings → Terminal: in Shell mode it explicitly runs `shell__run` "
        "under the `command` permission category; in Agent mode it is sent as normal user text."
    )
    return "\n".join(lines)


def cmd_compact(args: str, ctx: Dict[str, Any]) -> CommandResult:
    return CommandResult(action=CommandAction.COMPACT)


def cmd_mode(args: str, ctx: Dict[str, Any]) -> CommandResult:
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

    mgr = SkillsManager(str(ctx.get("work_dir") or ""), data_dir=ctx.get("data_dir"))
    skills = mgr.list_skills()
    if not skills:
        return CommandResult(
            action=CommandAction.DISPLAY,
            display_text=(
                "No skills found. Add a skill directory with `SKILL.md` to "
                f"`{get_global_subdir('skills', data_dir=ctx.get('data_dir'))}` or `.pycat/skills/`."
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


def mode_prompt(mode: str, args: str) -> CommandResult:
    if not args.strip():
        return CommandResult(CommandAction.MODE_SWITCH, mode)
    return CommandResult(CommandAction.PROMPT_RUN, PromptInvocation(
        content=args.strip(), mode_slug=mode,
        metadata={"command_run": {"name": mode, "source": "slash_command"}},
        original_text=f"/{mode} {args.strip()}",
    ))


def cmd_plan(args: str, ctx: Dict[str, Any]) -> CommandResult:
    return mode_prompt("plan", args)


def cmd_agents(args: str, ctx: Dict[str, Any]) -> CommandResult:
    parts = args.strip().split(maxsplit=2)
    if not parts or parts == ['list']:
        return CommandResult(CommandAction.OPEN_PANEL, {'name': 'agents', 'args': ''})
    if len(parts) != 3 or parts[0] != 'run':
        return CommandResult(CommandAction.DISPLAY, display_text='Usage: /agents run PROFILE GOAL')
    return CommandResult(CommandAction.PROMPT_RUN, PromptInvocation(content=parts[2], mode_slug='agent',
        delegate_profile=parts[1], original_text='/agents ' + args,
        metadata={'command_run': {'name': 'agents', 'profile': parts[1], 'source': 'slash_command'}}))


def cmd_memory(args: str, ctx: Dict[str, Any]) -> str:
    conv = ctx.get("conversation")
    if not conv:
        return "No active conversation."
    work_dir = str(getattr(conv, "work_dir", "") or "")
    if args.strip():
        key, eq, value = args.partition("=")
        content = value.strip() if eq and key.strip() and value.strip() else args.strip()
        ok, message = MemoryService.handle_tool_action(
            work_dir=work_dir,
            action="add",
            target="memory",
            content=content,
            conversation=conv,
            data_dir=getattr(conv, "data_dir", None),
        )
        if ok:
            return f"Memory saved ({message})"
        return f"Memory save failed: {message}"
    sections = []
    for target_info in MemoryService.describe_targets(work_dir, data_dir=getattr(conv, "data_dir", None)):
        entries = target_info["entries"]
        body = "\n".join(f"  - {entry.splitlines()[0] if entry else ''}" for entry in entries)
        sections.append(
            f"**{target_info['target']}** ({target_info['used_chars']}/{target_info['char_limit']} chars)\n"
            + (body or "  (empty)")
        )
    return "\n\n".join(sections)


def cmd_export(args: str, ctx: Dict[str, Any]) -> CommandResult:
    fmt = args.strip() or "markdown"
    return CommandResult(action=CommandAction.EXPORT, data=fmt)
