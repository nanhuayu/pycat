"""Stable GitHub Release lookup for the optional update notification."""
from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Mapping
from typing import Any, Literal

import httpx


STABLE_REPOSITORY = "nanhuayu/pycat"
STABLE_RELEASE_API = "https://api.github.com/repos/nanhuayu/pycat/releases/latest"
STABLE_RELEASE_PAGE = "https://github.com/nanhuayu/pycat/releases"
RELEASE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
MAX_RELEASE_RESPONSE_BYTES = 1_000_000

_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_release_version(value: str) -> tuple[int, int, int] | None:
    """Parse the stable ``MAJOR.MINOR.PATCH`` form used by Release tags."""

    raw = str(value or "").strip()
    if raw.lower().startswith("v"):
        raw = raw[1:]
    match = _VERSION_PATTERN.fullmatch(raw)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def update_check_due(settings: Mapping[str, Any] | None, *, now: float) -> bool:
    """Return whether the automatic check interval has elapsed."""

    values = settings or {}
    if values.get("update_check_enabled", True) is False:
        return False
    try:
        last_checked = float(values.get("update_last_checked_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        last_checked = 0.0
    return float(now) - last_checked >= RELEASE_CHECK_INTERVAL_SECONDS


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str


@dataclass(frozen=True)
class ReleaseInfo:
    tag_name: str
    version: str
    name: str
    html_url: str
    published_at: str
    body: str
    assets: tuple[ReleaseAsset, ...] = ()


@dataclass(frozen=True)
class ReleaseCheckResult:
    status: Literal["available", "up_to_date", "error"]
    current_version: str
    release: ReleaseInfo | None = None
    error: str = ""


class ReleaseChecker:
    """Fetch and compare the latest stable Release from the public repository."""

    def __init__(
        self,
        *,
        api_url: str = STABLE_RELEASE_API,
        timeout_seconds: float = 8.0,
        user_agent: str = "PyCat-Release-Checker",
    ) -> None:
        self.api_url = str(api_url or STABLE_RELEASE_API).strip() or STABLE_RELEASE_API
        self.timeout_seconds = max(1.0, float(timeout_seconds or 8.0))
        self.user_agent = str(user_agent or "PyCat-Release-Checker").strip()

    def check(self, *, current_version: str) -> ReleaseCheckResult:
        """Return a non-throwing result for UI/background-job consumers."""

        current = parse_release_version(current_version)
        normalized_current = str(current_version or "").strip().lstrip("v")
        if current is None:
            return ReleaseCheckResult(
                status="error",
                current_version=normalized_current,
                error="当前版本格式无效。",
            )

        try:
            payload = self._fetch_payload()
            release = self._parse_release(payload)
        except httpx.HTTPStatusError as exc:
            status_code = getattr(exc.response, "status_code", "unknown")
            return ReleaseCheckResult(
                status="error",
                current_version=normalized_current,
                error=f"GitHub Release 检查失败（HTTP {status_code}）。",
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            detail = str(exc or "网络请求失败").strip()
            return ReleaseCheckResult(
                status="error",
                current_version=normalized_current,
                error=f"暂时无法检查更新：{detail or '网络请求失败'}",
            )
        except (TypeError, ValueError) as exc:
            detail = str(exc or "Release 数据无效").strip()
            return ReleaseCheckResult(
                status="error",
                current_version=normalized_current,
                error=f"Release 数据无效：{detail or '未知格式'}",
            )

        status = "available" if parse_release_version(release.version) > current else "up_to_date"
        return ReleaseCheckResult(
            status=status,
            current_version=normalized_current,
            release=release,
        )

    def _fetch_payload(self) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
        }
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True) as client:
            response = client.get(self.api_url, headers=headers)
            response.raise_for_status()
            if len(response.content) > MAX_RELEASE_RESPONSE_BYTES:
                raise ValueError("响应体超过限制")
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("响应不是 JSON 对象")
        return payload

    @staticmethod
    def _parse_release(payload: Mapping[str, Any]) -> ReleaseInfo:
        if bool(payload.get("draft")) or bool(payload.get("prerelease")):
            raise ValueError("未找到稳定 Release")

        tag_name = str(payload.get("tag_name") or "").strip()
        version_tuple = parse_release_version(tag_name)
        if version_tuple is None:
            raise ValueError("tag 不是 vMAJOR.MINOR.PATCH")
        version = ".".join(str(part) for part in version_tuple)

        raw_html_url = str(payload.get("html_url") or "").strip()
        html_url = raw_html_url if raw_html_url.startswith("https://") else STABLE_RELEASE_PAGE
        assets: list[ReleaseAsset] = []
        raw_assets = payload.get("assets")
        if isinstance(raw_assets, list):
            for raw_asset in raw_assets:
                if not isinstance(raw_asset, Mapping):
                    continue
                name = str(raw_asset.get("name") or "").strip()
                url = str(raw_asset.get("browser_download_url") or "").strip()
                if name and url.startswith("https://"):
                    assets.append(ReleaseAsset(name=name, url=url))

        return ReleaseInfo(
            tag_name=tag_name,
            version=version,
            name=str(payload.get("name") or tag_name).strip() or tag_name,
            html_url=html_url,
            published_at=str(payload.get("published_at") or "").strip(),
            body=str(payload.get("body") or "").strip()[:12000],
            assets=tuple(assets),
        )


__all__ = [
    "MAX_RELEASE_RESPONSE_BYTES",
    "RELEASE_CHECK_INTERVAL_SECONDS",
    "ReleaseAsset",
    "ReleaseCheckResult",
    "ReleaseChecker",
    "ReleaseInfo",
    "STABLE_RELEASE_API",
    "STABLE_RELEASE_PAGE",
    "STABLE_REPOSITORY",
    "parse_release_version",
    "update_check_due",
]
