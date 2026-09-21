"""Bounded public directory projections. Registry metadata never executes code."""
import re
from urllib.parse import urlparse

from pycat.models.contracts.mcp import McpServerConfig


def website(value) -> str:
    value = str(value or "")[:2048]
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc and not parsed.username else ""


def mcp_options(server: dict) -> list[dict]:
    """Prepare supported configurations; leave advanced templates to the editor."""
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", str(server.get("name", "mcp")))[:80]
    options = []
    for remote in server.get("remotes", [])[:10]:
        url = website(remote.get("url"))
        transport = {"streamable-http": "streamable_http", "sse": "sse"}.get(remote.get("type"))
        if not url or not transport or "{" in url or "}" in url:
            continue
        headers = {entry["name"]: "" for entry in remote.get("headers", [])[:32]
                   if re.fullmatch(r"[A-Za-z0-9_-]+", str(entry.get("name", "")))}
        config = McpServerConfig(name=name, transport=transport, url=url, headers=headers, enabled=False)
        options.append({"label": transport + " · " + url, "configuration": config.to_dict()})
    for package in server.get("packages", [])[:10]:
        kind, identifier, version = package.get("registryType"), str(package.get("identifier", "")), str(package.get("version", ""))
        if package.get("transport", {}).get("type") != "stdio" or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,99}", version):
            continue
        # No executable names, dynamic variables, or install scripts from metadata.
        if any(arg.get("value") != "-y" for arg in package.get("runtimeArguments", [])):
            continue
        arguments = []
        supported = True
        for argument in package.get("packageArguments", []):
            value = argument.get("value")
            if not isinstance(value, str) or "{" in value or len(value) > 1024 or argument.get("variables"):
                supported = False
                break
            if argument.get("type") == "named":
                flag = argument.get("name", "")
                if not re.fullmatch(r"--?[A-Za-z][A-Za-z0-9-]*", flag):
                    supported = False
                    break
                arguments.append(flag)
            elif argument.get("type") != "positional":
                supported = False
                break
            arguments.append(value)
        if not supported or len(arguments) > 64:
            continue
        if kind == "npm" and re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*", identifier):
            if package.get("registryBaseUrl", "https://registry.npmjs.org").rstrip("/") != "https://registry.npmjs.org":
                continue
            command, args, label = "npx", ["-y", identifier + "@" + version, *arguments], "npm · 需要 Node.js"
        elif kind == "pypi" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
            if package.get("registryBaseUrl", "https://pypi.org").rstrip("/") != "https://pypi.org":
                continue
            command, args, label = "uvx", [identifier + "==" + version, *arguments], "PyPI · 需要 uv"
        else:
            continue
        env = {entry["name"]: "" if entry.get("isSecret") else str(entry.get("default", ""))[:2048]
               for entry in package.get("environmentVariables", [])[:64]
               if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(entry.get("name", "")))}
        config = McpServerConfig(name=name, command=command, args=args, env=env, enabled=False)
        options.append({"label": label + " · " + identifier + "@" + version, "configuration": config.to_dict()})
    return options


def mcp_rows(data: dict) -> list[dict]:
    rows = []
    for entry in data.get("servers", [])[:20]:
        try:
            server = entry["server"]
            if entry.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {}).get("status", "active") != "active":
                continue
            name = str(server["name"])[:200]
            rows.append({"id": "registry:" + name, "kind": "mcp", "title": name,
                "description": str(server.get("description", ""))[:2000], "source": "MCP 官方 Registry",
                "website": website(server.get("repository", {}).get("url")), "management": "market",
                "current_version": "", "market_version": str(server.get("version", ""))[:100],
                "installed": False, "status": "市场条目", "options": mcp_options(server)})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return rows
