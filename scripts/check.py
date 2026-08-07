#!/usr/bin/env python
"""Unified quality gate entry point (orchestration only).

Usage:
    python scripts/check.py -Scope fast|full|gui|docs

Scopes:
    fast  compileall + unit/runtime/cli/channel tests + docs gates
    gui   offscreen Qt GUI tests (tests/gui_tests)
    docs  docs/check_docs.py + tool catalog generation check
    full  fast + gui
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(label: str, argv: list[str], *, env: dict | None = None) -> int:
    print(f"==> {label}", flush=True)
    result = subprocess.run(argv, cwd=ROOT, env=env)
    if result.returncode != 0:
        print(f"XX {label} failed (exit {result.returncode})", flush=True)
    return result.returncode


def _docs_steps(py: str) -> list[tuple[str, list[str], dict | None]]:
    return [
        ("docs check", [py, "docs/check_docs.py"], None),
        ("tool catalog check", [py, "docs/generate_tool_catalog.py", "--check"], None),
    ]


def _test_env() -> dict[str, str]:
    env = dict(os.environ)
    # Some runtime/unit modules exercise Qt projections and dialogs. Keeping a
    # single offscreen test environment makes local and Linux CI behavior match.
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _fast_steps(py: str) -> list[tuple[str, list[str], dict | None]]:
    return [
        ("compileall", [py, "-m", "compileall", "core", "gui", "models", "cli", "tests", "-q"], None),
        (
            "boundary/unit/runtime/cli/channel tests",
            [py, "-m", "pytest", "tests/unit", "tests/runtime", "tests/cli_tests", "tests/channels", "-q"],
            _test_env(),
        ),
        *_docs_steps(py),
    ]


def _gui_steps(py: str) -> list[tuple[str, list[str], dict | None]]:
    return [("gui tests (offscreen)", [py, "-m", "pytest", "tests/gui_tests", "-q"], _test_env())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PyCat quality gate orchestrator")
    parser.add_argument(
        "-Scope",
        "--scope",
        dest="scope",
        choices=["fast", "full", "gui", "docs"],
        default="fast",
    )
    args = parser.parse_args(argv)

    py = sys.executable
    if args.scope == "fast":
        steps = _fast_steps(py)
    elif args.scope == "gui":
        steps = _gui_steps(py)
    elif args.scope == "docs":
        steps = _docs_steps(py)
    else:
        steps = [*_fast_steps(py), *_gui_steps(py)]

    failures = sum(1 for label, argv, env in steps if _run(label, argv, env=env) != 0)
    if failures:
        print(f"CHECK FAILED: {failures} step(s) failed", flush=True)
        return 1
    print("CHECK PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
