"""Command system.

Explicit commands use ``/``. Typed references use ``@``.

Extensible via ``~/.pycat/commands/`` or ``.pycat/commands/``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from pycat.core.commands.dispatcher import dispatch_command
from pycat.core.commands.parser import (
    extract_command_query,
    find_command_invocation,
    is_slash_command,
    parse_command_text,
)
from pycat.core.commands.types import (
    CommandAction,
    CommandPresentation,
    CommandResult,
    PromptInvocation,
    ShellInvocation,
    SlashCommand,
)
from pycat.core.commands.mentions import (
    MentionCandidate,
    MentionKind,
    MentionQuery,
    MentionResolver,
    extract_mention_query,
)
from pycat.core.skills import SkillsManager
from pycat.core.skills import resolve_skill_invocation_spec

logger = logging.getLogger(__name__)


class CommandRegistry:
    """Registry of commands and inline mention providers."""

    def __init__(self, *, data_dir=None) -> None:
        self.data_dir = data_dir
        self._commands: Dict[str, SlashCommand] = {}
        self.mention_provider = None
        self.argument_provider = None
        self._mention_triggers: Dict[str, Callable[[MentionQuery, Dict[str, Any]], List[MentionCandidate]]] = {
            "@": self._resolve_file_mentions,
        }
        self._register_builtins()

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def get(self, name: str) -> Optional[SlashCommand]:
        return self._commands.get(name)

    def list_commands(self) -> List[SlashCommand]:
        seen = set()
        result = []
        for cmd in self._commands.values():
            if id(cmd) not in seen:
                seen.add(id(cmd))
                result.append(cmd)
        return result

    def has(self, name: str) -> bool:
        return bool(name) and name in self._commands

    def get_placeholder_hints(self, *, limit: int = 4) -> List[str]:
        hints: List[str] = []
        for cmd in self.list_commands():
            presentation = getattr(cmd, "presentation", None)
            if not presentation or not presentation.include_in_placeholder:
                continue
            hint = (
                presentation.placeholder_hint
                or presentation.usage
                or f"/{cmd.name}"
            ).strip()
            if not hint or hint in hints:
                continue
            hints.append(hint)
            if limit > 0 and len(hints) >= limit:
                break
        return hints

    def build_input_placeholder(self) -> str:
        slash_hints = "，".join(self.get_placeholder_hints(limit=4))
        command_hint = f"{slash_hints}，" if slash_hints else ""
        return f"输入消息... ({command_hint}@文件或 Agent，/skill-name)"

    def get_menu_presentation(self, name: str) -> Optional[CommandPresentation]:
        cmd = self.get(name)
        return getattr(cmd, "presentation", None) if cmd else None

    def is_command(self, text: str, context: Optional[Dict[str, Any]] = None) -> bool:
        invocation = find_command_invocation(text)
        prefix, cmd_name, _args = parse_command_text(text)
        if not prefix or not cmd_name:
            return False
        if self.has(cmd_name):
            return bool(invocation and not invocation.leading_text)
        return bool(prefix == "/" and self._resolve_skill_alias(cmd_name, context))

    def is_slash_command(self, text: str) -> bool:
        return is_slash_command(text) and self.is_command(text)

    def parse(self, text: str) -> tuple[str, str]:
        """Parse command text → ``("command", "args")``."""
        _prefix, cmd_name, args = parse_command_text(text)
        return (cmd_name, args)

    def get_mention_candidates(
        self,
        text: str,
        cursor_pos: int,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[tuple[MentionQuery, List[MentionCandidate]]]:
        """Resolve inline mention candidates for the cursor location."""
        query = extract_mention_query(text, cursor_pos, triggers=self._mention_triggers.keys())
        if not query:
            return None
        resolver = self._mention_triggers.get(query.trigger)
        if resolver is None:
            return None
        candidates = resolver(query, context or {})
        if not candidates:
            return None
        return query, candidates

    def get_command_candidates(
        self,
        text: str,
        cursor_pos: int,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[tuple[MentionQuery, List[MentionCandidate]]]:
        query_info = extract_command_query(text, cursor_pos, prefixes=("/",))
        if not query_info:
            return None

        trigger, command_body, start_pos, end_pos = query_info
        query = MentionQuery(
            trigger=trigger,
            prefix=command_body,
            start_pos=start_pos,
            end_pos=end_pos,
        )

        ctx = {**(context or {}), '_inline': bool(text[:start_pos].strip())}
        candidates = self._resolve_command_candidates(trigger, command_body, ctx)
        if not candidates:
            return None
        return query, candidates

    def resolve_mention_candidate(
        self,
        candidate: MentionCandidate,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Resolve a selected mention candidate into its backing value."""
        ctx = context or {}
        if candidate.kind == MentionKind.FILE:
            work_dir = str(ctx.get("work_dir") or "")
            if not work_dir:
                return None
            return MentionResolver(work_dir).resolve(candidate)
        return candidate.value

    def execute(self, text: str, context: Optional[Dict[str, Any]] = None) -> Optional[CommandResult]:
        """Execute a registered command and return a CommandResult."""
        try:
            context = {**(context or {}), "data_dir": self.data_dir}
            result = dispatch_command(text, commands=self._commands, context=context)
            if result is not None:
                return result

            invocation = find_command_invocation(text)
            if not invocation:
                return None

            prefix, cmd_name, _args = parse_command_text(text)
            if prefix == "/":
                skill_name = self._resolve_skill_name(cmd_name, context)
                if skill_name:
                    work_dir = str((context or {}).get("work_dir") or "")
                    skill = SkillsManager(work_dir, data_dir=self.data_dir).get(skill_name)
                    spec = resolve_skill_invocation_spec(skill) if skill is not None else None
                    return CommandResult(
                        action=CommandAction.PROMPT_RUN,
                        data=PromptInvocation(
                            content=invocation.surrounding_text,
                            mode_slug=(spec.mode if spec is not None else "agent"),
                            metadata={
                                "skill_run": {
                                    "name": skill_name,
                                    "user_input": invocation.surrounding_text,
                                    "invoke_mode": "run",
                                    "mode": spec.mode if spec is not None else "agent",
                                    "tool_selection": spec.tool_selection.to_dict() if spec is not None else None,
                                }
                            },
                            source_prefix=prefix,
                            original_text=str(text or "").strip(),
                        ),
                    )
            return None
        except Exception as e:
            return CommandResult(action=CommandAction.DISPLAY, display_text=f"Command error: {e}")

    def _resolve_skill_name(self, name: str, context: Optional[Dict[str, Any]]) -> str:
        normalized = str(name or "").strip().lower()
        if not normalized:
            return ""
        work_dir = str((context or {}).get("work_dir") or "")
        skill = SkillsManager(work_dir, data_dir=self.data_dir).get(normalized)
        if skill is not None and not resolve_skill_invocation_spec(skill).user_invocable:
            return ""
        return skill.name if skill else ""

    def _resolve_skill_alias(self, name: str, context: Optional[Dict[str, Any]]) -> str:
        return self._resolve_skill_name(name, context)

    def _list_skill_candidates(self, work_dir: str) -> List[Any]:
        try:
            return SkillsManager(work_dir or "", data_dir=self.data_dir).list_skills()
        except Exception:
            return []

    def _resolve_command_candidates(
        self,
        trigger: str,
        command_body: str,
        context: Dict[str, Any],
    ) -> List[MentionCandidate]:
        body = str(command_body or "")
        stripped = body.lstrip()
        lowered = stripped.lower()
        work_dir = str(context.get("work_dir") or "")

        candidates: List[MentionCandidate] = []
        if not stripped or not any(char.isspace() for char in stripped):
            name_filter = lowered.strip()
            commands = self.list_commands()
            seen: set[str] = set()
            for cmd in commands:
                if context.get('_inline'):
                    continue
                if cmd.name in seen:
                    continue
                haystack = f"{cmd.name} {cmd.description}".lower()
                if name_filter and name_filter not in haystack:
                    continue
                seen.add(cmd.name)
                presentation = getattr(cmd, "presentation", CommandPresentation())
                usage = (presentation.usage or f"/{cmd.name}").strip()
                display_usage = usage.replace("/", trigger, 1) if usage.startswith("/") else usage
                insert_text = (
                    presentation.completion_text.lstrip()
                    if presentation.completion_text
                    else f"{trigger}{cmd.name}{' ' if presentation.takes_argument else ''}"
                )
                if insert_text.startswith("/"):
                    insert_text = insert_text.replace("/", trigger, 1)
                candidates.append(
                    MentionCandidate(
                        label=f"{display_usage} - {cmd.description}",
                        value=cmd.name,
                        kind=MentionKind.COMMAND,
                        insert_text=insert_text,
                        submit_on_accept=presentation.submit_on_accept,
                    )
                )

            if trigger == "/":
                for skill in self._list_skill_candidates(work_dir):
                    skill_name = str(getattr(skill, "name", "") or "").strip().lower()
                    description = str(getattr(skill, "description", "") or "").strip()
                    spec = resolve_skill_invocation_spec(skill)
                    if not spec.user_invocable:
                        continue
                    haystack = f"{skill_name} {description}".lower()
                    if name_filter and name_filter not in haystack:
                        continue
                    candidates.append(
                        MentionCandidate(
                            label=f"/{skill_name} - Skill: {description or skill_name}",
                            value=skill_name,
                            kind=MentionKind.COMMAND,
                            insert_text=f"/{skill_name} ",
                        )
                    )
            return candidates[:30]
        if self.argument_provider is not None and not context.get('_inline'):
            parts = stripped.split(maxsplit=1)
            command = self.get(parts[0])
            if command:
                return self.argument_provider(command.name, parts[1] if len(parts) > 1 else '', context)[:30]
        return []

    def _resolve_file_mentions(
        self,
        query: MentionQuery,
        ctx: Dict[str, Any],
    ) -> List[MentionCandidate]:
        if self.mention_provider is not None:
            return self.mention_provider(query, ctx)
        work_dir = str(ctx.get("work_dir") or "")
        if not work_dir:
            return []
        return MentionResolver(work_dir).search(query.prefix)

    # ------------------------------------------------------------------
    # Built-in commands
    # ------------------------------------------------------------------

    def _register_builtins(self) -> None:
        from pycat.core.commands import handlers as h

        self.register(SlashCommand(
            name="help",
            description="Show available slash commands",
            handler=lambda args, ctx: h.cmd_help(args, ctx, list_commands=self.list_commands),
            presentation=CommandPresentation(
                usage="/help",
                completion_text="/help",
                submit_on_accept=True,
            ),
        ))
        self.register(SlashCommand(
            name="compact",
            description="Manually condense the conversation context",
            handler=h.cmd_compact,
            presentation=CommandPresentation(
                usage="/compact",
                completion_text="/compact",
                menu_label="压缩上下文",
                menu_tooltip="压缩较早历史为 summary，同时保留最近完整轮次。",
                placeholder_hint="/compact",
                include_in_placeholder=True,
            ),
        ))
        self.register(SlashCommand(
            name="mode",
            description="Switch conversation mode (e.g. /mode code)",
            handler=h.cmd_mode,
            presentation=CommandPresentation(
                usage="/mode <slug>",
                completion_text="/mode ",
                takes_argument=True,
                submit_on_accept=True,
            ),
        ))
        self.register(SlashCommand(
            name="tools",
            description="List available tools for the current mode",
            handler=h.cmd_tools,
            presentation=CommandPresentation(
                usage="/tools",
                completion_text="/tools",
            ),
        ))
        self.register(SlashCommand(
            name="new",
            aliases=("clear",),
            description="Start a new session; keep the previous session in history",
            handler=h.cmd_clear,
            presentation=CommandPresentation(
                usage="/new",
                completion_text="/new",
                submit_on_accept=True,
                menu_label="清空并新建会话",
                menu_tooltip="清空当前会话上下文，并立即切换到一个新会话。",
                placeholder_hint="/new",
                include_in_placeholder=True,
            ),
        ))
        self.register(SlashCommand(
            name="skills",
            description="List available skills",
            handler=h.cmd_skills,
            presentation=CommandPresentation(
                usage="/skills",
                completion_text="/skills",
            ),
        ))
        self.register(SlashCommand(
            name="plan",
            description="Switch to Plan mode, or plan a request",
            handler=h.cmd_plan,
            presentation=CommandPresentation(
                usage="/plan <content>",
                completion_text="/plan ",
                takes_argument=True,
            ),
        ))
        self.register(SlashCommand(
            name="memory",
            description="View or update the session memory document",
            handler=h.cmd_memory,
            presentation=CommandPresentation(
                usage="/memory <content>",
                completion_text="/memory ",
                takes_argument=True,
            ),
        ))
        self.register(SlashCommand(
            name="export",
            description="Export conversation (e.g. /export markdown)",
            handler=h.cmd_export,
            presentation=CommandPresentation(
                usage="/export <format>",
                completion_text="/export ",
                takes_argument=True,
            ),
        ))
        for name, action, description, aliases in (
            ("resume", CommandAction.RESUME, "Resume a session by ID or name", ()),
            ("model", CommandAction.MODEL_SWITCH, "Choose a configured model", ()),
            ("rename", CommandAction.RENAME, "Rename the current session", ()),
            ("exit", CommandAction.EXIT, "Exit the client", ("quit",)),
        ):
            self.register(SlashCommand(name, description,
                lambda args, ctx, action=action: CommandResult(action, args.strip()),
                CommandPresentation(usage=f"/{name}", takes_argument=name != "exit", submit_on_accept=True), aliases))
        self.register(SlashCommand("review", "Review a request in Review mode",
            lambda args, ctx: h.mode_prompt("review", args), CommandPresentation(usage="/review [request]", takes_argument=True)))
        self.register(SlashCommand('agents', 'Inspect profiles or explicitly run a delegated agent', h.cmd_agents,
            CommandPresentation(usage='/agents [run PROFILE GOAL]', takes_argument=True)))
        for name, description in (
            ("config", "Edit application configuration"), ("status", "Show session and runtime status"),
            ("permissions", "Inspect or change tool and filesystem access"), ("mcp", "Manage MCP servers"),
            ("channels", "Manage channel connections and bindings"),
            ("context", "Inspect session materials and context"), ("copy", "Copy the last response"),
            ("doctor", "Inspect application diagnostics"),
        ):
            self.register(SlashCommand(name, description,
                lambda args, ctx, name=name: CommandResult(CommandAction.OPEN_PANEL, {"name": name, "args": args.strip()}),
                CommandPresentation(usage=f"/{name}", takes_argument=True, submit_on_accept=True)))
