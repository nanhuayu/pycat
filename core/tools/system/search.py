from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, urlunparse

from core.tools.base import BaseTool, ToolContext, ToolResult

class WebSearchTool(BaseTool):
    """Built-in web search tool.

    Schema aligned with SearchService: uses 'additional_context' to allow
    the model to refine pre-extracted search terms.
    """

    def __init__(self, search_service, prepared_queries: Optional[List[str]] = None):
        self.search_service = search_service
        self.prepared_queries = prepared_queries or []

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        base_desc = (
            "Web search tool for finding current information, news, and real-time data from the internet. "
            "Use concise keyword queries; if results are irrelevant or empty, retry with a narrower query, "
            "source names, dates, or English/Chinese variants rather than repeating the same query."
        )
        if self.prepared_queries:
             base_desc += f"\n\nPrepared queries: {', '.join(self.prepared_queries)}"
        return base_desc

    @property
    def category(self) -> str:
        return "search"

    @property
    def source(self) -> str:
        return "search"

    @property
    def input_schema(self) -> Dict[str, Any]:
        # Aligned with SearchService.get_tool_schema: use additional_context
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Primary search query. Prefer 3-12 precise keywords, include dates/source names when useful."
                },
                "additional_context": {
                    "type": "string",
                    "description": "Optional query/context. Used when query is omitted."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of search results to return. Default: 5. Allowed range: 1-20."
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        primary = str(arguments.get("query") or "").strip()
        additional = str(arguments.get("additional_context") or "").strip()
        if primary:
            query = primary
        elif additional:
            query = additional
        elif self.prepared_queries:
            query = " ".join(self.prepared_queries)
        else:
            query = ""

        if not query:
            return ToolResult("No search query provided.")

        result = await self.search_service.search(query, max_results=arguments.get("max_results"))
        return ToolResult(result)


class FetchUrlTool(BaseTool):
    """Fetch a URL after search has identified a concrete source."""

    MAX_CHARS = 80_000

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch the content of a specific http(s) URL and return text, HTML, or lightweight markdown. "
            "Use after web_search finds a relevant source, or when the user provides a URL."
        )

    @property
    def category(self) -> str:
        return "search"

    @property
    def source(self) -> str:
        return "search"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http(s) URL to fetch.",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["text", "html", "markdown"],
                    "description": "Output format. Default: text.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum returned characters, 1000-80000. Default: 20000.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        url = str(arguments.get("url") or "").strip()
        if not url:
            return ToolResult("Missing 'url'.", is_error=True)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ToolResult("Only absolute http(s) URLs are supported.", is_error=True)
        request_url = self._iri_to_uri(url)

        output_format = str(arguments.get("output_format") or "text").strip().lower()
        if output_format not in {"text", "html", "markdown"}:
            output_format = "text"
        try:
            max_chars = int(arguments.get("max_chars") or 20_000)
        except Exception:
            max_chars = 20_000
        max_chars = max(1_000, min(max_chars, self.MAX_CHARS))

        request = urllib.request.Request(
            request_url,
            headers={
                "User-Agent": "PyCat-Agent/1.0 (+https://local.agent)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            },
            method="GET",
        )
        try:
            status, final_url, content_type, raw = await asyncio.to_thread(self._fetch_bytes, request, url)
        except urllib.error.HTTPError as exc:
            return ToolResult(f"Fetch failed: HTTP {exc.code} {exc.reason}", is_error=True)
        except Exception as exc:
            return ToolResult(f"Fetch failed: {exc}", is_error=True)

        encoding = self._detect_encoding(content_type) or "utf-8"
        text = raw.decode(encoding, errors="replace")
        if output_format == "html":
            body = text
        elif output_format == "markdown":
            body = self._html_to_markdown(text)
        else:
            body = self._html_to_text(text)

        truncated = len(body) > max_chars
        if truncated:
            body = body[:max_chars]

        payload = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "output_format": output_format,
            "truncated": truncated,
            "content_chars": len(body),
            "content": body,
        }
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _fetch_bytes(request: urllib.request.Request, fallback_url: str) -> tuple[int, str, str, bytes]:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = int(getattr(response, "status", 0) or 0)
            final_url = str(response.geturl() or fallback_url)
            content_type = str(response.headers.get("Content-Type") or "")
            raw = response.read(2_000_000)
        return status, final_url, content_type, raw

    @staticmethod
    def _iri_to_uri(url: str) -> str:
        parsed = urlparse(url)
        netloc = parsed.netloc.encode("idna").decode("ascii") if parsed.netloc else ""
        path = quote(parsed.path or "", safe="/%:@!$&'()*+,;=")
        query = quote(parsed.query or "", safe="=&?/%:@!$'()*+,;[]")
        fragment = quote(parsed.fragment or "", safe="/?%:@!$&'()*+,;=")
        return urlunparse((parsed.scheme, netloc, path, parsed.params, query, fragment))

    @staticmethod
    def _detect_encoding(content_type: str) -> str | None:
        match = re.search(r"charset=([^;\s]+)", content_type or "", flags=re.I)
        return match.group(1).strip('"') if match else None

    @staticmethod
    def _html_to_text(text: str) -> str:
        text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|section|article|header|footer|li|h[1-6])>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return text.strip()

    @classmethod
    def _html_to_markdown(cls, text: str) -> str:
        text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
        for level in range(1, 7):
            text = re.sub(
                rf"(?is)<h{level}[^>]*>(.*?)</h{level}>",
                lambda m, lvl=level: "\n" + "#" * lvl + " " + cls._html_to_text(m.group(1)) + "\n",
                text,
            )
        text = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda m: "\n- " + cls._html_to_text(m.group(1)), text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n\n", text)
        return cls._html_to_text(text)
