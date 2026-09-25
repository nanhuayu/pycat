"""Common command-line entrypoints; optional frontends load only on selection."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from pycat.core.version import __version__

CLI_COMMANDS = {"exec", "resume", "model", "config", "new", "rename", "pin", "archive", "delete",
                "import", "export", "serve", "version", "doctor", "tools", "skills", "mcp", "mode",
                "agents", "materials", "memory", "knowledge", "workspace", "channels", "processes", "providers",
                'show', 'edit', 'retry', 'compact', 'permissions', 'trace', 'tasks', 'context', 'search'}


def is_cli_invocation(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    return not args or args[0] not in {"--gui", "--pycat-python-exec-worker", "--pycat-askpass"}


def _common(parser, *, runtime=False):
    parser.add_argument("--data-dir", default=argparse.SUPPRESS)
    parser.add_argument("--endpoint", default=argparse.SUPPRESS, help="Attach to a running PyCat host")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default=argparse.SUPPRESS)
    if runtime:
        parser.add_argument("-m", "--model", default=argparse.SUPPRESS, help="provider|model, or an unambiguous model name")
        parser.add_argument("--mode", default=argparse.SUPPRESS)
        parser.add_argument("-C", "--work-dir", default=argparse.SUPPRESS)
        parser.add_argument("-r", "--resume", dest="resume_target", default=argparse.SUPPRESS)
        parser.add_argument("-c", "--continue", dest="continue_last", action="store_true", default=argparse.SUPPRESS)
        parser.add_argument("--attachment", action="append", default=argparse.SUPPRESS)
        parser.add_argument("--ref", action="append", default=argparse.SUPPRESS)
        parser.add_argument("--tool-approval", choices=["default", "ask", "allow", "deny", "custom"], default=argparse.SUPPRESS)
        parser.add_argument("--filesystem-mode", choices=["confined", "full_access"], default=argparse.SUPPRESS)


def build_parser():
    parser = argparse.ArgumentParser(prog="pycat", description="PyCat agent workbench — terminal, desktop and web")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("-p", "--print", dest="print_prompt", metavar="PROMPT", help="Execute one prompt; '-' reads stdin")
    parser.add_argument("--initial-prompt", help=argparse.SUPPRESS)
    _common(parser, runtime=True)
    subs = parser.add_subparsers(dest="command")
    execute = subs.add_parser("exec", help="Execute a prompt and exit")
    execute.add_argument("prompt", nargs="?")
    _common(execute, runtime=True)
    resume = subs.add_parser("resume", help="Continue a session, or open the session picker")
    resume.add_argument("target", nargs="?")
    resume.add_argument("--last", action="store_true")
    resume.add_argument("--all", action="store_true", help="Include sessions from other workspaces")
    resume.add_argument("--list", action="store_true")
    resume.add_argument("-p", "--print", dest="print_prompt", default=argparse.SUPPRESS)
    _common(resume, runtime=True)
    subs.add_parser("version", help="Print version")
    model = subs.add_parser("model", help="List or select a model")
    model.add_argument("value", nargs="?", default="list")
    model.add_argument("--session")
    _common(model)
    config = subs.add_parser("config", help="Read or update application configuration")
    config.add_argument("action", choices=["list", "get", "set"], nargs="?", default="list")
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    config.add_argument('--session', help='Read or change this session instead of global configuration')
    _common(config)
    for name in ("new", "rename", "pin", "archive", "delete", "export", "import"):
        item = subs.add_parser(name, help=f"{name.capitalize()} a session")
        _common(item)
        if name not in {"new", "import"}:
            item.add_argument("session")
        if name == "new":
            item.add_argument("--work-dir", default="")
            item.add_argument("--title", default="")
        if name == "rename":
            item.add_argument("title")
        if name in {"archive", "pin"}:
            item.add_argument("--undo", action="store_true")
        if name == "export":
            item.add_argument("destination")
            item.add_argument("--format", choices=["markdown", "docx", "html", "json"])
        if name == "import":
            item.add_argument("path")
    serve = subs.add_parser("serve", help="Start the API and WebUI host")
    _common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--token", help="Access token; defaults to a random token")
    from pycat.core.app.services.workbench import WorkbenchService
    groups = {}
    for name, description in WorkbenchService.catalog().items():
        parts = name.split(".", 1)
        group, verb = parts if len(parts) == 2 else (parts[0], "")
        if group in {"sessions", "config", "model", "input"}:
            continue
        if not verb:
            item = subs.add_parser(group, help=description["label"])
        else:
            if group not in groups:
                groups[group] = subs.add_parser(group).add_subparsers(dest="action", required=True)
            item = groups[group].add_parser(verb, help=description["label"])
        _common(item)
        item.set_defaults(operation=name)
        for field in description["parameters"]:
            kind = field["type"]
            convert = json.loads if kind.startswith(('dict', 'list', 'bool')) else int if kind.startswith("int") else str
            item.add_argument("--" + field["name"].replace("_", "-"), required=field["required"],
                              default=field["default"], type=convert)
    for verb, operation_name in {'show': 'sessions.read', 'compact': 'sessions.compact', 'permissions': 'sessions.permissions', 'trace': 'sessions.trace', 'tasks': 'sessions.tasks', 'context': 'sessions.context'}.items():
        item = subs.add_parser(verb)
        _common(item)
        item.add_argument('session')
        item.set_defaults(operation=operation_name)
        for field in WorkbenchService.catalog()[operation_name]['parameters']:
            if field['name'] != 'session':
                item.add_argument('--' + field['name'].replace('_', '-'), default=field['default'],
                    required=field['required'],
                    type=json.loads if field['type'] in {'dict', 'list', 'bool'} else int if field['type'].startswith('int') else str)
    for verb in ('edit', 'retry'):
        item = subs.add_parser(verb, help='Replace this user turn and its following messages')
        item.add_argument('session')
        item.add_argument('message')
        if verb == 'edit':
            item.add_argument('text')
        _common(item, runtime=True)
    agents = subs.add_parser("agents", help="Agent profiles")
    agents.add_argument("action", choices=["list", "run"], nargs="?", default="list")
    agents.add_argument('profile', nargs='?')
    agents.add_argument('goal', nargs='?')
    _common(agents, runtime=True)
    return parser


def _value(text):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text


async def execute_args(args):
    from pycat.cli.executor import CliExecutor
    from pycat.cli.output import CliOutput
    from pycat.models.contracts.agent import ApplicationError, RunRequest, TurnRevision
    out = CliOutput(mode=getattr(args, "output_format", "text"))
    endpoint = getattr(args, "endpoint", "")
    services = None
    executor = None
    try:
        if endpoint:
            from pycat.cli.client import RemoteExecutor
            executor = RemoteExecutor(endpoint, out)
            wb = executor
        else:
            executor = CliExecutor(data_dir=getattr(args, "data_dir", None),
                background_curation=out.is_interactive and args.command in {None, 'resume'} and getattr(args, 'print_prompt', None) is None)
            services = executor.services
            services.run_service.bind_loop()
            wb = services.workbench
        if endpoint:
            await executor.connect()
        if args.command in {'edit', 'retry'}:
            session = await wb.execute('sessions.read', {'session': args.session})
            return await executor.run_once(RunRequest(text=getattr(args, 'text', ''), conversation_id=args.session,
                expected_revision=session['revision'], revision=TurnRevision(args.command, args.message),
                model=getattr(args, 'model', None), mode=getattr(args, 'mode', None),
                tool_approval=getattr(args, 'tool_approval', None), filesystem_mode=getattr(args, 'filesystem_mode', None)), out)
        if args.command == 'agents':
            if args.action == 'list':
                rows = await wb.execute('mode.list', {'work_dir': getattr(args, 'work_dir', '')})
                out.result([row for row in rows if row.get('profile_kind') in {'subagent', 'both'}])
                return 0
            if not args.profile or not args.goal:
                raise ValueError('agents run requires PROFILE GOAL')
            args.print_prompt = f'/agents run {args.profile} {args.goal}'
        operation = getattr(args, "operation", "")
        if operation:
            fields = wb.catalog()[operation]["parameters"]
            result = await wb.execute(operation, {field["name"]: getattr(args, field["name"]) for field in fields},
                                      approval_callback=executor.approval(out))
            out.result(result)
            return 1 if isinstance(result, dict) and result.get('ok') is False else 0
        if args.command == "config":
            view = await wb.execute('sessions.settings', {'session': args.session}) if args.session else await wb.execute("config.read", {})
            if args.action == "list":
                out.result(view)
            elif args.action == "get":
                value = view if args.session else view["values"]
                for part in (args.key or "").split("."):
                    value = value[part]
                out.result(value)
            else:
                if not args.key or args.value is None:
                    raise ValueError("config set requires KEY VALUE")
                patch = _value(args.value)
                for part in reversed(args.key.split(".")):
                    patch = {part: patch}
                result = await wb.execute('sessions.settings', {'session': args.session, **patch, 'expected_revision': view['revision']}) if args.session else await wb.execute("config.update", {"patch": patch, "expected_revision": view["revision"]})
                out.result(result)
                if result.get('ok') is False:
                    return 1
            return 0
        if args.command == "model":
            if args.value == "list":
                out.result(await wb.execute("model.list", {}))
            elif not args.session:
                raise ValueError("model selection requires --session ID, or use --model when starting a run")
            else:
                out.result(await wb.execute("sessions.select", {"session": args.session, "model": args.value}))
            return 0
        if args.command in {"new", "rename", "pin", "archive", "delete", "import", "export"}:
            command = args.command
            data = {}
            if command not in {"new", "import"}:
                data["session"] = args.session
            for key in {"new": ("work_dir", "title"), "rename": ("title",), "import": ("path",),
                        "export": ("destination", "format")}.get(command, ()):
                data[key] = getattr(args, key)
            if command in {"pin", "archive"}:
                data["pinned" if command == "pin" else "archived"] = not args.undo
            out.result(await wb.execute("sessions." + ("create" if command == "new" else command), data))
            return 0
        work_dir = getattr(args, "work_dir", None)
        target = getattr(args, "resume_target", "") or getattr(args, "target", "") or ""
        last = getattr(args, "continue_last", False) or getattr(args, "last", False)
        scope = None if getattr(args, "all", False) else (('' if endpoint else os.getcwd()) if work_dir is None else work_dir)
        session = None
        if args.command == "resume" and (getattr(args, "list", False) or not out.is_interactive and not target and not last):
            out.result(await wb.execute("sessions.list", {"work_dir": scope}))
            return 0
        if target or last:
            session = await wb.execute("sessions.resume", {"target": target, "last": last, "work_dir": scope})
        prompt = getattr(args, "print_prompt", None)
        if args.command == "exec":
            prompt = args.prompt
        if prompt is not None or args.command == "exec":
            if prompt == "-" or prompt is None and not out.is_interactive:
                prompt = sys.stdin.read()
            if not str(prompt or "").strip() and not getattr(args, "attachment", None):
                raise ValueError("exec requires a prompt or '-' stdin input")
            request = RunRequest(text=prompt or "", conversation_id=session["id"] if session else None,
                work_dir=work_dir if session else scope, model=getattr(args, "model", None), mode=getattr(args, "mode", None),
                attachments=tuple({"path": path} for path in getattr(args, "attachment", ())),
                references=tuple(getattr(args, "ref", ())), tool_approval=getattr(args, "tool_approval", None),
                filesystem_mode=getattr(args, "filesystem_mode", None))
            return await executor.run_once(request, out)
        if not out.is_interactive:
            raise ValueError("An interactive terminal is required. Use pycat exec PROMPT or pycat --help.")
        try:
            from pycat.tui.app import run_tui
        except ModuleNotFoundError as exc:
            if str(exc.name).split('.', 1)[0] not in {"textual", "rich"}:
                raise
            raise ValueError('Install the terminal interface with: pip install "pycat[tui]"') from exc
        if getattr(args, 'model', None) or getattr(args, 'mode', None):
            if session is None:
                session = await wb.execute('sessions.create', {'work_dir': scope or ''})
            session = await wb.execute('sessions.select', {'session': session['id'],
                'model': getattr(args, 'model', None), 'mode': getattr(args, 'mode', None)})
        if getattr(args, 'tool_approval', None) or getattr(args, 'filesystem_mode', None):
            if session is None:
                session = await wb.execute('sessions.create', {'work_dir': scope or ''})
            await wb.execute('sessions.permissions', {'session': session['id'],
                'tool_approval': getattr(args, 'tool_approval', None), 'filesystem_mode': getattr(args, 'filesystem_mode', None)})
        if services:
            channels = services.channel_service.runtime_channels(services.settings_update_service.load())
            if channels:
                await asyncio.to_thread(services.channel_gateway.start, channels)
        else:
            executor.client.source = 'tui'
        await run_tui(services, client=executor.client if endpoint else None, session=session, work_dir=scope or "",
                      initial_prompt=getattr(args, "initial_prompt", "") or "",
                      open_sessions=args.command == "resume" and not session)
        return 0
    except (ApplicationError, ValueError, RuntimeError, OSError, KeyError) as exc:
        out.final(status="failed", error=str(exc))
        return 2
    finally:
        if executor is not None:
            await executor.aclose()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and argv[0] not in CLI_COMMANDS and argv[0] not in {"run", "chat", "list"}:
        argv = ["--initial-prompt", argv[0], *argv[1:]]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version or args.command == "version":
        print(f"PyCat {__version__}")
        return 0
    if args.command == "serve":
        try:
            from pycat.web.server import serve
            return serve(args)
        except ModuleNotFoundError as exc:
            if exc.name not in {"fastapi", "uvicorn"}:
                raise
            print('Install the web interface with: pip install "pycat[web]"', file=sys.stderr)
            return 2
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        return asyncio.run(execute_args(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
