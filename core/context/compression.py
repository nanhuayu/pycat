"""Context compression contracts and bounded source construction."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.content.archive_store import ArchivedContentRecord, SessionArchiveStore
from core.llm.token_budget import estimate_tokens
from models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
from models.contracts.session_state import SessionState


MIN_LLM_COMPRESSION_CHARS = 2_000
MAX_TOOL_ARGUMENT_CHARS = 1_000
MAP_SUMMARY_CHARS = 4_000
MAX_SUMMARY_CHARS = 8_000
COMPRESSION_INPUT_SAFETY_RATIO = 0.90
COMPRESSION_IMAGE_TOKEN_RESERVE = 1_024
COMPRESSION_IMAGE_REPLAY_TOKENS = 256


def json_output_contract(max_chars: int = MAX_SUMMARY_CHARS) -> str:
    return f"""Return exactly one valid JSON object:
{{"summary": "a detailed, compact continuation summary"}}

Requirements:
- Use only the supplied text and images; do not invent facts.
- Preserve user requirements, decisions, completed work, unresolved issues, errors, dates, paths, URLs, content_id values, image evidence, and next actions.
- Keep enough detail for another agent to continue the work without the removed material.
- Do not include raw dumps or a chronological tool-call ledger.
- Keep summary at or below {max(1, int(max_chars))} characters.
- Preserve useful headings and line breaks inside the JSON string.
- Return JSON only, with no Markdown fence or commentary.
""".strip()


JSON_OUTPUT_CONTRACT = json_output_contract()


@dataclass
class CompressionResult:
    summary: str = ""
    status: str = "empty"
    token_estimate: int = 0
    model: str = ""
    capability_id: str = ""
    error: str = ""
    calls: int = 0
    chunks: int = 0
    reduce_levels: int = 0
    strategy: str = ""
    vision_fallback: bool = False


@dataclass(frozen=True)
class CompressionSource:
    text: str
    references: tuple[str, ...] = field(default_factory=tuple)
    images: tuple[str, ...] = field(default_factory=tuple)
    tool_count: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text) + len(self.images) * COMPRESSION_IMAGE_REPLAY_TOKENS


@dataclass(frozen=True)
class CompressionChunk:
    text: str
    start: int
    end: int


class ArchiveCompressor(Protocol):
    async def summarize_archive(
        self,
        record: ArchivedContentRecord,
        *,
        conversation: Conversation | None = None,
        purpose: str = "maintenance",
    ) -> CompressionResult:
        ...

    async def compress_history(
        self,
        source: CompressionSource,
        *,
        conversation: Conversation | None = None,
    ) -> CompressionResult:
        ...

    def apply_archive_summary(
        self,
        record: ArchivedContentRecord,
        result: CompressionResult,
        *,
        conversation: Conversation | None = None,
    ) -> ArchivedContentRecord:
        ...


def archive_prompt(record: ArchivedContentRecord, text: str, *, purpose: str) -> str:
    header = (
        f"purpose={purpose}\ncontent_id={record.id}\nsource={record.source or record.title}\n"
        f"chars={len(text)}"
    )
    return f"Condense this exact archived content.\n\n{header}\n\n<content>\n{text}\n</content>"


def history_prompt(source: CompressionSource) -> str:
    return (
        "Condense the following exact conversation material into one continuation summary.\n\n"
        f"<conversation>\n{source.text}\n</conversation>"
    )


def build_history_source(
    messages: list[Message],
    state: SessionState | None,
    *,
    conversation: Conversation | None,
) -> CompressionSource:
    """Build full semantic text; the compressor owns model-aware chunking."""
    store = SessionArchiveStore(
        getattr(conversation, "work_dir", "") or ".",
        conversation_id=getattr(conversation, "id", None),
    )
    entries: list[dict[str, Any]] = []
    references: list[str] = []
    images: list[str] = []

    previous = str(getattr(state, "summary", "") or "").strip() if state is not None else ""
    if previous:
        entries.append({"header": "## Previous continuation summary", "text": previous, "ref": ""})

    for message in messages:
        role = str(getattr(message, "role", "message") or "message")
        content = str(getattr(message, "content", "") or "").strip()
        if content:
            entries.append(
                {
                    "header": f"## Message seq={int(getattr(message, 'seq_id', 0) or 0)} role={role}",
                    "text": content,
                    "ref": "",
                }
            )
        for tool_call in getattr(message, "tool_calls", None) or []:
            if not isinstance(tool_call, dict):
                continue
            payload = normalize_tool_result(tool_call.get("result")) if tool_call.get("result") is not None else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            name = tool_call_name(tool_call) or str(metadata.get("name") or "tool")
            content_id = str(metadata.get("content_id") or "").strip()
            arguments = _tool_arguments(tool_call)
            exact = _exact_tool_text(store, content_id, payload.get("content"))
            record = store.read_record(content_id) if content_id else None
            header = f"## Tool {name}"
            if arguments:
                header += f"\narguments={arguments}"
            if content_id:
                header += f"\ncontent_id={content_id} chars={len(exact)}"
                _append_unique(references, content_id)
            record_images = store.read_images(record) if record is not None else []
            image_count = len(record_images)
            if image_count:
                header += f"\narchived_images={image_count}"
                for image in record_images:
                    _append_unique(images, image)
            entries.append(
                {
                    "header": header,
                    "text": exact,
                    "ref": content_id,
                    "tool": True,
                }
            )
            for ref in getattr(record, "references", []) if record is not None else []:
                _append_unique(references, str(ref))

    reference_text = ""
    if references:
        reference_text = "\n\n## Recoverable references\n" + "\n".join(f"- {ref}" for ref in references)
    rendered = "\n\n".join(
        f"{str(entry.get('header') or '')}\n{str(entry.get('text') or '')}".rstrip()
        for entry in entries
    ) + reference_text
    return CompressionSource(
        text=rendered,
        references=tuple(references),
        images=tuple(images),
        tool_count=sum(1 for entry in entries if entry.get("tool")),
    )


def _tool_arguments(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    value = function.get("arguments")
    if isinstance(value, str):
        return value[:MAX_TOOL_ARGUMENT_CHARS]
    try:
        return json.dumps(value, ensure_ascii=False)[:MAX_TOOL_ARGUMENT_CHARS] if value is not None else ""
    except Exception:
        return str(value or "")[:MAX_TOOL_ARGUMENT_CHARS]


def _exact_tool_text(store: SessionArchiveStore, content_id: str, fallback: Any) -> str:
    if content_id:
        try:
            return store.read_original(content_id)
        except Exception:
            pass
    if isinstance(fallback, str):
        return fallback
    try:
        return json.dumps(fallback, ensure_ascii=False, indent=2) if fallback is not None else ""
    except Exception:
        return str(fallback or "")


def _append_unique(items: list[str], value: str) -> None:
    clean = str(value or "").strip()
    if clean and clean not in items:
        items.append(clean)


def split_text_by_token_budget(text: str, max_tokens: int) -> list[CompressionChunk]:
    source = str(text or "")
    if not source:
        return []
    budget = max(1, int(max_tokens or 0))
    if estimate_tokens(source) <= budget:
        return [CompressionChunk(text=source, start=0, end=len(source))]

    chunks: list[CompressionChunk] = []
    start = 0
    while start < len(source):
        low = start + 1
        high = len(source)
        best = low
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(source[start:middle]) <= budget:
                best = middle
                low = middle + 1
            else:
                high = middle - 1

        end = max(start + 1, best)
        if end < len(source):
            minimum_boundary = start + max(1, (end - start) // 2)
            paragraph = source.rfind("\n\n", minimum_boundary, end)
            line = source.rfind("\n", minimum_boundary, end)
            boundary = max(paragraph + 2 if paragraph >= 0 else -1, line + 1 if line >= 0 else -1)
            if boundary > start:
                end = boundary
        chunks.append(CompressionChunk(text=source[start:end], start=start, end=end))
        start = end
    return chunks


def group_sections_by_token_budget(sections: list[str], max_tokens: int) -> list[list[str]]:
    budget = max(1, int(max_tokens or 0))
    groups: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for section in sections:
        text = str(section or "")
        tokens = max(1, estimate_tokens(text))
        if current and current_tokens + tokens > budget:
            groups.append(current)
            current = []
            current_tokens = 0
        if tokens > budget:
            if current:
                groups.append(current)
                current = []
                current_tokens = 0
            groups.extend([[chunk.text] for chunk in split_text_by_token_budget(text, budget)])
            continue
        current.append(text)
        current_tokens += tokens
    if current:
        groups.append(current)
    return groups
