from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import re
import urllib.error
import urllib.request
from typing import Any, Dict
from urllib.parse import quote, urlparse, urlunparse

from core.tools.base import BaseTool, ToolContext, ToolResult


@dataclass(frozen=True)
class _FetchResponse:
    status: int
    final_url: str
    content_type: str
    raw: bytes
    headers: Dict[str, str]
    reason: str = ""


class _ResponseTooLarge(ValueError):
    pass


class WebSearchTool(BaseTool):
    def __init__(self, search_service):
        self.search_service = search_service

    @property
    def name(self) -> str:
        return "web__search"

    @property
    def display_name(self) -> str:
        return "搜索网页"

    @property
    def description(self) -> str:
        return "Search the configured web provider for current sources using one explicit query."

    @property
    def category(self) -> str:
        return "web"

    @property
    def source(self) -> str:
        return "search"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Concise search query."},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult("query is required.", is_error=True)
        return ToolResult(await self.search_service.search(query))


class FetchUrlTool(BaseTool):
    TIMEOUT_SECONDS = 20
    MAX_READ_BYTES = 2_000_000
    REQUEST_HEADERS = {
        "User-Agent": "PyCat/1.0",
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json,*/*;q=0.5",
    }

    @property
    def name(self) -> str:
        return "web__fetch"

    @property
    def display_name(self) -> str:
        return "读取网页"

    @property
    def description(self) -> str:
        return "Fetch one public HTTP URL; authentication and interactive browser challenges are not bypassed."

    @property
    def category(self) -> str:
        return "web"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute http(s) URL."}},
            "required": ["url"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        url = str(arguments.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ToolResult("Only absolute http(s) URLs are supported.", is_error=True)
        request_url = self._iri_to_uri(url)
        request = urllib.request.Request(request_url, headers=dict(self.REQUEST_HEADERS), method="GET")
        try:
            response = await asyncio.to_thread(self._fetch_bytes, request, url)
        except _ResponseTooLarge:
            return ToolResult(
                f"http_error: The response exceeds the {self.MAX_READ_BYTES}-byte download limit.\nurl={url}",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                f"http_error: The URL could not be fetched.\nstatus=0\nurl={url}\nmessage={exc}",
                is_error=True,
            )

        if not 200 <= response.status < 400:
            code, message = self._diagnose_http_failure(response)
            return ToolResult(
                f"{code}: {message}\nstatus={response.status}\nurl={url}\nfinal_url={response.final_url}",
                is_error=True,
            )

        decoded = self._decode_response_text(response)
        body = self._html_to_markdown(decoded) if self._looks_like_html(decoded, response.content_type) else decoded.strip()
        header = (
            f"url={url}\nfinal_url={response.final_url}\nstatus={response.status}\n"
            f"content_type={response.content_type or '-'}"
        )
        return ToolResult(f"{header}\n\n{body}")

    @classmethod
    def _fetch_bytes(cls, request: urllib.request.Request, fallback_url: str) -> _FetchResponse:
        try:
            with urllib.request.urlopen(request, timeout=cls.TIMEOUT_SECONDS) as response:
                headers = {str(key): str(value) for key, value in response.headers.items()}
                content_length = headers.get("Content-Length") or headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > cls.MAX_READ_BYTES:
                        raise _ResponseTooLarge
                raw = response.read(cls.MAX_READ_BYTES + 1)
                if len(raw) > cls.MAX_READ_BYTES:
                    raise _ResponseTooLarge
                return _FetchResponse(
                    status=int(getattr(response, "status", 0) or 0),
                    final_url=str(response.geturl() or fallback_url),
                    content_type=str(response.headers.get("Content-Type") or ""),
                    raw=raw,
                    headers=headers,
                )
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(cls.MAX_READ_BYTES)
            except Exception:
                raw = b""
            headers = {str(key): str(value) for key, value in (exc.headers.items() if exc.headers else [])}
            return _FetchResponse(
                status=int(exc.code or 0),
                final_url=str(exc.geturl() or fallback_url),
                content_type=str(headers.get("Content-Type") or headers.get("content-type") or ""),
                raw=raw,
                headers=headers,
                reason=str(exc.reason or ""),
            )

    @classmethod
    def _diagnose_http_failure(cls, response: _FetchResponse) -> tuple[str, str]:
        headers = {str(key).lower(): str(value).lower() for key, value in response.headers.items()}
        body = cls._decode_response_text(response)[:4000].lower()
        challenged = (
            headers.get("cf-mitigated") == "challenge"
            or "challenges.cloudflare.com" in body
            or "cf-chl-" in body
            or "_cf_chl_opt" in body
            or ("cloudflare" in headers.get("server", "") and "just a moment" in body)
        )
        if challenged:
            return (
                "browser_required",
                "The site requires an interactive browser challenge; use a configured browser tool or another source.",
            )
        if response.status == 429:
            return "rate_limited", "The site rate-limited this request; retry later or use another source."
        if response.status in {401, 403, 451}:
            return "access_denied", "The site denied this HTTP request; use an accessible source."
        return "http_error", f"The server returned HTTP {response.status} {response.reason}".strip()

    @staticmethod
    def _decode_response_text(response: _FetchResponse) -> str:
        match = re.search(r"charset=([^;\s]+)", response.content_type or "", flags=re.I)
        encoding = match.group(1).strip('"') if match else "utf-8"
        try:
            return response.raw.decode(encoding, errors="replace")
        except LookupError:
            return response.raw.decode("utf-8", errors="replace")

    @staticmethod
    def _looks_like_html(text: str, content_type: str) -> bool:
        return "html" in (content_type or "").lower() or bool(
            re.search(r"(?is)^\s*(<!doctype\s+html|<html|<head|<body)\b", text or "")
        )

    @staticmethod
    def _iri_to_uri(url: str) -> str:
        parsed = urlparse(url)
        netloc = parsed.netloc.encode("idna").decode("ascii") if parsed.netloc else ""
        path = quote(parsed.path or "", safe="/%:@!$&'()*+,;=")
        query = quote(parsed.query or "", safe="=&?/%:@!$'()*+,;[]")
        fragment = quote(parsed.fragment or "", safe="/?%:@!$&'()*+,;=")
        return urlunparse((parsed.scheme, netloc, path, parsed.params, query, fragment))

    @staticmethod
    def _html_to_text(text: str) -> str:
        text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|section|article|header|footer|li|h[1-6])>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    @classmethod
    def _html_to_markdown(cls, text: str) -> str:
        text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
        for level in range(1, 7):
            text = re.sub(
                rf"(?is)<h{level}[^>]*>(.*?)</h{level}>",
                lambda match, depth=level: "\n" + "#" * depth + " " + cls._html_to_text(match.group(1)) + "\n",
                text,
            )
        text = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda match: "\n- " + cls._html_to_text(match.group(1)), text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n\n", text)
        return cls._html_to_text(text)
