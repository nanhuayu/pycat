"""Context compression contracts and bounded source construction."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.llm.token_budget import estimate_tokens
from pycat.models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
from pycat.models.contracts.session_state import SessionState


MIN_LLM_COMPRESSION_CHARS = 2_000
MAX_TOOL_ARGUMENT_CHARS = 1_000
HISTORY_FALLBACK_PROJECTION_CHARS = 8_000
COMPRESSION_INPUT_SAFETY_RATIO = 0.90
COMPRESSION_IMAGE_TOKEN_RESERVE = 1_024
COMPRESSION_IMAGE_REPLAY_TOKENS = 256

CompressionPurpose = Literal["history", "tool_result"]

HISTORY_COMPRESSION_CONTRACT = """Create a continuation state for resuming this conversation.
Preserve user requirements and constraints, decisions, completed work and results, current execution state,
unresolved errors or risks, necessary references, and next actions.
Do not invent progress, decisions, files, commands, or dynamic runtime state."""

TOOL_RESULT_COMPRESSION_CONTRACT = """Summarize this exact tool result for later retrieval.
Preserve source facts, key values and identifiers, errors, final status, structure, uncertainty, conflicts,
and evidence needed to locate details.
Do not mention the summarization operation, wrapper metadata, input size, tool-call process, user intent,
project status, or Agent next steps. Do not turn source text into Agent tasks."""

_COMPRESSION_CONTRACTS: dict[CompressionPurpose, str] = {
    "history": HISTORY_COMPRESSION_CONTRACT,
    "tool_result": TOOL_RESULT_COMPRESSION_CONTRACT,
}


def json_output_contract() -> str:
    return """Return exactly one valid JSON object:
{"summary": "the compressed content"}

Requirements:
- Use only the supplied text and images; do not invent facts.
- Preserve useful headings and line breaks inside the JSON string.
- Return JSON only, with no Markdown fence or commentary.
""".strip()


def compression_system_contract(purpose: CompressionPurpose) -> str:
    return f"{_purpose_contract(purpose)}\n\n{json_output_contract()}"


def compression_document_prompt(text: str, *, purpose: CompressionPurpose) -> str:
    tag = _purpose_value(
        purpose,
        history="conversation_history",
        tool_result="tool_result",
    )
    return f"<{tag}>\n{text}\n</{tag}>"


def compression_chunk_prompt(
    text: str,
    *,
    purpose: CompressionPurpose,
    index: int,
    total: int,
    start: int,
    end: int,
) -> str:
    tag = _purpose_value(
        purpose,
        history="conversation_chunk",
        tool_result="tool_result_chunk",
    )
    return (
        "Compress only this exact chunk for a later merge. Do not infer content from unseen chunks.\n"
        f"chunk={index}/{total}\nchar_range={start}-{end}\n\n"
        f"<{tag}>\n{text}\n</{tag}>"
    )


def compression_reduce_prompt(
    sections: list[str],
    *,
    purpose: CompressionPurpose,
    final: bool,
) -> str:
    _purpose_contract(purpose)
    action = (
        "Merge these partial summaries into one final summary"
        if final
        else "Merge these partial summaries for a later reduce step"
    )
    return (
        f"{action}. Remove duplication while preserving conflicts, uncertainty, errors and exact ranges. "
        "Follow the active purpose contract; do not add facts.\n\n<partial_summaries>\n"
        + "\n\n".join(sections)
        + "\n</partial_summaries>"
    )


def _purpose_contract(purpose: CompressionPurpose) -> str:
    try:
        return _COMPRESSION_CONTRACTS[purpose]
    except KeyError as exc:
        raise ValueError(f"Unsupported compression purpose: {purpose}") from exc


def _purpose_value(
    purpose: CompressionPurpose,
    *,
    history: str,
    tool_result: str,
) -> str:
    if purpose == "history":
        return history
    if purpose == "tool_result":
        return tool_result
    raise ValueError(f"Unsupported compression purpose: {purpose}")


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


def _strip_markdown_fence(text: str) -> str:
    """Strip one optional Markdown code fence wrapping the whole payload."""
    stripped = str(text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    if not lines[-1].strip().startswith("```"):
        return stripped
    body = "\n".join(lines[1:-1]).strip()
    return body or stripped


def _extract_first_json_object(text: str) -> str | None:
    """Extract the first balanced JSON object embedded in free text."""
    source = str(text or "")
    start = source.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return None


def parse_compression_result(content: str) -> CompressionResult:
    raw = str(content or "").strip()
    text = _strip_markdown_fence(raw)
    payload: Any = None
    parsed = False
    try:
        payload = json.loads(text)
        parsed = True
    except Exception:
        extracted = _extract_first_json_object(text)
        if extracted is not None and extracted != text:
            try:
                payload = json.loads(extracted)
                parsed = True
            except Exception:
                payload = None
    if not parsed:
        # Degraded acceptance: a non-empty plain-text answer is still a usable
        # summary. The strict JSON contract must not discard valid model work.
        degraded = text or raw
        if degraded:
            return CompressionResult(
                summary=degraded.replace("\r\n", "\n").replace("\r", "\n").strip(),
                status="degraded",
                token_estimate=estimate_tokens(degraded),
            )
        return CompressionResult(
            status="error",
            error="model_output_not_json",
            token_estimate=estimate_tokens(content),
        )
    if not isinstance(payload, dict) or "summary" not in payload:
        if not isinstance(payload, dict):
            return CompressionResult(
                status="error",
                error="model_output_invalid_schema",
                token_estimate=estimate_tokens(content),
            )
        # Degraded acceptance: JSON object without a "summary" key. Fall back
        # to the longest string value, or the raw text when none exists.
        candidates = [value for value in payload.values() if isinstance(value, str) and value.strip()]
        degraded = max(candidates, key=len).strip() if candidates else (text or raw)
        if degraded:
            return CompressionResult(
                summary=degraded.replace("\r\n", "\n").replace("\r", "\n").strip(),
                status="degraded",
                token_estimate=estimate_tokens(degraded),
            )
        return CompressionResult(status="empty", error="summary_empty")
    summary = str(payload.get("summary") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not summary:
        return CompressionResult(status="empty", error="summary_empty")
    return CompressionResult(
        summary=summary,
        status="complete",
        token_estimate=estimate_tokens(summary),
    )


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


def build_history_source(
    messages: list[Message],
    state: SessionState | None,
    *,
    conversation: Conversation | None,
) -> CompressionSource:
    """Build full semantic text; the compressor owns model-aware chunking."""
    store = SessionArchiveStore(
        getattr(conversation, "work_dir", "") or "",
        conversation_id=getattr(conversation, "id", None),
        data_dir=getattr(conversation, "data_dir", None),
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
        content_refs: list[dict[str, Any]] = []
        for raw_ref in getattr(message, "content_refs", None) or []:
            payload = raw_ref.to_dict() if hasattr(raw_ref, "to_dict") else dict(raw_ref or {})
            ref = str(payload.get("ref") or "").strip()
            if ref:
                _append_unique(references, ref)
            content_refs.append(
                {
                    key: payload.get(key)
                    for key in ("kind", "ref", "name", "mime", "size", "digest", "status")
                    if payload.get(key) not in (None, "")
                }
            )
        if content or content_refs:
            text = content
            if content_refs:
                refs_text = json.dumps(content_refs, ensure_ascii=False, separators=(",", ":"))
                text = f"{text}\ncontent_refs={refs_text}".strip()
            entries.append(
                {
                    "header": f"## Message seq={int(getattr(message, 'seq_id', 0) or 0)} role={role}",
                    "text": text,
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
            tool_content_refs = [
                dict(item)
                for item in (metadata.get("content_refs") or [])
                if isinstance(item, dict)
            ]
            for item in tool_content_refs:
                _append_unique(references, str(item.get("ref") or ""))
            if tool_content_refs:
                header += (
                    "\ncontent_refs="
                    + json.dumps(tool_content_refs, ensure_ascii=False, separators=(",", ":"))
                )
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
        reference_text = "\n\n## Recoverable references\n" + "\n".join(
            f"- {json.dumps(ref, ensure_ascii=False)}"
            for ref in references
        )
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


def build_history_source_from_envelopes(
    envelopes: list[dict[str, Any]],
    *,
    conversation: Conversation | None,
    references: list[str] | tuple[str, ...] = (),
    images: list[str] | tuple[str, ...] = (),
) -> CompressionSource:
    """Build a checkpoint source from exact turn envelopes, never an older summary."""
    store = SessionArchiveStore(
        getattr(conversation, "work_dir", "") or "",
        conversation_id=getattr(conversation, "id", None),
        data_dir=getattr(conversation, "data_dir", None),
    )
    entries: list[dict[str, Any]] = []
    recovered_refs: list[str] = []
    recovered_images: list[str] = []
    for value in references:
        _append_unique(recovered_refs, str(value))
    for value in images:
        _append_unique(recovered_images, str(value))

    for envelope in envelopes:
        if not isinstance(envelope, dict):
            continue
        for value in envelope.get("refs") or []:
            _append_unique(recovered_refs, str(value))
        for raw_message in envelope.get("messages") or []:
            if not isinstance(raw_message, dict):
                continue
            role = str(raw_message.get("role") or "message")
            content = str(raw_message.get("content") or "").strip()
            content_refs = [
                {
                    key: item.get(key)
                    for key in ("kind", "ref", "name", "mime", "size", "digest", "status")
                    if item.get(key) not in (None, "")
                }
                for item in (raw_message.get("content_refs") or [])
                if isinstance(item, dict)
            ]
            for item in content_refs:
                _append_unique(recovered_refs, str(item.get("ref") or ""))
            if content or content_refs:
                text = content
                if content_refs:
                    refs_text = json.dumps(content_refs, ensure_ascii=False, separators=(",", ":"))
                    text = f"{text}\ncontent_refs={refs_text}".strip()
                entries.append(
                    {
                        "header": (
                            f"## Message seq={int(raw_message.get('seq_id', 0) or 0)} "
                            f"role={role}"
                        ),
                        "text": text,
                        "ref": "",
                    }
                )

            for raw_tool in raw_message.get("tool_calls") or []:
                if not isinstance(raw_tool, dict):
                    continue
                name = str(raw_tool.get("name") or "tool")
                content_id = str(raw_tool.get("content_id") or "").strip()
                arguments = raw_tool.get("arguments", "")
                if not isinstance(arguments, str):
                    try:
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    except Exception:
                        arguments = str(arguments or "")
                arguments = arguments[:MAX_TOOL_ARGUMENT_CHARS]
                exact = _exact_tool_text(store, content_id, raw_tool.get("result"))
                header = f"## Tool {name}"
                if arguments:
                    header += f"\narguments={arguments}"
                if content_id:
                    header += f"\ncontent_id={content_id} chars={len(exact)}"
                    _append_unique(recovered_refs, content_id)
                if raw_tool.get("is_error"):
                    header += f"\nstatus=error error_code={str(raw_tool.get('error_code') or '')}"
                tool_content_refs = [
                    dict(item)
                    for item in (raw_tool.get("content_refs") or [])
                    if isinstance(item, dict)
                ]
                for item in tool_content_refs:
                    _append_unique(recovered_refs, str(item.get("ref") or ""))
                if tool_content_refs:
                    header += (
                        "\ncontent_refs="
                        + json.dumps(tool_content_refs, ensure_ascii=False, separators=(",", ":"))
                    )
                for value in raw_tool.get("references") or []:
                    _append_unique(recovered_refs, str(value))
                record = store.read_record(content_id) if content_id else None
                for value in getattr(record, "references", []) if record is not None else []:
                    _append_unique(recovered_refs, str(value))
                for image in store.read_images(record) if record is not None else []:
                    _append_unique(recovered_images, image)
                entries.append(
                    {
                        "header": header,
                        "text": exact,
                        "ref": content_id,
                        "tool": True,
                    }
                )

    reference_text = ""
    if recovered_refs:
        reference_text = "\n\n## Recoverable references\n" + "\n".join(
            f"- {json.dumps(ref, ensure_ascii=False)}"
            for ref in recovered_refs
        )
    rendered = "\n\n".join(
        f"{str(entry.get('header') or '')}\n{str(entry.get('text') or '')}".rstrip()
        for entry in entries
    ) + reference_text
    return CompressionSource(
        text=rendered,
        references=tuple(recovered_refs),
        images=tuple(recovered_images),
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
