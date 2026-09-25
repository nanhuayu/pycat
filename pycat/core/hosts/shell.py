"""Local shell resolution shared by execution and environment descriptions."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from pycat.models.contracts.config import ShellConfig


def available_shells() -> list[tuple[str, str]]:
    names = [("powershell", "pwsh"), ("cmd", "cmd.exe"), ("powershell", "powershell.exe"),
             ("wsl", "wsl.exe")] if os.name == "nt" else [
                 ("bash", "bash"), ("zsh", "zsh"), ("sh", "sh"), ("fish", "fish"), ("powershell", "pwsh")]
    return [(kind, path) for kind, name in names if (path := shutil.which(name))]


def resolve_shell(config: ShellConfig, *, interactive: bool = False) -> tuple[str, str]:
    kind, program = config.backend, config.executable
    if kind == "auto":
        if program:
            kind = Path(program).stem.lower()
            kind = "powershell" if kind in {"pwsh", "powershell"} else kind
        elif os.name == "nt":
            kind, program = next(iter(available_shells()), ("cmd", os.environ.get("ComSpec", "cmd.exe")))
        else:
            program = os.environ.get("SHELL", "") if interactive else "/bin/sh"
            program = program or "/bin/sh"
            kind = Path(program).name
    if kind in {"cmd", "wsl"} and os.name != "nt":
        raise ValueError(f"{kind} requires a Windows execution host")
    if not program:
        program = {"cmd": os.environ.get("ComSpec", "cmd.exe"), "wsl": "wsl.exe",
                   "powershell": shutil.which("pwsh") or ("powershell.exe" if os.name == "nt" else "pwsh")}.get(kind, kind)
    executable = shutil.which(program)
    if not executable:
        raise FileNotFoundError(f"Shell executable unavailable: {program}")
    return kind, executable


def shell_command(command: str, cwd: Path, config: ShellConfig, *, interactive: bool = False) -> list[str]:
    kind, program = resolve_shell(config, interactive=interactive)
    args = [program, *config.arguments]
    if kind == "wsl":
        if config.wsl_distro:
            args += ["--distribution", config.wsl_distro]
        # wsl --cd resolves Windows paths using the selected distro's real mounts.
        args += ["--cd", str(cwd)]
        if command or not interactive:
            args += ["--exec", "sh", "-c", command]
        return args
    if kind == "cmd":
        return args + (["/d", "/q"] if interactive and not command else ["/d", "/s", "/c", command])
    if kind == "powershell":
        return args + (["-NoLogo"] if interactive and not command else ["-NoLogo", "-NoProfile", "-Command", command])
    return args + (["-i"] if interactive and not command else ["-c", command])


def process_invocation(command: str, cwd: Path, config: ShellConfig):
    args = shell_command(command, cwd, config)
    if os.name == "nt" and resolve_shell(config)[0] == "cmd":
        return f'{subprocess.list2cmdline(args[:-1])} "{command}"'
    return args
