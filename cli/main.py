from __future__ import annotations

import argparse
import asyncio
import sys

from cli import APP_VERSION
from cli.executor import CliExecutor, CliRunRequest


CLI_COMMANDS = {"run", "chat", "list", "version"}


def is_cli_invocation(argv: list[str] | None = None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return False
    first = args[0]
    return first in CLI_COMMANDS or first in {"--version", "-V"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pycat", description="PyCat headless CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run one prompt and exit")
    run_parser.add_argument("prompt", nargs="?", help="Prompt text. Use '-' to read stdin.")
    _add_runtime_options(run_parser)

    chat_parser = subparsers.add_parser("chat", help="Start an interactive chat loop")
    _add_runtime_options(chat_parser)

    list_parser = subparsers.add_parser("list", help="List runtime resources")
    list_parser.add_argument("target", choices=["modes", "providers", "models", "tools", "conversations"])
    list_parser.add_argument("--output", choices=["text", "json"], default="text")
    list_parser.add_argument("--work-dir", default="")

    subparsers.add_parser("version", help="Print version and exit")
    return parser


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", default="chat")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--conversation", default="")
    parser.add_argument("--output", choices=["text", "json"], default="text")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if bool(getattr(args, "version", False)) or args.command == "version":
        print(f"PyCat {APP_VERSION}")
        return 0

    executor = CliExecutor()
    if args.command == "run":
        prompt = _read_prompt(args.prompt)
        if not prompt.strip():
            parser.error("run requires a prompt or '-' stdin input")
        return asyncio.run(executor.run_once(CliRunRequest(
            prompt=prompt,
            mode=args.mode,
            provider=args.provider,
            model=args.model,
            work_dir=args.work_dir,
            conversation_id=args.conversation,
            output=args.output,
        )))
    if args.command == "chat":
        return asyncio.run(executor.chat(
            mode=args.mode,
            provider=args.provider,
            model=args.model,
            work_dir=args.work_dir,
            output_mode=args.output,
        ))
    if args.command == "list":
        return executor.list_items(args.target, output_mode=args.output, work_dir=args.work_dir)

    parser.print_help()
    return 2


def _read_prompt(value: str | None) -> str:
    if value == "-":
        return sys.stdin.read()
    return str(value or "")


if __name__ == "__main__":
    raise SystemExit(main())
