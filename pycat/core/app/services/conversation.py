"""Conversation lifecycle service.

Centralizes conversation CRUD and message operations that were
previously scattered across UI presenters and MainWindow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pycat.core.app.repositories.conversation import ConversationRepository
from pycat.core.app.services.importers import parse_imported_data
from pycat.core.app.state import ConversationSettingsUpdate
from pycat.core.channel.bindings import is_bound_channel_conversation
from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.content.export import document_format, export_document
from pycat.core.context.history import (
    is_real_user_message,
    restartable_user_by_id,
    restartable_user_for_assistant,
)
from pycat.core.context.turn_restart import reconcile_after_turn_restart
from pycat.core.memory.evolution import MemoryEvolutionLedger
from pycat.core.memory.service import MemoryService
from pycat.core.observability.debug_trace import resolve_debug_trace_dir
from pycat.core.observability.reader import MAX_EVENTS, read_trace_events, read_trace_node, read_trace_payload
from pycat.core.persistence import atomic_write_text
from pycat.core.state.artifact import ArtifactService
from pycat.core.state.todo import TodoService
from pycat.models.contracts.agent import ConversationBusyError, InvalidRequestError, PersistenceError
from pycat.models.conversation import Conversation, Message
from pycat.models.model_ref import build_model_ref, provider_matches_name
from pycat.models.session_paths import normalize_work_dir, resolve_session_root

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkDirChangeResult:
    """Outcome of the single workspace-switch use case."""

    ok: bool
    changed: bool = False
    moved: bool = False
    blocked_processes: int = 0
    error: str = ""
    old_work_dir: str = ""
    new_work_dir: str = ""


@dataclass(frozen=True)
class ConversationRevisionResult:
    """Outcome of restarting from one real user turn."""

    ok: bool
    conversation: Conversation | None = None
    error: str = ""


class ConversationService:
    """Manages conversation persistence and message operations."""

    def __init__(self, repository: ConversationRepository) -> None:
        self._repository = repository
        self._activity_lock = threading.RLock()
        self._active_turns: dict[str, str] = {}
        self._lifecycle_operations: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Cross-entry-point activity boundary
    # ------------------------------------------------------------------

    def begin_turn(self, conversation_id: str, *, activity_token: str | None = None) -> str | None:
        """Claim a conversation for one live Agent turn.

        Channel and Desktop runs share this small registry.  A lifecycle
        operation can therefore make one atomic decision instead of racing a
        separate GUI-only streaming flag.
        """
        key = str(conversation_id or "").strip()
        if not key:
            return None
        with self._activity_lock:
            owner = self._lifecycle_operations.get(key)
            if key in self._active_turns or (owner is not None and owner != activity_token):
                return None
            if owner is not None:
                self._lifecycle_operations.pop(key)
            token = uuid.uuid4().hex
            self._active_turns[key] = token
            return token

    def end_turn(self, conversation_id: str, token: str) -> None:
        with self._activity_lock:
            key = str(conversation_id or "").strip()
            if self._active_turns.get(key) == token:
                self._active_turns.pop(key, None)

    def begin_lifecycle(self, conversation_id: str, operation: str) -> str | None:
        """Claim a conversation for delete or workspace migration."""
        key = str(conversation_id or "").strip()
        kind = str(operation or "lifecycle").strip() or "lifecycle"
        if not key:
            return None
        with self._activity_lock:
            if key in self._active_turns or key in self._lifecycle_operations:
                return None
            token = f"{kind}:{uuid.uuid4().hex}"
            self._lifecycle_operations[key] = token
            return token

    def end_lifecycle(self, conversation_id: str, token: str | None) -> None:
        key = str(conversation_id or "").strip()
        if not key or not token:
            return
        with self._activity_lock:
            if self._lifecycle_operations.get(key) == token:
                self._lifecycle_operations.pop(key, None)

    def is_active(self, conversation_id: str) -> bool:
        key = str(conversation_id or "").strip()
        with self._activity_lock:
            return key in self._active_turns or key in self._lifecycle_operations

    def is_turn_active(self, conversation_id: str) -> bool:
        with self._activity_lock:
            return str(conversation_id or "").strip() in self._active_turns

    def is_lifecycle_active(self, conversation_id: str) -> bool:
        with self._activity_lock:
            return str(conversation_id or "").strip() in self._lifecycle_operations

    def active_operation(self, conversation_id: str) -> str:
        """Return the current activity kind without exposing registry tokens."""
        key = str(conversation_id or "").strip()
        if not key:
            return ""
        with self._activity_lock:
            token = self._lifecycle_operations.get(key)
            if token:
                return token.split(":", 1)[0]
            if key in self._active_turns:
                return "turn"
        return ""

    # ------------------------------------------------------------------
    # Conversation CRUD
    # ------------------------------------------------------------------

    def create(self, title: str | None = None) -> Conversation:
        conv = Conversation(data_dir=str(self._repository.data_dir))
        if title:
            conv.title = title
        return conv

    def load(self, conversation_id: str) -> Optional[Conversation]:
        conversation = self._repository.load(conversation_id)
        if conversation is not None:
            conversation.data_dir = str(self._repository.data_dir)
            self.reconcile_artifacts(conversation)
        return conversation

    def exists(self, conversation_id: str) -> bool:
        checker = getattr(self._repository, "exists", None)
        if callable(checker):
            return bool(checker(conversation_id))
        return self._repository.load(conversation_id) is not None

    def export(self, conversation: Conversation | str, destination: str | Path,
               *, format: str | None = None) -> Path:
        """Export a supplied snapshot or a stored conversation through one format owner."""
        if isinstance(conversation, str):
            conversation = self.load(conversation)
        if conversation is None:
            raise ValueError("Conversation not found")
        target = Path(destination).expanduser()
        selected = document_format(target, format)
        if selected == "json":
            atomic_write_text(target, json.dumps(conversation.to_dict(), ensure_ascii=False, indent=2))
            return target
        lines = [f"# {conversation.title or 'Conversation'}\n"]
        lines.extend(f"## {message.role.upper()}\n\n{message.content or ''}\n"
                     for message in conversation.messages if message.role != "system")
        return export_document("\n".join(lines), target, format=selected, title=conversation.title or "Conversation")

    def list_all(self) -> List[Dict[str, Any]]:
        return self._repository.list_all()

    def reconcile_index(self) -> List[Dict[str, Any]]:
        """One startup reconciliation; ordinary views keep using the small index."""
        with self._activity_lock:
            return self._repository.reconcile_index()

    def cancel_delegation(self, conversation_id: str) -> Conversation:
        """Persist cancellation without replacing a live run's transcript."""
        with self._activity_lock:
            conversation = self.load(conversation_id)
            if conversation is None or conversation.delegation is None:
                raise ValueError('独立任务不存在。')
            if conversation.delegation.submission == 'settled':
                return conversation
            conversation.delegation.cancel_requested = True
            if not self._repository.save(conversation):
                raise OSError('无法保存任务停止请求。')
            return conversation

    def update_navigation(
        self, conversation_id: str, *, title: str | None = None,
        pinned: bool | None = None, archived: bool | None = None,
    ) -> Conversation:
        """Persist navigation metadata against the latest, idle snapshot.

        Archiving only changes discoverability. It never deletes content,
        changes the workspace or terminates background processes.
        """
        token = self.begin_lifecycle(conversation_id, "navigation")
        if token is None:
            raise RuntimeError("会话正在运行或维护，请结束后再修改。")
        try:
            conversation = self.load(conversation_id)
            if conversation is None:
                raise ValueError("会话不存在。")
            if title is not None:
                if not title.strip():
                    raise ValueError("对话名称不能为空。")
                self.set_title(conversation, title)
            if pinned is not None:
                conversation.pinned = bool(pinned)
            if archived is not None:
                conversation.archived = bool(archived)
            if not self.save(conversation, activity_token=token):
                raise OSError("会话更改未保存，请重试。")
            return conversation
        finally:
            self.end_lifecycle(conversation_id, token)

    def save(self, conversation: Conversation, *, activity_token: str | None = None) -> bool:
        conversation.data_dir = str(self._repository.data_dir)
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        with self._activity_lock:
            owner = self._lifecycle_operations.get(conversation_id)
            if owner is not None and owner != activity_token:
                logger.debug("Rejected save for conversation %s during lifecycle operation", conversation_id)
                return False
            # A view or a late run cannot erase the authoritative task handoff.
            latest = self._repository.load(conversation_id)
            if latest is not None and latest.delegation is not None:
                current = latest.delegation
                incoming = conversation.delegation
                owns = activity_token and activity_token in {
                    self._active_turns.get(conversation_id), owner}
                if incoming is None or (incoming.to_dict() != current.to_dict() and not owns):
                    return False
                if incoming.run_id != current.run_id and not (owner and owner == activity_token):
                    return False
                if incoming.run_id == current.run_id:
                    phases = {'queued': 0, 'started': 1, 'settled': 2}
                    if current.cancel_requested and not incoming.cancel_requested:
                        return False
                    if phases[incoming.submission] < phases[current.submission]:
                        return False
            self.reconcile_artifacts(conversation)
            return self._repository.save(conversation)

    @staticmethod
    def revision_fingerprint(conversation: Conversation) -> str:
        """Hash only facts that affect a turn restart result."""
        payload = {
            "id": str(conversation.id or ""),
            "work_dir": str(conversation.work_dir or ""),
            "messages": [message.to_dict() for message in conversation.messages],
            "seq_counter": conversation.current_seq_id(),
            "state": conversation.get_state().to_dict(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()

    @classmethod
    def view_revision(cls, conversation: Conversation) -> str:
        """CAS for client edits, including selection and normalized settings.

        Transcript restart identity remains independent of the selected model;
        clients additionally protect the configuration they actually displayed.
        """
        settings = {'tool_approval': 'default', 'filesystem_mode': 'confined', **conversation.settings}
        payload = [cls.revision_fingerprint(conversation), conversation.provider_id, conversation.provider_name,
                   conversation.model, conversation.mode, settings, conversation.get_llm_config().to_dict()]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def delete(self, conversation_id: str) -> bool:
        key = str(conversation_id or "").strip()
        token = self.begin_lifecycle(key, "delete")
        if token is None:
            return False
        try:
            return self._delete(key)
        finally:
            self.end_lifecycle(key, token)

    def _delete(self, conversation_id: str) -> bool:
        conversation = None
        try:
            conversation = self._repository.load(conversation_id)
            if conversation is None:
                return False
            work_dir = str(getattr(conversation, "work_dir", "") or "").strip()
            session_root = resolve_session_root(work_dir, conversation_id, data_dir=getattr(conversation, "data_dir", None))
        except Exception as exc:
            logger.warning("Failed to resolve conversation %s for deletion: %s", conversation_id, exc)
            return False

        staged_root: Path | None = None
        repository_deleted = False
        try:
            if session_root.exists():
                staged_root = session_root.with_name(
                    f".{session_root.name}.deleting-{uuid.uuid4().hex[:8]}"
                )
                os.replace(session_root, staged_root)

            repository_deleted = bool(self._repository.delete(conversation_id))
            if not repository_deleted:
                raise OSError("conversation repository delete failed")
            self._cancel_pending_memory(conversation)

            if staged_root is not None:
                shutil.rmtree(staged_root)
            return True
        except Exception as exc:
            logger.warning("Failed to delete conversation %s: %s", conversation_id, exc)
            if staged_root is not None and staged_root.exists() and not session_root.exists():
                try:
                    os.replace(staged_root, session_root)
                except Exception as restore_exc:
                    logger.warning(
                        "Failed to restore session root for %s: %s",
                        conversation_id,
                        restore_exc,
                    )
            if repository_deleted and self._repository.load(conversation_id) is None:
                try:
                    self._repository.save(conversation)
                except Exception as restore_exc:
                    logger.warning(
                        "Failed to restore conversation %s after delete rollback: %s",
                        conversation_id,
                        restore_exc,
                    )
            return False

    def import_from_file(self, file_path: str) -> Optional[Conversation]:
        try:


            with open(file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            conversation = parse_imported_data(data)
            if conversation:
                conversation.id = str(uuid.uuid4())
                conversation.delegation = None
                if self.save(conversation):
                    return conversation
                logger.warning("Could not save imported conversation from %s", file_path)
        except Exception as exc:
            logger.warning("Error importing conversation from %s: %s", file_path, exc)
        return None

    def update_tasks(self, conversation_id: str, operations: list[dict], *, expected_revision: str) -> Conversation:
        token = self.begin_lifecycle(conversation_id, 'tasks')
        if not token:
            raise ConversationBusyError('Session is busy.')
        try:
            conversation = self.load(conversation_id)
            if conversation is None or self.view_revision(conversation) != expected_revision:
                raise InvalidRequestError('Session changed; reload before editing tasks.')
            if not operations or len(operations) > 100:
                raise InvalidRequestError('Provide between 1 and 100 task operations.')
            sequence = conversation.next_seq_id()
            state = conversation.get_state()
            feedback = TodoService.handle_ops(state, operations, sequence)
            if any(item.startswith('Rejected') for item in feedback):
                raise InvalidRequestError('\n'.join(feedback))
            state.last_updated_seq = sequence
            state.state_version += 1
            conversation.set_state(state)
            if not self.save(conversation, activity_token=token):
                raise PersistenceError('Could not save tasks.')
            return conversation
        finally:
            self.end_lifecycle(conversation_id, token)

    def trace(self, conversation_id: str, *, run_id: str = "", cursor: int | None = 0, limit: int = 200) -> dict:
        """Read captured events, not current state; page with the returned byte cursor."""
        conversation = self.load(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        root = resolve_debug_trace_dir(conversation_id=conversation.id, work_dir=conversation.work_dir,
                                       data_dir=conversation.data_dir)
        return read_trace_events(root / "events.jsonl", request_ids=(run_id,) if run_id else (), cursor=cursor, limit=limit)

    def trace_node(self, conversation_id: str, node_id: str, *, run_id: str,
                   events: list[dict] | None = None) -> dict:
        """Read one node from the bounded tail or an already fetched trace page.

        Pass a page's events when inspecting older pages or several nodes, to
        avoid rescanning the log. References remain scoped to this session.
        """
        conversation = self.load(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        root = resolve_debug_trace_dir(conversation_id=conversation.id, work_dir=conversation.work_dir,
                                       data_dir=conversation.data_dir)
        note = ""
        if events is None:
            page = read_trace_events(root / "events.jsonl", request_ids=(run_id,), cursor=None)
            events, note = page["events"], page["note"]
        selected = [e for e in events[-MAX_EVENTS:] if e.get("node_id") == node_id and e.get("request_id", "") == run_id]
        archive = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=conversation.data_dir)

        def read_archive(part, content_id):
            record = archive.read_record(content_id)
            ref = (record.metadata.get("input_ref") if part == "input" else record.original_ref) if record else ""
            if not ref:
                return {"status": "missing", "content": None, "note": "Archive 内容不存在。"}
            return read_trace_payload(archive.session_root, str(archive.resolve_ref(ref)), as_json=part == "input")

        result = read_trace_node(root, selected, archive_reader=read_archive)
        result["note"] = note if selected else (note or "当前读取范围没有该节点；可传入 trace() 对应页的 events。")
        return result

    @staticmethod
    def reconcile_artifacts(conversation: Conversation) -> None:
        try:
            state = conversation.get_state()
            changed = ArtifactService.reconcile_artifact_files(
                state,
                work_dir=str(getattr(conversation, "work_dir", "") or "").strip(),
                conversation_id=getattr(conversation, "id", None),
                data_dir=getattr(conversation, "data_dir", None),
            )
            if changed:
                conversation.set_state(state)
        except Exception as exc:
            logger.debug("Failed to reconcile artifact files: %s", exc)

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def commit_message(self, conversation: Conversation, message: Message, *, activity_token=None) -> None:
        """Commit a prepared input; restore the caller's snapshot on failure."""
        before = conversation.clone()
        conversation.add_message(message)
        if message.role == "user":
            conversation.archived = False
        self.ensure_title(conversation)
        if not self.save(conversation, activity_token=activity_token):
            conversation.__dict__.update(before.__dict__)
            raise PersistenceError("Could not save input; execution was not started.")

    def add_message(
        self, conversation: Conversation, message: Message, *, auto_save: bool = False,
    ) -> None:
        conversation.add_message(message)
        if auto_save:
            self.save(conversation)

    def replace_user_turn(
        self,
        conversation_id: str,
        target_message_id: str,
        *,
        content: str,
        content_refs: list | None = None,
        images: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_fingerprint: str,
        user_message_id: str = "",
        activity_token: str | None = None,
        allow_external: bool = False,
    ) -> ConversationRevisionResult:
        """Replace one user turn and restart from the replacement.

        ``allow_external`` is reserved for an explicitly bound Channel
        conversation.  Desktop callers keep the safer local-only default.
        """

        if not str(content or "").strip() and not content_refs and not images:
            return ConversationRevisionResult(ok=False, error="消息正文和附件不能同时为空")
        replacement = Message(
            id=str(user_message_id or uuid.uuid4()),
            role="user",
            content=str(content or ""),
            content_refs=deepcopy(list(content_refs or [])),
            images=list(images or []),
            metadata=deepcopy(dict(metadata or {})),
        )
        return self._apply_turn_change(
            conversation_id,
            target_message_id,
            expected_fingerprint=expected_fingerprint,
            replacement=replacement,
            activity_token=activity_token,
            change="replace",
            allow_external=allow_external,
        )

    def regenerate_user_turn(
        self,
        conversation_id: str,
        target_message_id: str,
        *,
        expected_fingerprint: str,
        activity_token: str | None = None,
        allow_external: bool = False,
    ) -> ConversationRevisionResult:
        """Keep one real user turn and discard every message after it."""

        return self._apply_turn_change(
            conversation_id,
            target_message_id,
            expected_fingerprint=expected_fingerprint,
            replacement=None,
            activity_token=activity_token,
            change="regenerate",
            allow_external=allow_external,
        )

    def remove_turn(
        self,
        conversation_id: str,
        target_message_id: str,
        *,
        expected_fingerprint: str,
        activity_token: str | None = None,
        allow_external: bool = False,
    ) -> ConversationRevisionResult:
        """Remove the selected user-led turn and every later message.

        The target may be either the user message or its assistant response;
        the whole turn is removed so tool-call relationships cannot be left
        dangling.  This only changes the PyCat transcript.  Workspace files,
        processes, network requests and Channel-side messages are not undone.
        """

        return self._apply_turn_change(
            conversation_id,
            target_message_id,
            expected_fingerprint=expected_fingerprint,
            replacement=None,
            activity_token=activity_token,
            change="delete",
            allow_external=allow_external,
        )

    def _apply_turn_change(
        self,
        conversation_id: str,
        target_message_id: str,
        *,
        expected_fingerprint: str,
        replacement: Message | None,
        activity_token: str | None,
        change: str,
        allow_external: bool,
    ) -> ConversationRevisionResult:
        """Apply the single atomic transaction shared by edit/retry/delete."""

        key = str(conversation_id or "").strip()
        target_id = str(target_message_id or "").strip()
        fingerprint = str(expected_fingerprint or "").strip()
        if not key or not target_id or not fingerprint:
            return ConversationRevisionResult(ok=False, error="修订请求不完整")

        owned_token = activity_token
        release_token = False
        if owned_token is None:
            owned_token = self.begin_lifecycle(key, "revision")
            release_token = True
        else:
            with self._activity_lock:
                if self._lifecycle_operations.get(key) != owned_token:
                    owned_token = None
        if owned_token is None:
            return ConversationRevisionResult(ok=False, error="该会话正在运行或处理，暂不能修订")

        try:
            authoritative = self.load(key)
            if authoritative is None:
                return ConversationRevisionResult(ok=False, error="会话不存在")
            # The caller may request external revision only for a persisted
            # Channel binding.  Keep this boundary in Core as well as GUI.
            allow_external = bool(
                allow_external and is_bound_channel_conversation(authoritative)
            )
            if self.revision_fingerprint(authoritative) != fingerprint:
                return ConversationRevisionResult(
                    ok=False,
                    error="会话已发生变化，请重新发起修订",
                )

            target_index = next(
                (
                    index
                    for index, message in enumerate(authoritative.messages)
                    if str(getattr(message, "id", "") or "") == target_id
                ),
                -1,
            )
            if target_index < 0:
                return ConversationRevisionResult(ok=False, error="要修订的消息不存在")
            selected = authoritative.messages[target_index]
            if change == "delete" and selected.role == "assistant":
                target = restartable_user_for_assistant(
                    authoritative.messages,
                    target_id,
                    allow_external=allow_external,
                )
            elif selected.role == "user":
                target = restartable_user_by_id(
                    authoritative.messages,
                    target_id,
                    allow_external=allow_external,
                )
            else:
                target = None
            if target is None:
                return ConversationRevisionResult(
                    ok=False,
                    error="只能修订本地真实用户消息，且不能越过后续外部输入",
                )
            target_index = next(
                (
                    index
                    for index, message in enumerate(authoritative.messages)
                    if str(getattr(message, "id", "") or "")
                    == str(getattr(target, "id", "") or "")
                ),
                -1,
            )
            if target_index < 0:
                return ConversationRevisionResult(ok=False, error="用户轮次不存在")
            revised = deepcopy(authoritative)
            target_seq = int(getattr(target, "seq_id", 0) or 0)
            if target_seq <= 0:
                return ConversationRevisionResult(ok=False, error="目标消息缺少有效序号")

            if change == "delete":
                revised.messages = revised.messages[:target_index]
                state_update_seq = revised.current_seq_id()
            elif replacement is None:
                revised.messages = revised.messages[: target_index + 1]
                state_update_seq = revised.current_seq_id()
            else:
                replacement.metadata = {
                    **deepcopy(dict(getattr(target, "metadata", {}) or {})),
                    **deepcopy(dict(getattr(replacement, "metadata", {}) or {})),
                }
                revised.messages = revised.messages[:target_index]
                revised.add_message(replacement)
                state_update_seq = int(replacement.seq_id or 0)

            reconcile_after_turn_restart(
                revised,
                target_user_seq=target_seq,
                state_update_seq=state_update_seq,
            )

            if replacement is not None:
                first_real_user = next(
                    (
                        message
                        for message in authoritative.messages
                        if is_real_user_message(message)
                    ),
                    None,
                )
                old_title = self._generated_title_for_message(target)
                if first_real_user is target and authoritative.title == old_title:
                    revised.title = self._generated_title_for_message(replacement)
            elif change == "delete":
                first_real_user = next(
                    (
                        message
                        for message in authoritative.messages
                        if is_real_user_message(message)
                    ),
                    None,
                )
                old_title = self._generated_title_for_message(target)
                if first_real_user is target and authoritative.title == old_title:
                    revised.title = "New Chat"
            revised.updated_at = datetime.now()

            revised.settings["memory_generation"] = int(revised.settings.get("memory_generation", 0)) + 1
            if not self.save(revised, activity_token=owned_token):
                return ConversationRevisionResult(ok=False, error="保存修订后的会话失败")
            self._cancel_pending_memory(authoritative)
            return ConversationRevisionResult(
                ok=True,
                conversation=revised,
            )
        except Exception as exc:
            logger.warning("Failed to revise conversation %s: %s", key, exc)
            return ConversationRevisionResult(ok=False, error=f"会话修订失败：{exc}")
        finally:
            if release_token:
                self.end_lifecycle(key, owned_token)

    @staticmethod
    def _generated_title_for_message(message: Message) -> str:
        probe = Conversation(title="New Chat", messages=[deepcopy(message)])
        probe.generate_title_from_first_message()
        return str(probe.title or "New Chat")

    def ensure_title(self, conversation: Conversation) -> None:
        """Auto-generate a title from the first message if needed."""
        if len(conversation.messages) == 1:
            conversation.generate_title_from_first_message()

    # ------------------------------------------------------------------
    # Provider helpers
    # ------------------------------------------------------------------

    @staticmethod
    def find_provider(providers: list, provider_id: str):
        for p in providers:
            if p.id == provider_id:
                return p
        return None

    @staticmethod
    def resolve_provider(providers: list, provider_id: str = "", provider_name: str = ""):
        normalized_id = str(provider_id or "").strip()
        normalized_name = str(provider_name or "").strip()

        if normalized_id:
            provider = ConversationService.find_provider(providers, normalized_id)
            if provider is not None:
                return provider

        if normalized_name:
            for provider in providers:
                if provider_matches_name(provider, normalized_name):
                    return provider
        return None

    def configure_llm(
        self,
        conversation: Conversation,
        *,
        providers: list | None = None,
        provider_id: str | None = None,
        provider_name: str | None = None,
        api_type: str | None = None,
        model: str | None = None,
    ) -> Conversation:
        provider_list = list(providers or [])
        llm_config = conversation.get_llm_config()

        next_provider_id = llm_config.provider_id
        if provider_id is not None:
            next_provider_id = str(provider_id or "").strip()

        next_provider_name = llm_config.provider_name
        if provider_name is not None:
            next_provider_name = str(provider_name or "").strip()

        next_api_type = llm_config.api_type
        if api_type is not None:
            next_api_type = str(api_type or "").strip().lower()

        if provider_list:
            resolved = self.resolve_provider(
                provider_list,
                provider_id=next_provider_id,
                provider_name=next_provider_name,
            )
            if resolved is not None:
                next_provider_id = str(getattr(resolved, "id", "") or next_provider_id).strip()
                next_provider_name = str(getattr(resolved, "name", "") or next_provider_name).strip()
                next_api_type = str(getattr(resolved, "api_type", "") or next_api_type).strip().lower()

        next_model = llm_config.model
        if model is not None:
            next_model = str(model or "").strip()

        conversation.set_llm_config(
            llm_config.with_updates(
                provider_id=next_provider_id,
                provider_name=next_provider_name,
                api_type=next_api_type,
                model=next_model,
            )
        )
        return conversation

    def set_title(self, conversation: Conversation, title: str) -> Conversation:
        next_title = str(title or "").strip()
        if next_title:
            conversation.title = next_title
            conversation.updated_at = datetime.now()
        return conversation

    def set_mode(self, conversation: Conversation, mode_slug: str) -> Conversation:
        conversation.mode = str(mode_slug or "chat").strip() or "chat"
        conversation.updated_at = datetime.now()
        return conversation

    def change_work_dir(
        self,
        conversation: Conversation,
        new_work_dir: str,
        *,
        active_processes: Any = None,
    ) -> WorkDirChangeResult:
        key = str(getattr(conversation, "id", "") or "").strip()
        token = self.begin_lifecycle(key, "workspace")
        if token is None:
            return WorkDirChangeResult(
                ok=False,
                error="该会话正在运行或处理，暂不能切换 workspace",
                old_work_dir=str(getattr(conversation, "work_dir", "") or ""),
                new_work_dir=str(new_work_dir or ""),
            )
        try:
            return self._change_work_dir(
                conversation,
                new_work_dir,
                active_processes=active_processes,
                _activity_token=token,
            )
        finally:
            self.end_lifecycle(key, token)

    def _change_work_dir(
        self,
        conversation: Conversation,
        new_work_dir: str,
        *,
        active_processes: Any = None,
        _activity_token: str | None = None,
    ) -> WorkDirChangeResult:
        """Switch the workspace of one conversation, migrating its session root.

        This is the only supported workspace-switch path. The transaction:

        1. normalize old/new workspace; identical resolved paths only update display;
        2. refuse while the conversation owns live shell processes;
        3. refuse when the target session root already exists; a missing source
           root degrades to a plain field update;
        4. move via a temporary sibling (same-volume rename or validated
           cross-volume copy-then-rename);
        5. persist; on save failure move the directory back and restore the field.
        """
        old_raw = normalize_work_dir(getattr(conversation, "work_dir", ""))
        new_raw = normalize_work_dir(new_work_dir)

        def _normalize(value: str) -> str:
            if not value:
                return ""
            if value.startswith("ssh://"):
                return normalize_work_dir(value)
            try:
                return str(Path(value).expanduser().resolve())
            except Exception:
                return value

        old_norm, new_norm = _normalize(old_raw), _normalize(new_raw)
        result_base = {"old_work_dir": old_raw, "new_work_dir": new_raw}
        if old_norm == new_norm:
            changed = old_raw != new_raw
            if changed:
                snapshot = deepcopy(conversation)
                conversation.work_dir = new_raw
                conversation.updated_at = datetime.now()
                if not self.save(conversation, activity_token=_activity_token):
                    self._restore_conversation(conversation, snapshot)
                    return WorkDirChangeResult(
                        ok=False,
                        error="保存会话失败，已恢复原 workspace",
                        **result_base,
                    )
            return WorkDirChangeResult(ok=True, changed=changed, **result_base)

        blocked = self._count_active_processes(active_processes, conversation)
        if blocked:
            return WorkDirChangeResult(
                ok=False,
                blocked_processes=blocked,
                error=f"会话仍有 {blocked} 个未结束的 Shell 进程，请先在 Inspector 中停止后再切换 workspace。",
                **result_base,
            )

        old_root = resolve_session_root(old_norm, str(conversation.id), data_dir=getattr(conversation, "data_dir", None))
        new_root = resolve_session_root(new_norm, str(conversation.id), data_dir=getattr(conversation, "data_dir", None))
        source_exists = old_root.is_dir()
        if new_root.exists():
            return WorkDirChangeResult(
                ok=False,
                error=f"目标会话目录已存在，拒绝覆盖：{new_root}",
                **result_base,
            )

        snapshot = deepcopy(conversation)
        moved = False
        source_backup: Path | None = None
        if source_exists:
            try:
                source_backup = self._move_session_root(old_root, new_root)
                moved = True
            except Exception as exc:
                logger.warning("Failed to migrate session root %s -> %s: %s", old_root, new_root, exc)
                return WorkDirChangeResult(
                    ok=False,
                    error=f"迁移会话目录失败：{exc}",
                    **result_base,
                )

        try:
            if moved:
                self._rewrite_session_refs(conversation, old_root, new_root, new_work_dir=new_raw)
            conversation.work_dir = new_raw
            conversation.settings["memory_generation"] = int(conversation.settings.get("memory_generation", 0)) + 1
            conversation.updated_at = datetime.now()
            saved = self.save(conversation, activity_token=_activity_token)
        except Exception as exc:
            logger.warning("Failed to persist workspace migration for %s: %s", conversation.id, exc)
            saved = False
        if not saved:
            rollback_error = ""
            if moved:
                try:
                    if source_backup is not None:
                        os.rename(source_backup, old_root)
                        shutil.rmtree(new_root)
                    else:
                        self._move_session_root(new_root, old_root)
                except Exception as exc:
                    rollback_error = f"；目录回滚也失败（{exc}），请手动核对 {old_root} 与 {new_root}"
            self._restore_conversation(conversation, snapshot)
            return WorkDirChangeResult(
                ok=False,
                moved=bool(moved and rollback_error),
                error=f"保存会话失败，已恢复原 workspace{rollback_error}",
                **result_base,
            )
        self._cancel_pending_memory(snapshot)
        if source_backup is not None:
            try:
                shutil.rmtree(source_backup)
            except Exception as exc:
                logger.warning("Failed to clean migrated session backup %s: %s", source_backup, exc)
        return WorkDirChangeResult(ok=True, changed=True, moved=moved, **result_base)

    @staticmethod
    def _count_active_processes(active_processes: Any, conversation: Conversation) -> int:
        if active_processes is None:
            return 0
        value = active_processes(getattr(conversation, "id", "")) if callable(active_processes) else active_processes
        if value is None:
            return 0
        if isinstance(value, int):
            return max(0, value)
        try:
            return len(value)
        except TypeError:
            return 1 if value else 0

    @staticmethod
    def _move_session_root(source: Path, target: Path) -> Path | None:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.migrating-{uuid.uuid4().hex[:8]}"
        if ConversationService._same_volume(source, target.parent):
            os.rename(source, staging)
            try:
                os.rename(staging, target)
            except Exception:
                os.rename(staging, source)
                raise
            return None
        source_backup = source.parent / f".{source.name}.migrating-{uuid.uuid4().hex[:8]}"
        try:
            shutil.copytree(source, staging)
            ConversationService._validate_copy(source, staging)
            os.rename(staging, target)
            try:
                os.rename(source, source_backup)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                raise
            return source_backup
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _same_volume(source: Path, target_parent: Path) -> bool:
        if os.name == "nt":
            return os.path.splitdrive(str(source))[0].lower() == os.path.splitdrive(str(target_parent.resolve()))[0].lower()
        try:
            return os.stat(source).st_dev == os.stat(target_parent).st_dev
        except OSError:
            return False

    @staticmethod
    def _validate_copy(source: Path, staging: Path) -> None:
        def _stats(root: Path) -> tuple[int, int]:
            files = [path for path in root.rglob("*") if path.is_file()]
            return len(files), sum(path.stat().st_size for path in files)

        source_stats, staging_stats = _stats(source), _stats(staging)
        if source_stats != staging_stats:
            shutil.rmtree(staging, ignore_errors=True)
            raise IOError(f"copy validation failed: {source_stats} != {staging_stats}")

    @staticmethod
    def _rewrite_session_refs(conversation: Conversation, old_root: Path, new_root: Path, *, new_work_dir: str) -> None:
        """Rewrite absolute references that point at the old session root.

        Session-internal references are relative by contract, so this only
        repairs legacy absolute references; external file references stay
        untouched because they never contain the old session root prefix.
        """
        replacements = {
            old_root.as_posix(): new_root.as_posix(),
            str(old_root): str(new_root),
        }

        def _walk(value: Any) -> Any:
            if isinstance(value, str):
                for old, new in replacements.items():
                    if old in value:
                        value = value.replace(old, new)
                return value
            if isinstance(value, dict):
                result = {key: _walk(item) for key, item in value.items()}
                if value.get("kind") in {"archive", "artifact"} and value.get("conversation_id") == conversation.id:
                    result["workspace"] = new_work_dir
                return result
            if isinstance(value, list):
                return [_walk(item) for item in value]
            return value

        for message in conversation.messages or []:
            metadata = getattr(message, "metadata", None)
            if isinstance(metadata, dict):
                message.metadata = _walk(metadata)
            message.content_refs = [type(ref).from_dict(_walk(ref.to_dict())) for ref in message.content_refs]
            message.tool_calls = _walk(list(message.tool_calls or []))
        conversation.settings = _walk(dict(conversation.settings or {}))
        state = conversation.get_state()
        conversation.set_state(type(state).from_dict(_walk(state.to_dict())))

    @staticmethod
    def _restore_conversation(target: Conversation, snapshot: Conversation) -> None:
        target.__dict__.clear()
        target.__dict__.update(deepcopy(snapshot.__dict__))

    def set_setting(self, conversation: Conversation, key: str, value: Any) -> Conversation:
        settings = dict(conversation.settings or {})
        if value is None:
            settings.pop(str(key), None)
        else:
            settings[str(key)] = value
        conversation.settings = settings
        if key == "memory_enabled" and value is False:
            conversation.settings["memory_generation"] = int(conversation.settings.get("memory_generation", 0)) + 1
            self._cancel_pending_memory(conversation)
        conversation.updated_at = datetime.now()
        return conversation

    def set_settings(self, conversation: Conversation, updates: Dict[str, Any]) -> Conversation:
        settings = dict(conversation.settings or {})
        for key, value in dict(updates or {}).items():
            if value is None:
                settings.pop(str(key), None)
            else:
                settings[str(key)] = value
        conversation.settings = settings
        if updates.get("memory_enabled") is False:
            conversation.settings["memory_generation"] = int(conversation.settings.get("memory_generation", 0)) + 1
            self._cancel_pending_memory(conversation)
        conversation.updated_at = datetime.now()
        return conversation

    @staticmethod
    def _cancel_pending_memory(conversation: Conversation) -> None:
        MemoryEvolutionLedger(MemoryService.evolution_path_for(conversation.work_dir, data_dir=getattr(conversation, "data_dir", None))).cancel(conversation.id)

    def apply_settings_update(
        self,
        conversation: Conversation,
        update: ConversationSettingsUpdate,
        *,
        providers: list | None = None,
    ) -> Conversation:
        provider_list = list(providers or [])
        self.set_title(conversation, update.title)
        self.configure_llm(
            conversation,
            providers=provider_list,
            provider_id=update.provider_id,
            provider_name=update.provider_name,
            api_type=update.api_type,
            model=update.model,
        )

        llm_config_updates: dict[str, Any] = {
            "stream": update.stream,
        }
        if update.temperature is not None:
            llm_config_updates["temperature"] = update.temperature
        if update.top_p is not None:
            llm_config_updates["top_p"] = update.top_p
        if update.max_tokens is not None:
            llm_config_updates["max_tokens"] = update.max_tokens
        llm_config = conversation.get_llm_config().with_updates(**llm_config_updates)
        conversation.set_llm_config(llm_config)

        self.set_mode(conversation, update.mode_slug)
        settings_payload: dict[str, Any] = {
            "session_instructions": str(update.session_instructions or "").strip() or None,
            "max_context_messages": int(update.max_context_messages)
            if isinstance(update.max_context_messages, int) and int(update.max_context_messages) > 0
            else None,
            "max_turns": None,
            "show_thinking": bool(update.show_thinking),
            "primary_model_ref": str(update.primary_model_ref or "").strip() or None,
            "memory_enabled": bool(update.memory_enabled),
            "tool_selection": update.tool_selection.to_dict() if update.tool_selection is not None else None,
            "allowed_channel_sources": [
                str(item).strip() for item in (update.allowed_channel_sources or ()) if str(item).strip()
            ],
            "trusted_channel_sources": [
                str(item).strip() for item in (update.trusted_channel_sources or ()) if str(item).strip()
            ],
            "channel_notice_policy": str(update.channel_notice_policy or "notice").strip().lower() or "notice",
            "system_prompt": None,
            "custom_instructions": None,
            "system_prompt_override": None,
            "prompt_optimizer_model": None,
        }
        if update.pycat_assistant_enabled is not None:
            settings_payload["pycat_assistant_enabled"] = bool(update.pycat_assistant_enabled)
        self.set_settings(conversation, settings_payload)
        return conversation

    def build_model_ref(
        self,
        conversation: Conversation,
        providers: list | None = None,
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> str:
        llm_config = conversation.get_llm_config()
        next_provider_id = str(provider_id if provider_id is not None else (llm_config.provider_id or conversation.provider_id or "")).strip()
        next_provider_name = str(provider_name if provider_name is not None else (llm_config.provider_name or conversation.provider_name or "")).strip()
        next_model = str(model if model is not None else (llm_config.model or conversation.model or "")).strip()

        provider_list = list(providers or [])
        resolved = None
        if provider_list:
            resolved = self.resolve_provider(
                provider_list,
                provider_id=next_provider_id,
                provider_name=next_provider_name,
            )
            if resolved is not None:
                next_provider_name = str(getattr(resolved, "name", "") or next_provider_name).strip()

        return build_model_ref(next_provider_name, next_model or llm_config.resolved_model())
