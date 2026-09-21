"""Explicit extension discovery and pinned installation, shared by all clients.

MCP configuration still belongs to SettingsUpdateService; preparing a driver
returns a verified draft and never silently saves or enables a server.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx

from pycat.core.app.services.skill import (
    MAX_IMPORT_FILE_BYTES, MAX_IMPORT_MEMBERS, MAX_IMPORT_TOTAL_BYTES, SkillService,
)
from pycat.core.app.services.extension_market import mcp_rows
from pycat.core.persistence import atomic_write_text
from pycat.core.skills.manage import SKILL_NAME_RE, is_link_or_reparse
from pycat.models.contracts.mcp import McpServerConfig


CATALOG = (
    {"id": "agent-browser", "kind": "mcp", "title": "浏览器 · agent-browser", "management": "managed",
     "description": "原生驱动，无需 Node.js。复用本机 Chrome / Edge / Chromium，按对话隔离浏览器。",
     "website": "https://github.com/vercel-labs/agent-browser", "source": "vercel-labs/agent-browser"},
    {"id": "playwright", "kind": "mcp", "title": "浏览器 · Playwright MCP", "management": "external",
     "description": "可选浏览器后端，需要 Node.js 18+。通过 MCP 页配置和探测，程序更新由 npm 管理。",
     "website": "https://github.com/microsoft/playwright-mcp", "source": "microsoft/playwright-mcp"},
    {"id": "cua-driver", "kind": "mcp", "title": "桌面 · Cua Driver", "management": "external",
     "description": "可选跨平台桌面后端。Windows / Linux 使用驱动，macOS 需官方签名应用和系统授权；本期不自动安装。",
     "website": "https://cua.ai/docs/concepts/choose-a-cua-driver-integration", "source": "trycua/cua"},
    {"id": "mcp-registry", "kind": "mcp", "title": "MCP 官方目录", "management": "directory",
     "description": "查找更多 MCP Server，再在 MCP 页导入配置。目录信息不代表 PyCat 已验证该程序。",
     "website": "https://registry.modelcontextprotocol.io", "source": "Model Context Protocol"},
    {"id": "skills-directory", "kind": "skill", "title": "Skills 目录", "management": "directory",
     "description": "查找技能的原始 GitHub 仓库和目录，通过本页固定提交安装并检查更新。",
     "website": "https://skills.sh", "source": "skills.sh"},
)


class ExtensionService:
    def __init__(self, *, data_dir: Path, skills: SkillService, mcp_probe):
        self.root = Path(data_dir).resolve() / "extensions"
        self.skills = skills
        self.mcp_probe = mcp_probe
        self._install_lock = threading.Lock()

    def catalog(self, *, work_dir: str = "", servers=()) -> list[dict]:
        rows = [dict(item, current_version="", status="未安装", installed=False) for item in CATALOG]
        browser = next((s for s in servers if s.integration == "agent-browser"), None)
        rows[0].update(supported=bool(self.browser_asset()), browser_path=self.find_browser())
        if browser:
            receipt = self._browser_receipt(browser)
            rows[0].update(current_version=receipt.get("version", ""), installed=True,
                           status="已配置" if receipt else "自定义配置", server=browser.name)
        for row in rows[1:]:
            row["status"] = "外部管理" if row["management"] == "external" else "在线目录"
        for server in servers:
            if server is browser:
                continue
            rows.append({"id": "configured:" + server.name, "kind": "mcp", "title": server.name,
                "management": "external", "installed": True, "current_version": "", "website": "",
                "source": server.endpoint_summary(), "server": server.name,
                "description": "已配置的 MCP。请通过原安装方式更新程序；远程服务由服务端更新。工具列表可在 MCP 页重新探测。",
                "status": "已启用" if server.enabled else "已停用"})
        for skill in self.skills.list_for_workdir(work_dir, include_disabled=True):
            receipt = self.skills.installation_info(skill) if not skill.read_only else {}
            management = "bundled" if skill.source_scope == "bundled" else "managed" if receipt else "external"
            rows.append({"id": skill.name, "kind": "skill", "title": skill.name,
                         "description": skill.description, "management": management,
                         "current_version": receipt.get("version") or skill.metadata.get("version", ""),
                         "source": receipt.get("repository") or {"bundled": "PyCat 内置", "global": "用户目录", "project": "项目目录", "external": "外部目录"}.get(skill.source_scope, skill.source_scope),
                         "scope": skill.source_scope, "installed": True,
                         "website": "https://github.com/" + receipt["repository"] if receipt else "",
                         "status": "已启用" if skill.enabled else "已停用"})
        return rows

    @staticmethod
    def browser_asset() -> str:
        system = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
        machine = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
        if not system or not machine or (system == "win32" and machine != "x64"):
            return ""
        if system == "linux" and platform.libc_ver()[0] != "glibc":
            system = "linux-musl"
        return f"agent-browser-{system}-{machine}" + (".exe" if system == "win32" else "")

    async def search_market(self, kind: str, query: str, *, cursor: str = "", work_dir: str = "", servers=(), cancelled=None) -> dict:
        query = query.strip()
        if kind not in {"mcp", "skill"} or len(query) > 200 or len(cursor) > 1024:
            raise ValueError("无效的市场类型或搜索词；搜索词最多 200 字。")
        if kind == "mcp":
            params = {"search": query, "version": "latest", "limit": 20}
            if cursor:
                params["cursor"] = cursor
            data = await self._fetch_json("https://registry.modelcontextprotocol.io/v0.1/servers?" + urlencode(params), cancelled=cancelled)
            rows = mcp_rows(data)
            names = {server.name for server in servers}
            for row in rows:
                if any(option["configuration"]["name"] in names for option in row["options"]):
                    row.update(installed=True, status="已配置")
            return {"items": rows, "next_cursor": str(data.get("metadata", {}).get("nextCursor") or "")[:1024]}
        if len(query) < 2:
            raise ValueError("请输入至少两个字符来搜索 Skills 市场。")
        data = await self._fetch_json("https://skills.sh/api/search?" + urlencode({"q": query, "limit": 20}), cancelled=cancelled)
        rows = []
        for entry in data.get("skills", [])[:20]:
            repo = str(entry.get("source", ""))
            name = str(entry.get("skillId") or str(entry.get("id", "")).rsplit("/", 1)[-1])
            try:
                self._skill_source(repo, name)
            except ValueError:
                continue
            rows.append({"id": "market:" + repo + "/" + name, "kind": "skill", "title": name,
                "description": "来自 skills.sh 的公开目录。准备安装时核对 GitHub 目录并固定提交。",
                "source": repo, "repository": repo, "skill_name": name, "website": "https://github.com/" + repo,
                "management": "market", "status": "市场条目", "current_version": "", "installed": False})
        installed = {row["id"]: row for row in self.catalog(work_dir=work_dir) if row["kind"] == "skill"}
        for row in rows:
            current = installed.get(row["skill_name"])
            if current and current["management"] == "managed" and current["source"] == row["repository"]:
                row.update(installed=True, current_version=current["current_version"], scope=current["scope"], status=current["status"])
        return {"items": rows, "next_cursor": ""}

    async def preview_market_skill(self, repository: str, name: str, *, cancelled=None) -> dict:
        self._skill_source(repository, name)
        plan = await self.preview_skill(repository, name, cancelled=cancelled)
        tree = await self._fetch_json(f"https://api.github.com/repos/{repository}/git/trees/{plan['version']}?recursive=1", cancelled=cancelled)
        paths = [item["path"].rsplit("/", 1)[0] for item in tree.get("tree", [])
                 if item.get("type") == "blob" and str(item.get("path", "")).endswith("/SKILL.md")
                 and PurePosixPath(item["path"]).parent.name == name]
        if tree.get("truncated") or len(paths) != 1:
            raise ValueError("无法唯一确定技能目录，请使用“从 GitHub 安装”填写确切目录。")
        self._skill_source(repository, paths[0])
        return dict(plan, path=paths[0])

    @staticmethod
    def find_browser() -> str:
        for command in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            if found := shutil.which(command):
                return found
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                      Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(key):
                candidates += [Path(os.environ[key]) / relative for relative in (
                    "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe")]
        return str(next((path for path in candidates if path.is_file()), ""))

    async def check_browser(self, *, cancelled=None) -> dict:
        asset = self.browser_asset()
        if not asset:
            raise ValueError("当前平台暂无可用的原生浏览器驱动，请使用外部 MCP。")
        release = await self._fetch_json("https://api.github.com/repos/vercel-labs/agent-browser/releases/latest", cancelled=cancelled)
        item = next((item for item in release.get("assets", []) if item.get("name") == asset), None)
        if not item or not str(item.get("digest", "")).startswith("sha256:"):
            raise ValueError("官方发布缺少当前平台的 SHA-256 校验信息。")
        return {"id": "agent-browser", "version": release["tag_name"], "asset": asset,
                "sha256": item["digest"][7:], "size": item["size"]}

    @contextmanager
    def _installation(self):
        if not self._install_lock.acquire(blocking=False):
            raise ValueError("另一项扩展安装正在进行，请稍后重试。")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            yield
        finally:
            self._install_lock.release()

    async def prepare_browser(self, plan: dict, *, existing: McpServerConfig | None = None,
                              browser_path: str = "", cancelled=None) -> McpServerConfig:
        self._cancel(cancelled)
        version, digest = str(plan.get("version", "")), str(plan.get("sha256", ""))
        asset = self.browser_asset()
        if (plan.get("id") != "agent-browser" or not asset or plan.get("asset") != asset
                or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?", version)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("无效的浏览器版本或校验信息，请重新检查更新。")
        if existing is not None and existing.integration != "agent-browser":
            raise ValueError("不能覆盖其他来源的 MCP 配置。")
        with self._installation(), tempfile.TemporaryDirectory(prefix=".browser-", dir=self.root) as temporary:
            executable_name = "agent-browser.exe" if asset.endswith(".exe") else "agent-browser"
            target = self.root / "agent-browser" / (version + "-" + digest[:12])
            staged = Path(temporary) / executable_name
            target_executable = target / executable_name
            if is_link_or_reparse(target) or is_link_or_reparse(target_executable):
                raise ValueError("安装目录不能是符号链接或 junction。")
            if not target_executable.is_file():
                await self._download(f"https://github.com/vercel-labs/agent-browser/releases/download/{version}/{asset}",
                                     staged, limit=64 * 1024 * 1024, cancelled=cancelled)
                if hashlib.sha256(staged.read_bytes()).hexdigest() != digest:
                    raise ValueError("SHA-256 校验失败；原有版本保持不变。")
                staged.chmod(0o755)
                await self._verify_executable(staged, version, cancelled=cancelled)
                atomic_write_text(Path(temporary) / "receipt.json", json.dumps(plan, ensure_ascii=False))
                self._cancel(cancelled)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
            elif hashlib.sha256(target_executable.read_bytes()).hexdigest() != digest:
                raise ValueError("已安装文件的 SHA-256 不匹配，请移走损坏的安装目录后重试。")
            config = McpServerConfig.from_dict(existing.to_dict()) if existing else McpServerConfig(
                name="browser", command=str(target_executable), args=["mcp"], integration="agent-browser")
            config.command = str(target_executable)
            if browser_path:
                if not Path(browser_path).is_file():
                    raise ValueError("浏览器程序不存在。")
                config.env["AGENT_BROWSER_EXECUTABLE_PATH"] = browser_path
            elif "AGENT_BROWSER_EXECUTABLE_PATH" not in config.env and (detected := self.find_browser()):
                config.env["AGENT_BROWSER_EXECUTABLE_PATH"] = detected
            self._cancel(cancelled)
            result = await self.mcp_probe(config)
            if not result.get("ok"):
                raise ValueError("浏览器 MCP 连接验证失败：" + str(result.get("error") or result.get("message") or result))
            self._cancel(cancelled)
            config.cached_tools = [t if isinstance(t, str) else t["name"] for t in result.get("tools", [])]
            required = {"agent_browser_open", "agent_browser_snapshot", "agent_browser_close"}
            if missing := required.difference(config.cached_tools):
                raise ValueError("浏览器 MCP 缺少必要工具：" + ", ".join(sorted(missing)))
            return config

    def _browser_receipt(self, server: McpServerConfig) -> dict:
        path = Path(server.command).parent / "receipt.json"
        if not path.resolve().is_relative_to(self.root) or not path.is_file() or path.stat().st_size > 8192:
            return {}
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _skill_source(repository: str, path: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository) or repository.endswith("/.."):
            raise ValueError("来源请填写 GitHub owner/repo。")
        if not path or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("技能目录必须位于仓库内。")
        for part in path.split("/"):
            SkillService._check_import_component(part)
        if not SKILL_NAME_RE.fullmatch(PurePosixPath(path).name):
            raise ValueError("技能目录名必须为 3–64 位小写字母、数字或连字符。")

    async def preview_skill(self, repository: str, path: str, ref: str = "HEAD", *, cancelled=None) -> dict:
        repository, path, ref = repository.strip(), path.strip().strip("/"), ref.strip() or "HEAD"
        self._skill_source(repository, path)
        result = await self._fetch_json(f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}", cancelled=cancelled)
        version = str(result.get("sha", ""))
        if not re.fullmatch(r"[a-f0-9]{40}", version):
            raise ValueError("GitHub 未返回有效提交。")
        return {"id": "github-skill", "repository": repository, "path": path, "ref": ref,
                "version": version, "name": PurePosixPath(path).name}

    async def check_skill(self, name: str, *, work_dir: str = "", cancelled=None) -> dict:
        skill = self.skills.get(name, work_dir=work_dir, include_disabled=True)
        receipt = self.skills.installation_info(skill) if skill and not skill.read_only else {}
        if not receipt:
            raise ValueError("此技能未记录托管来源。内置技能随 PyCat 更新；本地和外部技能请从原来源重新导入。")
        result = await self.preview_skill(receipt["repository"], receipt["path"], receipt["ref"], cancelled=cancelled)
        result["scope"] = skill.source_scope
        result["current_version"] = receipt["version"]
        return result

    async def install_skill(self, plan: dict, *, scope: str = "global", work_dir: str = "",
                            overwrite: bool = False, cancelled=None) -> str:
        self._cancel(cancelled)
        self._skill_source(str(plan.get("repository", "")), str(plan.get("path", "")))
        if (plan.get("id") != "github-skill" or not re.fullmatch(r"[a-f0-9]{40}", str(plan.get("version", "")))
                or plan.get("name") != PurePosixPath(plan["path"]).name):
            raise ValueError("请先检查并选择一个明确的技能提交。")
        root = self.skills._scope_root(scope, work_dir)
        expected = None
        if (root / plan["name"]).exists():
            skill = self.skills.get(plan["name"], work_dir=work_dir, include_disabled=True)
            if skill is None or Path(skill.source).parent.resolve() != (root / plan["name"]).resolve():
                raise ValueError("同名技能被更高优先级的来源覆盖，请先处理同名技能。")
            receipt = self.skills.installation_info(skill)
            if not overwrite or any(receipt.get(key) != plan.get(key) for key in ("repository", "path")):
                raise ValueError("目标技能已存在，不能覆盖未托管或不同来源的技能。")
            expected = receipt.get("content_digest")
            if not expected or self.skills.content_digest(root / plan["name"]) != expected:
                raise ValueError("技能存在本地修改，请先保留或移走修改后再更新。")
        with self._installation(), tempfile.TemporaryDirectory(prefix=".skill-", dir=self.root) as temporary:
            staged = await self._stage_github_skill(plan, Path(temporary), cancelled=cancelled)
            self._cancel(cancelled)
            receipt = {key: plan[key] for key in ("repository", "path", "ref", "version", "name")}
            return str(self.skills.import_managed(staged, scope=scope, work_dir=work_dir, overwrite=overwrite,
                                                  install_receipt=receipt, expected_digest=expected))

    async def _stage_github_skill(self, plan: dict, destination: Path, *, cancelled=None) -> Path:
        repo, version, prefix = plan["repository"], plan["version"], plan["path"] + "/"
        tree = await self._fetch_json(f"https://api.github.com/repos/{repo}/git/trees/{version}?recursive=1", cancelled=cancelled)
        if tree.get("truncated"):
            raise ValueError("仓库目录过大，无法完整验证；请下载技能目录后本地导入。")
        entries = [item for item in tree.get("tree", []) if str(item.get("path", "")).startswith(prefix) and item.get("type") != "tree"]
        if not entries or len(entries) > MAX_IMPORT_MEMBERS:
            raise ValueError("技能目录不存在、为空或文件过多。")
        if sum(int(item.get("size", 0)) for item in entries) > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("技能超过总大小限制。")
        staged = destination / plan["name"]
        staged.mkdir()
        seen = set()
        actual_total = 0
        for item in entries:
            relative = item["path"][len(prefix):]
            parts = SkillService._safe_zip_parts(relative)
            key = "/".join(parts).casefold()
            if key in seen or item.get("mode") not in {"100644", "100755"} or item.get("type") != "blob":
                raise ValueError("技能包含重复路径、链接或不支持的文件类型。")
            seen.add(key)
            if relative in {".pycat-install.json", ".disabled"}:
                continue
            if int(item.get("size", 0)) > MAX_IMPORT_FILE_BYTES:
                raise ValueError("技能文件超过大小限制。")
            target = staged.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            await self._download(f"https://raw.githubusercontent.com/{repo}/{version}/{quote(item['path'], safe='/')}",
                                 target, limit=MAX_IMPORT_FILE_BYTES, cancelled=cancelled)
            # Git's blob object hash binds the downloaded contents to the reviewed commit.
            content = target.read_bytes()
            actual_total += len(content)
            if len(content) != int(item.get("size", -1)) or actual_total > MAX_IMPORT_TOTAL_BYTES:
                raise ValueError("技能文件大小与提交信息不符或超过总大小限制。")
            if hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() != item["sha"]:
                raise ValueError("技能文件与固定提交不匹配。")
        return staged

    @staticmethod
    def _cancel(cancelled):
        if cancelled and cancelled():
            raise InterruptedError("操作已取消；原有配置和技能保持不变。")

    async def _chunks(self, url: str, *, limit: int, cancelled=None):
        started, size = time.monotonic(), 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15), headers={"User-Agent": "PyCat-Extensions"}) as client:
            for _ in range(6):
                self._cancel(cancelled)
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.username or parsed.password or not (
                    parsed.hostname in {"github.com", "api.github.com", "raw.githubusercontent.com", "skills.sh", "registry.modelcontextprotocol.io"}
                    or (parsed.hostname or "").endswith(".githubusercontent.com")
                ):
                    raise ValueError("仅允许已配置的 GitHub、MCP Registry 与 Skills 市场 HTTPS 来源。")
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        self._cancel(cancelled)
                        size += len(chunk)
                        if size > limit or time.monotonic() - started > 300:
                            raise ValueError("扩展下载超过大小或时间限制。")
                        yield chunk
                    return
            raise ValueError("扩展下载重定向次数过多。")

    async def _fetch_json(self, url: str, *, cancelled=None) -> dict:
        content = bytearray()
        async for chunk in self._chunks(url, limit=8 * 1024 * 1024, cancelled=cancelled):
            content.extend(chunk)
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("远程元数据格式无效。")
        return value

    async def _download(self, url: str, path: Path, *, limit: int, cancelled=None):
        with path.open("wb") as stream:
            async for chunk in self._chunks(url, limit=limit, cancelled=cancelled):
                stream.write(chunk)

    async def _verify_executable(self, path: Path, version: str, *, cancelled=None):
        process = await asyncio.create_subprocess_exec(str(path), "--version", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        task = asyncio.create_task(process.communicate())
        try:
            async with asyncio.timeout(20):
                while not task.done():
                    self._cancel(cancelled)
                    await asyncio.wait({task}, timeout=0.1)
                stdout, stderr = await task
            if process.returncode or version.removeprefix("v") not in stdout.decode(errors="replace"):
                raise ValueError("浏览器驱动版本验证失败：" + stderr.decode(errors="replace")[:300])
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.gather(task, return_exceptions=True)
