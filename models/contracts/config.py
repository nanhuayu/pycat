from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

from models.contracts.capability import CapabilitiesConfig
from models.contracts.channel import ChannelConfig
from models.contracts.tooling import ToolPermissionConfig, ToolPolicy


DEFAULT_ACCENT = "lavender"
SUPPORTED_ACCENTS = ("blue", "lavender")


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def _as_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        s = str(v)
    except Exception:
        return default
    return s


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    try:
        return bool(v)
    except Exception:
        return default


def _as_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _clamp_int(v: int, lo: int | None = None, hi: int | None = None) -> int:
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


@dataclass(frozen=True)
class CompressionPolicyConfig:
    tight_replay_threshold_ratio: float = 0.35
    token_threshold_ratio: float = 0.80
    history_keep_last_turns: int = 3

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CompressionPolicyConfig":
        d = _as_dict(dict(data) if data is not None else {})
        compact_ratio = max(0.11, min(0.95, _as_float(d.get("token_threshold_ratio"), 0.80)))
        tight_ratio = max(0.10, min(0.94, _as_float(d.get("tight_replay_threshold_ratio"), 0.35)))
        tight_ratio = min(tight_ratio, compact_ratio - 0.01)
        return CompressionPolicyConfig(
            tight_replay_threshold_ratio=tight_ratio,
            token_threshold_ratio=compact_ratio,
            history_keep_last_turns=_clamp_int(_as_int(d.get("history_keep_last_turns"), 3), 1, 200),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tight_replay_threshold_ratio": float(self.tight_replay_threshold_ratio),
            "token_threshold_ratio": float(self.token_threshold_ratio),
            "history_keep_last_turns": int(self.history_keep_last_turns),
        }


@dataclass(frozen=True)
class ContextConfig:
    agent_auto_compress_enabled: bool = True
    compression_policy: CompressionPolicyConfig = field(default_factory=CompressionPolicyConfig)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ContextConfig":
        d = _as_dict(dict(data) if data is not None else {})
        return ContextConfig(
            agent_auto_compress_enabled=_as_bool(d.get("agent_auto_compress_enabled"), True),
            compression_policy=CompressionPolicyConfig.from_dict(_as_dict(d.get("compression_policy"))),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_auto_compress_enabled": bool(self.agent_auto_compress_enabled),
            "compression_policy": self.compression_policy.to_dict(),
        }


@dataclass(frozen=True)
class PromptsConfig:
    global_instructions: str = ""
    include_environment: bool = True
    file_tree_max_depth: int = 2

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "PromptsConfig":
        d = _as_dict(dict(data) if data is not None else {})
        return PromptsConfig(
            global_instructions=_as_str(
                d.get("global_instructions")
                or d.get("agent_tool_guidelines")
                or d.get("default_system_prompt")
                or d.get("base_role_definition"),
                "",
            ).strip(),
            include_environment=_as_bool(d.get("include_environment"), True),
            file_tree_max_depth=int(d.get("file_tree_max_depth", 2) or 2),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "global_instructions": (self.global_instructions or "").strip(),
            "include_environment": bool(self.include_environment),
            "file_tree_max_depth": self.file_tree_max_depth,
        }


@dataclass(frozen=True)
class RetryConfig:
    """Global LLM retry policy configuration."""
    max_retries: int = 3
    base_delay: float = 1.0
    backoff_factor: float = 2.0

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "RetryConfig":
        d = _as_dict(dict(data) if data is not None else {})
        return RetryConfig(
            max_retries=int(_as_float(d.get("max_retries"), 3)),
            base_delay=float(_as_float(d.get("base_delay"), 1.0)),
            backoff_factor=float(_as_float(d.get("backoff_factor"), 2.0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "backoff_factor": self.backoff_factor,
        }


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Global defaults for agent loop execution."""

    max_turns: int = 20

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "AgentRuntimeConfig":
        d = _as_dict(dict(data) if data is not None else {})
        raw = d.get("max_turns") if "max_turns" in d else d.get("maxTurns")
        defaults = AgentRuntimeConfig()
        return AgentRuntimeConfig(
            max_turns=_clamp_int(_as_int(raw, defaults.max_turns), 1, 1000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"max_turns": int(self.max_turns)}


@dataclass(frozen=True)
class ShellConfig:
    backend: str = "cmd"
    cmd_executable: str = ""
    powershell_executable: str = "powershell.exe"
    wsl_executable: str = "wsl.exe"
    wsl_distro: str = ""
    output_encoding: str = "auto"
    inherit_env: bool = True
    bang_command_behavior: str = "shell"
    # Single wait knob for shell tools: shell__run waits at most this long
    # before returning a background process_id (the process keeps running);
    # shell__read wait_seconds is capped at the same value.
    wait_seconds: int = 120

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ShellConfig":
        d = _as_dict(dict(data) if data is not None else {})
        backend = _as_str(d.get("backend"), "cmd").strip().lower() or "cmd"
        if backend not in {"cmd", "powershell", "wsl"}:
            backend = "cmd"

        encoding = _as_str(d.get("output_encoding"), "auto").strip().lower() or "auto"
        if encoding not in {"auto", "utf-8", "system", "gb18030"}:
            encoding = "auto"

        bang_behavior = _as_str(d.get("bang_command_behavior"), "shell").strip().lower() or "shell"
        if bang_behavior not in {"shell", "agent"}:
            bang_behavior = "shell"

        return ShellConfig(
            backend=backend,
            cmd_executable=_as_str(d.get("cmd_executable"), "").strip(),
            powershell_executable=_as_str(d.get("powershell_executable"), "powershell.exe").strip() or "powershell.exe",
            wsl_executable=_as_str(d.get("wsl_executable"), "wsl.exe").strip() or "wsl.exe",
            wsl_distro=_as_str(d.get("wsl_distro"), "").strip(),
            output_encoding=encoding,
            inherit_env=_as_bool(d.get("inherit_env"), True),
            bang_command_behavior=bang_behavior,
            wait_seconds=_clamp_int(
                _as_int(
                    d.get("wait_seconds", d.get("foreground_timeout_seconds", d.get("read_wait_max_seconds"))),
                    120,
                ),
                5,
                600,
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "cmd_executable": self.cmd_executable,
            "powershell_executable": self.powershell_executable,
            "wsl_executable": self.wsl_executable,
            "wsl_distro": self.wsl_distro,
            "output_encoding": self.output_encoding,
            "inherit_env": bool(self.inherit_env),
            "bang_command_behavior": self.bang_command_behavior if self.bang_command_behavior in {"shell", "agent"} else "shell",
            "wait_seconds": _clamp_int(int(self.wait_seconds or 120), 5, 600),
        }


@dataclass(frozen=True)
class AppConfig:
    # UI
    theme: str = "light"
    accent: str = DEFAULT_ACCENT
    show_sidebar: bool = True
    show_stats: bool = False
    show_thinking: bool = True
    log_stream: bool = False
    proxy_url: str = ""
    llm_timeout_seconds: float = 600.0
    splitter_sizes: List[int] = field(default_factory=list)
    chat_splitter_sizes: List[int] = field(default_factory=list)
    main_window_size: List[int] = field(default_factory=list)
    close_to_tray: bool = True

    # Feature configs
    agent: AgentRuntimeConfig = field(default_factory=AgentRuntimeConfig)
    permissions: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)
    default_chat_model: str = ""
    default_auxiliary_model: str = ""
    channels: List[ChannelConfig] = field(default_factory=list)
    shell: ShellConfig = field(default_factory=ShellConfig)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "AppConfig":
        d = _as_dict(dict(data) if data is not None else {})

        def _sizes(v: Any) -> List[int]:
            xs = _as_list(v)
            out: List[int] = []
            for x in xs:
                try:
                    out.append(int(x))
                except Exception:
                    continue
            return out

        theme = _as_str(d.get("theme"), "light").strip().lower() or "light"
        if theme not in {"light", "dark"}:
            theme = "light"
        accent = _as_str(
            d.get("accent") or d.get("accent_color"),
            DEFAULT_ACCENT,
        ).strip().lower() or DEFAULT_ACCENT
        if accent not in SUPPORTED_ACCENTS:
            accent = DEFAULT_ACCENT

        return AppConfig(
            theme=theme,
            accent=accent,
            show_sidebar=_as_bool(d.get("show_sidebar"), True),
            show_stats=_as_bool(d.get("show_stats"), False),
            show_thinking=_as_bool(d.get("show_thinking"), True),
            log_stream=_as_bool(d.get("log_stream"), False),
            proxy_url=_as_str(d.get("proxy_url"), "").strip(),
            llm_timeout_seconds=max(30.0, min(3600.0, _as_float(d.get("llm_timeout_seconds"), 600.0))),
            splitter_sizes=_sizes(d.get("splitter_sizes")),
            chat_splitter_sizes=_sizes(d.get("chat_splitter_sizes")),
            main_window_size=_sizes(d.get("main_window_size"))[:2],
            close_to_tray=_as_bool(d.get("close_to_tray"), True),
            agent=AgentRuntimeConfig.from_dict(_as_dict(d.get("agent"))),
            permissions=ToolPermissionConfig.from_settings_dict(d),
            retry=RetryConfig.from_dict(_as_dict(d.get("retry"))),
            context=ContextConfig.from_dict(_as_dict(d.get("context"))),
            prompts=PromptsConfig.from_dict(_as_dict(d.get("prompts"))),
            capabilities=CapabilitiesConfig.from_dict(_as_dict(d.get("capabilities")))
            if isinstance(d.get("capabilities"), dict)
            else CapabilitiesConfig(),
            default_chat_model=_as_str(d.get("default_chat_model"), "").strip(),
            default_auxiliary_model=_as_str(d.get("default_auxiliary_model"), "").strip(),
            channels=[
                ChannelConfig.from_dict(item)
                for item in _as_list(d.get("channels"))
                if isinstance(item, dict)
            ],
            shell=ShellConfig.from_dict(
                _as_dict(d.get("shell"))
                or {"backend": d.get("shell_backend")}
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "theme": self.theme,
            "accent": self.accent,
            "show_sidebar": bool(self.show_sidebar),
            "show_stats": bool(self.show_stats),
            "show_thinking": bool(self.show_thinking),
            "log_stream": bool(self.log_stream),
            "proxy_url": self.proxy_url or "",
            "llm_timeout_seconds": float(self.llm_timeout_seconds),
            "splitter_sizes": [int(x) for x in (self.splitter_sizes or [])],
            "chat_splitter_sizes": [int(x) for x in (self.chat_splitter_sizes or [])],
            "main_window_size": [int(x) for x in (self.main_window_size or [])][:2],
            "close_to_tray": bool(self.close_to_tray),
            "agent": self.agent.to_dict(),
            "retry": self.retry.to_dict(),
            "permissions": self.permissions.to_dict(),
            "context": self.context.to_dict(),
            "prompts": self.prompts.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "default_chat_model": (self.default_chat_model or "").strip(),
            "default_auxiliary_model": (self.default_auxiliary_model or "").strip(),
            "channels": [channel.to_dict() for channel in (self.channels or [])],
            "shell": self.shell.to_dict(),
        }
        return data


@dataclass(frozen=True)
class ProjectConfig:
    work_dir: str
    modes: List[Dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def from_modes_json(work_dir: str, data: Any) -> "ProjectConfig":
        modes: Any
        if isinstance(data, dict):
            modes = data.get("modes")
        else:
            modes = data

        mode_list: List[Dict[str, Any]] = []
        for item in _as_list(modes):
            if isinstance(item, dict):
                mode_list.append(dict(item))

        return ProjectConfig(work_dir=str(work_dir or ""), modes=mode_list)

    def to_modes_json(self) -> Dict[str, Any]:
        return {"modes": [dict(m) for m in (self.modes or [])]}
