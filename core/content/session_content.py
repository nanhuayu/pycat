"""Canonical session input snapshots and request-time materialization."""
from __future__ import annotations

import base64
import codecs
import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from models.contracts.content import (
    ContentRef,
    InputPreparationFailure,
    InputPreparationResult,
)
from models.conversation import Conversation, Message
from models.session_paths import resolve_session_root
from core.content.office import extract_office_text, is_office_attachment


MAX_INPUT_FILE_BYTES = 25 * 1024 * 1024
MAX_INPUT_BATCH_BYTES = 64 * 1024 * 1024
MAX_MATERIALIZED_TEXT_BYTES = 1024 * 1024
TEXT_ATTACHMENT_BUDGET_RATIO = 0.25
TEXT_ATTACHMENT_BYTES_PER_TOKEN = 3
_SAFE_CONTENT_ID = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


@dataclass
class RequestContentCache:
    """Verified immutable input data reused only during one LLM request."""

    snapshots: dict[tuple[str, str], tuple[ContentRef, bytes]] = field(default_factory=dict)
    data_urls: dict[tuple[str, str], str] = field(default_factory=dict)


def text_attachment_byte_budget(prompt_limit: int) -> int:
    proportional = int(max(0, int(prompt_limit or 0)) * TEXT_ATTACHMENT_BUDGET_RATIO * TEXT_ATTACHMENT_BYTES_PER_TOKEN)
    return min(MAX_MATERIALIZED_TEXT_BYTES, proportional)


class SessionContentService:
    """Own immutable user-selected inputs below one conversation session root."""

    def prepare_inputs(
        self,
        conversation: Conversation,
        attachments: Iterable[dict[str, Any]],
        *,
        message_id: str = "",
    ) -> InputPreparationResult:
        prepared: list[ContentRef] = []
        failures: list[InputPreparationFailure] = []
        created_refs: list[ContentRef] = []
        created_ids: set[str] = set()
        total_size = 0
        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue
            source = str(attachment.get("path") or "")
            try:
                raw, detected_name, detected_mime = self._read_source(source)
                name = self._safe_name(attachment.get("name")) or detected_name
                mime = self._safe_mime(attachment.get("mime")) or detected_mime
                if total_size + len(raw) > MAX_INPUT_BATCH_BYTES:
                    raise ValueError(f"attachment batch exceeds {MAX_INPUT_BATCH_BYTES} bytes")
                ref, created = self._store(
                    conversation,
                    raw=raw,
                    name=name,
                    mime=mime,
                    message_id=message_id,
                )
                total_size += len(raw)
                prepared.append(ref)
                if created and ref.id not in created_ids:
                    created_refs.append(ref)
                    created_ids.add(ref.id)
            except Exception as exc:
                failures.append(InputPreparationFailure(source=source, error=str(exc)))
        return InputPreparationResult(
            refs=prepared,
            failures=failures,
            created_refs=created_refs,
        )

    def cleanup_unreferenced(
        self,
        conversation: Conversation,
        refs: Iterable[ContentRef | str],
    ) -> list[str]:
        referenced = {
            self._ref_value(ref)
            for message in (getattr(conversation, "messages", []) or [])
            for ref in (getattr(message, "content_refs", []) or [])
        }
        removed: list[str] = []
        for ref in refs or []:
            value = self._ref_value(ref)
            if not value or value in referenced:
                continue
            try:
                record_dir = self._record_dir(conversation, ref)
                if record_dir.is_dir():
                    shutil.rmtree(record_dir)
                removed.append(value)
            except FileNotFoundError:
                removed.append(value)
        return removed

    def load_ref(self, conversation: Conversation, ref: ContentRef | str) -> ContentRef:
        record_dir = self._record_dir(conversation, ref)
        payload = json.loads((record_dir / "meta.json").read_text(encoding="utf-8"))
        loaded = ContentRef.from_dict(payload)
        if (
            loaded.kind != "input"
            or loaded.id != record_dir.name
            or loaded.digest != record_dir.name
            or loaded.ref != f"input:{record_dir.name}"
            or loaded.size < 0
        ):
            raise ValueError("invalid input metadata")
        return loaded

    def resolve_original(self, conversation: Conversation, ref: ContentRef | str) -> Path:
        original = self._record_dir(conversation, ref) / "original"
        if not original.is_file():
            raise FileNotFoundError(f"input snapshot is missing: {self._ref_value(ref)}")
        return original

    def materialize_messages(
        self,
        conversation: Conversation,
        messages: Iterable[Message],
        *,
        include_image_data: bool = True,
        text_byte_budget: int | None = None,
        cache: RequestContentCache | None = None,
    ) -> list[Message]:
        remaining = [
            max(0, int(MAX_MATERIALIZED_TEXT_BYTES if text_byte_budget is None else text_byte_budget))
        ]
        source_messages = list(messages or [])
        newest_first = [
            self._materialize_message(
                conversation,
                message,
                include_image_data=include_image_data,
                remaining_text_bytes=remaining,
                cache=cache,
            )
            for message in reversed(source_messages)
        ]
        return list(reversed(newest_first))

    def materialize_message(
        self,
        conversation: Conversation,
        message: Message,
        *,
        include_image_data: bool = True,
        text_byte_budget: int | None = None,
        cache: RequestContentCache | None = None,
    ) -> Message:
        remaining = [
            max(0, int(MAX_MATERIALIZED_TEXT_BYTES if text_byte_budget is None else text_byte_budget))
        ]
        return self._materialize_message(
            conversation,
            message,
            include_image_data=include_image_data,
            remaining_text_bytes=remaining,
            cache=cache,
        )

    def _materialize_message(
        self,
        conversation: Conversation,
        message: Message,
        *,
        include_image_data: bool,
        remaining_text_bytes: list[int],
        cache: RequestContentCache | None,
    ) -> Message:
        materialized = copy.deepcopy(message)
        refs = list(getattr(message, "content_refs", []) or [])
        if not refs:
            return materialized

        text_parts: list[str] = []
        images: list[str] = list(materialized.images or [])
        for raw_ref in refs:
            try:
                requested = raw_ref if isinstance(raw_ref, ContentRef) else ContentRef.from_dict(raw_ref)
                ref, raw, cache_key = self._load_snapshot(conversation, requested, cache=cache)
                mime = str(ref.mime or "application/octet-stream").lower()
                name = str(requested.name or ref.name or "attachment")
                label = f"{name} ({mime}, {ref.ref})"
                if mime.startswith("image/"):
                    if include_image_data:
                        data_url = cache.data_urls.get(cache_key) if cache is not None else None
                        if data_url is None:
                            encoded = base64.b64encode(raw).decode("ascii")
                            data_url = f"data:{mime};base64,{encoded}"
                            if cache is not None:
                                cache.data_urls[cache_key] = data_url
                        images.append(data_url)
                    else:
                        images.append(ref.ref)
                    text_parts.append(f"[Attachment: {label}]")
                    continue
                if is_office_attachment(name, mime):
                    available = max(0, int(remaining_text_bytes[0] or 0))
                    extracted = extract_office_text(
                        raw,
                        name=name,
                        mime=mime,
                        max_bytes=available,
                    )
                    remaining_text_bytes[0] = max(
                        0,
                        available - len(extracted.text.encode("utf-8")),
                    )
                    suffix = "\n[truncated=true]" if extracted.truncated else ""
                    text_parts.append(
                        f"--- Office attachment: {label} ---\n"
                        f"{extracted.text}{suffix}\n"
                        "--- End Office attachment ---"
                    )
                    continue
                if self._is_text(ref):
                    available = max(0, int(remaining_text_bytes[0] or 0))
                    included = min(len(raw), available)
                    truncated = included < len(raw)
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    view = decoder.decode(raw[:included], final=not truncated)
                    remaining_text_bytes[0] = max(0, available - included)
                    suffix = "\n[truncated=true]" if truncated else ""
                    text_parts.append(
                        f"--- Attachment: {label} ---\n{view}{suffix}\n--- End Attachment ---"
                    )
                    continue
                text_parts.append(f"[Attachment: {label}; content is not automatically included]")
            except Exception as exc:
                text_parts.append(f"[Attachment unavailable: {self._ref_value(raw_ref)}; {exc}]")

        if text_parts:
            materialized.content = "\n\n".join(
                part for part in (str(materialized.content or ""), *text_parts) if part
            )
        materialized.images = images
        return materialized

    def _store(
        self,
        conversation: Conversation,
        *,
        raw: bytes,
        name: str,
        mime: str,
        message_id: str,
    ) -> tuple[ContentRef, bool]:
        if len(raw) > MAX_INPUT_FILE_BYTES:
            raise ValueError(f"attachment exceeds {MAX_INPUT_FILE_BYTES} bytes: {name}")
        digest = hashlib.sha256(raw).hexdigest()
        input_root = resolve_session_root(conversation.work_dir, conversation.id) / "input"
        record_dir = input_root / digest
        original = record_dir / "original"
        metadata = record_dir / "meta.json"

        if metadata.exists():
            existing = ContentRef.from_dict(json.loads(metadata.read_text(encoding="utf-8")))
            if existing.digest == digest and existing.size == len(raw):
                existing_raw = self._read_bounded_file(original)
                if len(existing_raw) != len(raw) or hashlib.sha256(existing_raw).hexdigest() != digest:
                    raise ValueError(f"existing input snapshot failed integrity validation: {digest}")
                return ContentRef(
                    **{
                        **existing.to_dict(),
                        "name": name or existing.name,
                        "mime": mime or existing.mime,
                        "message_id": str(message_id or ""),
                    }
                ), False
            raise ValueError(f"input metadata does not match content digest: {digest}")

        ref = ContentRef(
            id=digest,
            name=name or "attachment",
            mime=mime or "application/octet-stream",
            size=len(raw),
            digest=digest,
            ref=f"input:{digest}",
            message_id="",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        created = self._publish_record(input_root, record_dir, raw=raw, ref=ref)
        stored = ContentRef.from_dict(json.loads(metadata.read_text(encoding="utf-8")))
        stored_raw = self._read_bounded_file(original)
        if len(stored_raw) != stored.size or hashlib.sha256(stored_raw).hexdigest() != stored.digest:
            raise ValueError(f"input snapshot failed integrity validation: {digest}")
        return ContentRef(
            **{
                **stored.to_dict(),
                "name": name or stored.name,
                "mime": mime or stored.mime,
                "message_id": str(message_id or ""),
            }
        ), created

    @staticmethod
    def _publish_record(
        input_root: Path,
        record_dir: Path,
        *,
        raw: bytes,
        ref: ContentRef,
    ) -> bool:
        input_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{record_dir.name}-", dir=input_root))
        created = False
        try:
            (staging / "original").write_bytes(raw)
            (staging / "meta.json").write_text(
                json.dumps(ref.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.rename(staging, record_dir)
                created = True
            except OSError:
                if not record_dir.is_dir():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return created

    @staticmethod
    def _read_source(source: str) -> tuple[bytes, str, str]:
        if source.startswith("data:"):
            header, separator, payload = source.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ValueError("only base64 data URLs are supported")
            mime = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
            if not mime.startswith("image/"):
                raise ValueError("only image data URLs are supported")
            max_encoded_size = ((MAX_INPUT_FILE_BYTES + 2) // 3) * 4
            if len(payload) > max_encoded_size:
                raise ValueError(f"attachment exceeds {MAX_INPUT_FILE_BYTES} bytes")
            try:
                raw = base64.b64decode(payload, validate=True)
            except Exception as exc:
                raise ValueError("invalid base64 attachment") from exc
            extension = _IMAGE_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or "")
            return raw, f"pasted-image{extension}", mime

        path = Path(source).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"attachment is not a file: {source}")
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_FILE_BYTES + 1)
        if len(raw) > MAX_INPUT_FILE_BYTES:
            raise ValueError(f"attachment exceeds {MAX_INPUT_FILE_BYTES} bytes: {path.name}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return raw, path.name, mime

    @staticmethod
    def _safe_name(value: Any) -> str:
        name = re.split(r"[\\/]", str(value or "").strip())[-1]
        name = "".join(character for character in name if ord(character) >= 32).strip()
        return name[:255] if name not in {".", ".."} else ""

    @staticmethod
    def _safe_mime(value: Any) -> str:
        mime = str(value or "").strip().lower()
        if not mime or len(mime) > 127 or "/" not in mime:
            return ""
        if any(ord(character) < 33 or ord(character) > 126 for character in mime):
            return ""
        return mime

    def _load_snapshot(
        self,
        conversation: Conversation,
        requested: ContentRef,
        *,
        cache: RequestContentCache | None,
    ) -> tuple[ContentRef, bytes, tuple[str, str]]:
        record_dir = self._record_dir(conversation, requested)
        cache_key = (str(record_dir.parent.resolve()), record_dir.name)
        if cache is not None and cache_key in cache.snapshots:
            ref, raw = cache.snapshots[cache_key]
            return ref, raw, cache_key

        ref = self.load_ref(conversation, requested)
        original = self.resolve_original(conversation, ref)
        raw = self._read_bounded_file(original)
        if (
            len(raw) > MAX_INPUT_FILE_BYTES
            or len(raw) != ref.size
            or hashlib.sha256(raw).hexdigest() != ref.digest
        ):
            raise ValueError("input snapshot failed integrity validation")
        if cache is not None:
            cache.snapshots[cache_key] = (ref, raw)
        return ref, raw, cache_key

    @staticmethod
    def _read_bounded_file(path: Path) -> bytes:
        with path.open("rb") as stream:
            return stream.read(MAX_INPUT_FILE_BYTES + 1)

    @staticmethod
    def _is_text(ref: ContentRef) -> bool:
        mime = str(ref.mime or "").lower()
        if mime.startswith("text/"):
            return True
        return mime in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
        }

    @classmethod
    def _record_dir(cls, conversation: Conversation, ref: ContentRef | str) -> Path:
        value = cls._ref_value(ref)
        prefix, separator, content_id = value.partition(":")
        if prefix != "input" or not separator or not _SAFE_CONTENT_ID.fullmatch(content_id):
            raise ValueError(f"unsafe input ref: {value!r}")
        input_root = resolve_session_root(conversation.work_dir, conversation.id) / "input"
        record_dir = input_root / content_id
        if record_dir.parent != input_root:
            raise ValueError(f"unsafe input ref: {value!r}")
        return record_dir

    @staticmethod
    def _ref_value(ref: ContentRef | str | Any) -> str:
        if isinstance(ref, ContentRef):
            return str(ref.ref or "")
        if isinstance(ref, dict):
            return str(ref.get("ref") or "")
        return str(ref or "")
