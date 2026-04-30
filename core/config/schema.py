from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping


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
    per_message_lookback: int = 20
    tool_min_chars: int = 200
    assistant_min_chars: int = 800
    max_active_messages: int = 20
    token_threshold_ratio: float = 0.70
    keep_last_n: int = 3

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CompressionPolicyConfig":
        d = _as_dict(dict(data) if data is not None else {})
        return CompressionPolicyConfig(
            per_message_lookback=_clamp_int(_as_int(d.get("per_message_lookback"), 20), 1, 200),
            tool_min_chars=_clamp_int(_as_int(d.get("tool_min_chars"), 200), 0, 20000),
            assistant_min_chars=_clamp_int(_as_int(d.get("assistant_min_chars"), 800), 0, 200000),
            max_active_messages=_clamp_int(_as_int(d.get("max_active_messages"), 20), 5, 500),
            token_threshold_ratio=max(0.10, min(0.95, _as_float(d.get("token_threshold_ratio"), 0.70))),
            keep_last_n=_clamp_int(_as_int(d.get("keep_last_n"), 3), 1, 200),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "per_message_lookback": int(self.per_message_lookback),
            "tool_min_chars": int(self.tool_min_chars),
            "assistant_min_chars": int(self.assistant_min_chars),
            "max_active_messages": int(self.max_active_messages),
            "token_threshold_ratio": float(self.token_threshold_ratio),
            "keep_last_n": int(self.keep_last_n),
        }


@dataclass(frozen=True)
class ContextConfig:
    default_max_context_messages: int = 0
    agent_auto_compress_enabled: bool = True
    summary_model: str = ""
    summary_system_prompt: str = ""
    summary_include_tool_details: bool = False
    compression_policy: CompressionPolicyConfig = field(default_factory=CompressionPolicyConfig)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ContextConfig":
        d = _as_dict(dict(data) if data is not None else {})
        default_max = _as_int(d.get("default_max_context_messages"), 0)
        default_max = default_max if default_max > 0 else 0
        return ContextConfig(
            default_max_context_messages=default_max,
            agent_auto_compress_enabled=_as_bool(d.get("agent_auto_compress_enabled"), True),
            summary_model=_as_str(d.get("summary_model"), "").strip(),
            summary_system_prompt=_as_str(d.get("summary_system_prompt"), "").strip(),
            summary_include_tool_details=_as_bool(d.get("summary_include_tool_details"), False),
            compression_policy=CompressionPolicyConfig.from_dict(_as_dict(d.get("compression_policy"))),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_max_context_messages": int(self.default_max_context_messages),
            "agent_auto_compress_enabled": bool(self.agent_auto_compress_enabled),
            "summary_model": (self.summary_model or "").strip(),
            "summary_system_prompt": (self.summary_system_prompt or "").strip(),
            "summary_include_tool_details": bool(self.summary_include_tool_details),
            "compression_policy": self.compression_policy.to_dict(),
        }


@dataclass(frozen=True)
class PromptsConfig:
    # Used when mode doesn't specify roleDefinition.
    default_system_prompt: str = ""
    # Backward-compatible name used previously.
    base_role_definition: str = ""
    agent_tool_guidelines: str = ""
    include_environment: bool = True
    include_state: bool = True
    file_tree_max_depth: int = 2

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "PromptsConfig":
        d = _as_dict(dict(data) if data is not None else {})
        return PromptsConfig(
            default_system_prompt=_as_str(d.get("default_system_prompt"), "").strip(),
            base_role_definition=_as_str(d.get("base_role_definition"), "").strip(),
            agent_tool_guidelines=_as_str(d.get("agent_tool_guidelines"), "").strip(),
            include_environment=_as_bool(d.get("include_environment"), True),
            include_state=_as_bool(d.get("include_state"), True),
            file_tree_max_depth=int(d.get("file_tree_max_depth", 2) or 2),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_system_prompt": (self.default_system_prompt or "").strip(),
            "base_role_definition": (self.base_role_definition or "").strip(),
            "agent_tool_guidelines": (self.agent_tool_guidelines or "").strip(),
            "include_environment": bool(self.include_environment),
            "include_state": bool(self.include_state),
            "file_tree_max_depth": self.file_tree_max_depth,
        }


@dataclass(frozen=True)
class PromptOptimizerConfig:
    selected_template: str = "default"
    templates: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "PromptOptimizerConfig":
        d = _as_dict(dict(data) if data is not None else {})
        templates = d.get("templates")
        templates = templates if isinstance(templates, dict) else {}
        clean: Dict[str, str] = {}
        for k, v in templates.items():
            if not k:
                continue
            key = _as_str(k).strip()
            if not key:
                continue
            clean[key] = _as_str(v, "").strip()
        sel = _as_str(d.get("selected_template"), "default").strip() or "default"
        return PromptOptimizerConfig(selected_template=sel, templates=clean)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_template": self.selected_template or "default",
            "templates": dict(self.templates or {}),
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
class PermissionsConfig:
    auto_approve_read: bool = True
    auto_approve_edit: bool = False
    auto_approve_command: bool = False

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "PermissionsConfig":
        d = _as_dict(dict(data) if data is not None else {})
        # Keep compatibility with legacy flat keys on root.
        return PermissionsConfig(
            auto_approve_read=_as_bool(d.get("auto_approve_read"), True),
            auto_approve_edit=_as_bool(d.get("auto_approve_edit"), False),
            auto_approve_command=_as_bool(d.get("auto_approve_command"), False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "auto_approve_read": bool(self.auto_approve_read),
            "auto_approve_edit": bool(self.auto_approve_edit),
            "auto_approve_command": bool(self.auto_approve_command),
        }


@dataclass(frozen=True)
class ChannelConfig:
    id: str = ""
    name: str = ""
    type: str = "webhook"
    enabled: bool = False
    allow_tools: bool = True
    source: str = ""
    description: str = ""
    agent_id: str = ""
    session_id: str = ""
    permission_mode: str = ""
    status: str = "draft"
    created_at: int = 0
    updated_at: int = 0
    webhook_url: str = ""
    token: str = ""
    secret: str = ""
    config: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ChannelConfig":
        d = _as_dict(dict(data) if data is not None else {})
        channel_type = _as_str(d.get("type"), "webhook").strip().lower() or "webhook"
        name = _as_str(d.get("name"), "").strip()
        source = _as_str(d.get("source"), "").strip()
        config = _as_dict(d.get("config"))
        if not config and channel_type == "webhook" and _as_str(d.get("webhook_url"), "").strip():
            config = {"webhook_url": _as_str(d.get("webhook_url"), "").strip()}
        elif _as_str(d.get("webhook_url"), "").strip() and not _as_str(config.get("webhook_url"), "").strip():
            config = {**config, "webhook_url": _as_str(d.get("webhook_url"), "").strip()}

        legacy_token = _as_str(d.get("token"), "").strip()
        legacy_secret = _as_str(d.get("secret"), "").strip()
        if legacy_token:
            if channel_type == "telegram" and not _as_str(config.get("bot_token"), "").strip():
                config = {**config, "bot_token": legacy_token}
            elif channel_type == "feishu" and not _as_str(config.get("verification_token"), "").strip():
                config = {**config, "verification_token": legacy_token}
            if not _as_str(config.get("token"), "").strip():
                config = {**config, "token": legacy_token}

        if legacy_secret:
            if channel_type in {"wechat", "feishu", "qqbot"} and not _as_str(config.get("app_secret"), "").strip():
                config = {**config, "app_secret": legacy_secret}
            elif channel_type == "webhook" and not _as_str(config.get("signing_secret"), "").strip():
                config = {**config, "signing_secret": legacy_secret}
            if not _as_str(config.get("secret"), "").strip():
                config = {**config, "secret": legacy_secret}

        if channel_type == "qqbot":
            openclaw_app_id = _as_str(config.get("appId"), "").strip()
            openclaw_client_secret = _as_str(config.get("clientSecret"), "").strip()
            if openclaw_app_id and not _as_str(config.get("app_id"), "").strip():
                config = {**config, "app_id": openclaw_app_id}
            if openclaw_client_secret and not _as_str(config.get("app_secret"), "").strip():
                config = {**config, "app_secret": openclaw_client_secret}

        if not source:
            source_name = name or channel_type
            source = f"plugin:{channel_type}:{source_name}"
        return ChannelConfig(
            id=_as_str(d.get("id"), "").strip(),
            name=name,
            type=channel_type,
            enabled=_as_bool(d.get("enabled"), False),
            allow_tools=_as_bool(d.get("allow_tools"), True),
            source=source,
            description=_as_str(d.get("description"), "").strip(),
            agent_id=_as_str(d.get("agent_id"), "").strip(),
            session_id=_as_str(d.get("session_id"), "").strip(),
            permission_mode=_as_str(d.get("permission_mode"), "").strip(),
            status=_as_str(d.get("status"), "draft").strip() or "draft",
            created_at=_as_int(d.get("created_at"), 0),
            updated_at=_as_int(d.get("updated_at"), 0),
            webhook_url=_as_str(d.get("webhook_url"), "").strip(),
            token=_as_str(d.get("token"), "").strip(),
            secret=_as_str(d.get("secret"), "").strip(),
            config=config,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "enabled": bool(self.enabled),
            "allow_tools": bool(self.allow_tools),
            "source": self.source,
            "description": self.description,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "permission_mode": self.permission_mode,
            "status": self.status,
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "webhook_url": self.webhook_url,
            "token": self.token,
            "secret": self.secret,
            "config": dict(self.config or {}),
        }


@dataclass(frozen=True)
class ShellConfig:
    backend: str = "cmd"
    cmd_executable: str = ""
    powershell_executable: str = "powershell.exe"
    wsl_executable: str = "wsl.exe"
    wsl_distro: str = ""
    output_encoding: str = "auto"
    inherit_env: bool = True

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ShellConfig":
        d = _as_dict(dict(data) if data is not None else {})
        backend = _as_str(d.get("backend"), "cmd").strip().lower() or "cmd"
        if backend not in {"cmd", "powershell", "wsl"}:
            backend = "cmd"

        encoding = _as_str(d.get("output_encoding"), "auto").strip().lower() or "auto"
        if encoding not in {"auto", "utf-8", "system", "gb18030"}:
            encoding = "auto"

        return ShellConfig(
            backend=backend,
            cmd_executable=_as_str(d.get("cmd_executable"), "").strip(),
            powershell_executable=_as_str(d.get("powershell_executable"), "powershell.exe").strip() or "powershell.exe",
            wsl_executable=_as_str(d.get("wsl_executable"), "wsl.exe").strip() or "wsl.exe",
            wsl_distro=_as_str(d.get("wsl_distro"), "").strip(),
            output_encoding=encoding,
            inherit_env=_as_bool(d.get("inherit_env"), True),
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
        }


@dataclass(frozen=True)
class AppConfig:
    # UI
    theme: str = "dark"
    show_stats: bool = True
    show_thinking: bool = True
    log_stream: bool = False
    proxy_url: str = ""
    llm_timeout_seconds: float = 600.0
    splitter_sizes: List[int] = field(default_factory=list)
    chat_splitter_sizes: List[int] = field(default_factory=list)

    # Feature configs
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    prompt_optimizer: PromptOptimizerConfig = field(default_factory=PromptOptimizerConfig)
    prompt_optimizer_model: str = ""
    default_chat_model: str = ""
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

        # Compatibility: permissions were previously stored flat on root.
        permissions_src = {
            "auto_approve_read": d.get("auto_approve_read"),
            "auto_approve_edit": d.get("auto_approve_edit"),
            "auto_approve_command": d.get("auto_approve_command"),
        }

        return AppConfig(
            theme=_as_str(d.get("theme"), "dark").strip() or "dark",
            show_stats=_as_bool(d.get("show_stats"), True),
            show_thinking=_as_bool(d.get("show_thinking"), True),
            log_stream=_as_bool(d.get("log_stream"), False),
            proxy_url=_as_str(d.get("proxy_url"), "").strip(),
            llm_timeout_seconds=max(30.0, min(3600.0, _as_float(d.get("llm_timeout_seconds"), 600.0))),
            splitter_sizes=_sizes(d.get("splitter_sizes")),
            chat_splitter_sizes=_sizes(d.get("chat_splitter_sizes")),
            permissions=PermissionsConfig.from_dict(permissions_src),
            retry=RetryConfig.from_dict(_as_dict(d.get("retry"))),
            context=ContextConfig.from_dict(_as_dict(d.get("context"))),
            prompts=PromptsConfig.from_dict(_as_dict(d.get("prompts"))),
            prompt_optimizer=PromptOptimizerConfig.from_dict(_as_dict(d.get("prompt_optimizer"))),
            prompt_optimizer_model=_as_str(d.get("prompt_optimizer_model"), "").strip(),
            default_chat_model=_as_str(d.get("default_chat_model"), "").strip(),
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
            "show_stats": bool(self.show_stats),
            "show_thinking": bool(self.show_thinking),
            "log_stream": bool(self.log_stream),
            "proxy_url": self.proxy_url or "",
            "llm_timeout_seconds": float(self.llm_timeout_seconds),
            "splitter_sizes": [int(x) for x in (self.splitter_sizes or [])],
            "chat_splitter_sizes": [int(x) for x in (self.chat_splitter_sizes or [])],
            "retry": self.retry.to_dict(),
            "permissions": self.permissions.to_dict(),
            "context": self.context.to_dict(),
            "prompts": self.prompts.to_dict(),
            "prompt_optimizer": self.prompt_optimizer.to_dict(),
            "prompt_optimizer_model": (self.prompt_optimizer_model or "").strip(),
            "default_chat_model": (self.default_chat_model or "").strip(),
            "channels": [channel.to_dict() for channel in (self.channels or [])],
            "shell": self.shell.to_dict(),
            "shell_backend": self.shell.backend,
        }
        # Keep legacy flat permission keys for current UI consumers.
        data.update(self.permissions.to_dict())
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
