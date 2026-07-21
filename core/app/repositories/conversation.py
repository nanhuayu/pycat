from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from core.content.attachments import extract_composer_text
from models.conversation import Conversation


logger = logging.getLogger(__name__)


CONVERSATION_INDEX_SCHEMA_VERSION = 2
_SEARCH_ENTRY_LIMIT = 48
_SEARCH_ENTRY_TEXT_LIMIT = 600
_CONTROL_MESSAGE_PREFIXES = ("[AUTO-CONTINUE]", "[WARNING]")


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        return "\n".join(str(item) for item in value["parts"] if isinstance(item, (str, int, float)))
    return "" if value is None else str(value)


def _search_entries(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        return []

    entries: list[dict[str, str]] = []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        raw_text = _message_text(message.get("content"))
        if role == "user":
            raw_text = extract_composer_text(raw_text, message.get("metadata"))
        text = re.sub(r"\s+", " ", raw_text.strip())
        if not text or (role == "user" and text.startswith(_CONTROL_MESSAGE_PREFIXES)):
            continue
        entries.append({"role": role, "text": text[:_SEARCH_ENTRY_TEXT_LIMIT]})
        if len(entries) >= _SEARCH_ENTRY_LIMIT:
            break
    entries.reverse()
    return entries


class ConversationRepository:
    """File repository for persisted conversation JSON and metadata index."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.conversations_dir = self.data_dir / "conversations"
        self.conversations_index_file = self.data_dir / "conversations_index.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)

    def save(self, conversation: Conversation) -> bool:
        temp_name = ""
        try:
            file_path = self.conversations_dir / f"{conversation.id}.json"
            self.conversations_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{conversation.id}.",
                suffix=".tmp",
                dir=str(self.conversations_dir),
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(conversation.to_json())
            os.replace(temp_name, file_path)
            self._upsert_index(conversation)
            return True
        except Exception as exc:
            logger.warning("Error saving conversation %s: %s", getattr(conversation, "id", ""), exc)
            try:
                if temp_name:
                    Path(temp_name).unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def load(self, conversation_id: str) -> Conversation | None:
        try:
            file_path = self.conversations_dir / f"{conversation_id}.json"
            if file_path.exists():
                return Conversation.from_json(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Error loading conversation %s: %s", conversation_id, exc)
        return None

    def list_all(self) -> list[dict[str, Any]]:
        indexed = self._load_index()
        if indexed is not None:
            return indexed
        conversations = self._rebuild_index()
        self._save_index(conversations)
        return conversations

    def list_model_references(self) -> list[dict[str, str]]:
        """Read provider/model projections for one-time catalog migrations."""

        indexed = self._load_index()
        if indexed is None:
            indexed = self._rebuild_index()
            self._save_index(indexed)
        if all(
            "provider_id" in row and "provider_name" in row for row in indexed
        ):
            return [
                {
                    "provider_id": str(row.get("provider_id") or "").strip(),
                    "provider_name": str(row.get("provider_name") or "").strip(),
                    "model": str(row.get("model") or "").strip(),
                }
                for row in indexed
                if str(row.get("model") or "").strip()
            ]

        references: list[dict[str, str]] = []
        for file_path in self.conversations_dir.glob("*.json"):
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                llm_config = payload.get("llm_config") if isinstance(payload.get("llm_config"), dict) else {}
                model = str(llm_config.get("model") or payload.get("model") or "").strip()
                if not model:
                    continue
                references.append(
                    {
                        "provider_id": str(llm_config.get("provider_id") or payload.get("provider_id") or "").strip(),
                        "provider_name": str(llm_config.get("provider_name") or payload.get("provider_name") or "").strip(),
                        "model": model,
                    }
                )
            except Exception as exc:
                logger.debug("Failed to read model reference from %s: %s", file_path, exc)
        return references

    def delete(self, conversation_id: str) -> bool:
        try:
            file_path = self.conversations_dir / f"{conversation_id}.json"
            if file_path.exists():
                file_path.unlink()
                self._remove_index(conversation_id)
                return True
        except Exception as exc:
            logger.warning("Error deleting conversation %s: %s", conversation_id, exc)
        return False

    def _metadata_from_dict(self, data: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        conversation_id = str(data.get("id") or "").strip()
        if not conversation_id:
            return None
        messages = data.get("messages", [])
        return {
            "id": conversation_id,
            "title": data.get("title", "Untitled"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "work_dir": data.get("work_dir", ""),
            "provider_id": data.get("provider_id", ""),
            "provider_name": data.get("provider_name", ""),
            "model": data.get("model", ""),
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "search_entries": _search_entries(messages),
        }

    def _metadata(self, conversation: Conversation) -> dict[str, Any]:
        return self._metadata_from_dict(conversation.to_dict()) or {
            "id": str(getattr(conversation, "id", "") or ""),
            "title": str(getattr(conversation, "title", "") or "Untitled"),
            "created_at": getattr(getattr(conversation, "created_at", None), "isoformat", lambda: None)(),
            "updated_at": getattr(getattr(conversation, "updated_at", None), "isoformat", lambda: None)(),
            "work_dir": str(getattr(conversation, "work_dir", "") or ""),
            "provider_id": str(getattr(conversation, "provider_id", "") or ""),
            "provider_name": str(getattr(conversation, "provider_name", "") or ""),
            "model": str(getattr(conversation, "model", "") or ""),
            "message_count": len(getattr(conversation, "messages", []) or []),
            "search_entries": _search_entries(
                [message.to_dict() for message in (getattr(conversation, "messages", []) or [])]
            ),
        }

    def _load_index(self) -> list[dict[str, Any]] | None:
        try:
            if not self.conversations_index_file.exists():
                return None
            payload = json.loads(self.conversations_index_file.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or int(payload.get("schema_version", 0) or 0)
                != CONVERSATION_INDEX_SCHEMA_VERSION
            ):
                return None
            rows = payload.get("conversations") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return None
            if any(
                not isinstance(item, dict) or not isinstance(item.get("search_entries"), list)
                for item in rows
            ):
                return None
            ids = {
                str(item.get("id") or "").strip()
                for item in rows
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
            disk_ids = {path.stem for path in self.conversations_dir.glob("*.json") if path.stat().st_size > 0}
            if ids != disk_ids:
                return None
            out = [dict(item) for item in rows if isinstance(item, dict)]
            out.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
            return out
        except Exception as exc:
            logger.debug("Failed to load conversation index: %s", exc)
            return None

    def _save_index(self, conversations: list[dict[str, Any]]) -> None:
        try:
            self.conversations_index_file.write_text(
                json.dumps(
                    {
                        "schema_version": CONVERSATION_INDEX_SCHEMA_VERSION,
                        "conversations": list(conversations or []),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Failed to save conversation index: %s", exc)

    def _upsert_index(self, conversation: Conversation) -> None:
        rows = self._load_index()
        if rows is None:
            rows = self._rebuild_index()
        metadata = self._metadata(conversation)
        next_rows = [row for row in rows if str(row.get("id") or "") != metadata["id"]]
        next_rows.append(metadata)
        next_rows.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        self._save_index(next_rows)

    def _remove_index(self, conversation_id: str) -> None:
        rows = self._load_index()
        if rows is None:
            rows = self._rebuild_index()
        next_rows = [row for row in rows if str(row.get("id") or "") != str(conversation_id or "")]
        self._save_index(next_rows)

    def _rebuild_index(self) -> list[dict[str, Any]]:
        conversations: list[dict[str, Any]] = []
        try:
            for file_path in self.conversations_dir.glob("*.json"):
                try:
                    if file_path.stat().st_size <= 0:
                        logger.debug("Skipping empty conversation file: %s", file_path)
                        continue
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        logger.debug("Skipping non-object conversation file: %s", file_path)
                        continue
                    metadata = self._metadata_from_dict(data)
                    if metadata is None:
                        logger.debug("Skipping conversation file without id: %s", file_path)
                        continue
                    conversations.append(metadata)
                except Exception as exc:
                    logger.debug("Skipping unreadable conversation file %s: %s", file_path, exc)
        except Exception as exc:
            logger.warning("Error listing conversations from %s: %s", self.conversations_dir, exc)
        conversations.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return conversations
