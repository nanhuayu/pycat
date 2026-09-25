"""Host-owned agent-browser session boundaries; no parallel tool executor."""
import asyncio
import os
import subprocess
from copy import deepcopy
from dataclasses import replace
from importlib.resources import files

import psutil

from pycat.models.contracts.mcp import McpServerConfig

ALLOWED_TOOLS = {"agent_browser_" + name for name in (
    "tools_profiles", "open", "read", "snapshot", "back", "forward", "reload", "click", "fill", "type",
    "press", "check", "uncheck", "select", "scroll", "wait_ms", "wait_for_selector", "wait_for_text",
    "wait_for_load", "screenshot", "get_text", "get_url", "get_title", "tab_new", "tab_list", "tab_switch",
    "tab_close", "eval", "close", "set_headers", "set_credentials", "set_offline", "network_route",
    "network_unroute", "network_requests", "network_request", "network_har_start", "network_har_stop",
)}
RESERVED_ARGUMENTS = {"session", "namespace", "all", "extraArgs", "restore", "restoreSave",
                      "restoreCheckUrl", "restoreCheckText", "restoreCheckFn", "idleTimeout",
                      "headed", "webgpu", "webmcp", "allowedDomains", "caCert", "clearCaCert"}


def scoped_schema(schema: dict) -> dict | None:
    if schema["name"] not in ALLOWED_TOOLS:
        return None
    result = deepcopy(schema)
    parameters = result.get("parameters", {})
    for name in RESERVED_ARGUMENTS:
        parameters.get("properties", {}).pop(name, None)
    if "timeoutMs" in parameters.get("properties", {}):
        parameters["properties"]["timeoutMs"]["maximum"] = 60000
    if "required" in parameters:
        parameters["required"] = [name for name in parameters["required"] if name not in RESERVED_ARGUMENTS]
    return result


def scoped_config(config: McpServerConfig, namespace: str, session: str) -> McpServerConfig:
    allowed_env = {"AGENT_BROWSER_" + key for key in ("EXECUTABLE_PATH", "HEADED", "PROXY", "PROXY_BYPASS",
                                                    "IGNORE_HTTPS_ERRORS", "ALLOWED_DOMAINS", "CA_CERT")}
    env = {key: value for key, value in config.env.items() if not key.startswith("AGENT_BROWSER_") or key in allowed_env}
    # Explicit empty configuration prevents inherited user/project profiles.
    env["AGENT_BROWSER_CONFIG"] = str(files("pycat").joinpath("assets", "extensions", "browser.json"))
    env.update(AGENT_BROWSER_NAMESPACE=namespace, AGENT_BROWSER_SESSION=session)
    env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = "300000"
    return replace(config, env=env)


def scoped_arguments(name: str, arguments: dict, namespace: str, session: str) -> dict:
    if name not in ALLOWED_TOOLS:
        raise ValueError("此工具不属于 PyCat 托管浏览器的会话工具集。")
    if RESERVED_ARGUMENTS.intersection(arguments):
        raise ValueError("浏览器会话、启动参数和生命周期由 PyCat 管理；请在 MCP 配置中设置浏览器选项。")
    timeout = arguments.get("timeoutMs", 30000)
    if type(timeout) is not int or not 1 <= timeout <= 60000:
        raise ValueError("浏览器调用超时必须在 1–60000 毫秒之间。")
    return {**arguments, "namespace": namespace, "session": session, "restore": False, "idleTimeout": "5m",
            "timeoutMs": timeout}


async def _lifecycle_command(config: McpServerConfig, *arguments: str, timeout: float):
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_BROWSER_")}
    env.update(config.env)
    process = await asyncio.create_subprocess_exec(config.command, "--idle-timeout", "5m", *arguments,
        env=env, cwd=config.cwd or None, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        if process.returncode:
            raise RuntimeError("浏览器会话准备或清理失败，请检查本机浏览器程序与 MCP 环境变量。")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def prepare_browser_daemon(config: McpServerConfig):
    # Windows native MCP 0.38.1 joins stdout readers after its CLI exits. A
    # newly spawned daemon inherits those pipe handles, preventing completion.
    # Start/reuse the scoped daemon with null stdio through a read-only CLI
    # command first, including after idle expiry. This does not navigate pages.
    if os.name == "nt":
        await _lifecycle_command(config, "get", "url", timeout=20)


def reap_browser_processes(config: McpServerConfig):
    """Reap only processes with the exact executable AND our session identity.

    The daemon can outlive MCP on cancellation; a PID file alone is insufficient
    proof of ownership. psutil also protects kill() against recycled PIDs.
    """
    namespace, session = config.env.get("AGENT_BROWSER_NAMESPACE"), config.env.get("AGENT_BROWSER_SESSION")
    if not namespace or not session:
        return
    expected = os.path.normcase(os.path.realpath(config.command))
    owned = {}
    for process in psutil.process_iter(["exe"]):
        try:
            if not process.info.get("exe") or os.path.normcase(os.path.realpath(process.info["exe"])) != expected:
                continue
            env = process.environ()
            if env.get("AGENT_BROWSER_NAMESPACE") != namespace or env.get("AGENT_BROWSER_SESSION") != session:
                continue
            for child in process.children(recursive=True):
                owned[child.pid] = child
            owned[process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for process in owned.values():
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(owned.values()), timeout=1)
    if alive:
        raise RuntimeError("自有浏览器进程尚未退出。")
