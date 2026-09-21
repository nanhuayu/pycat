"""Durable-memory facade: conversation wiring for the two-file MemoryStore.

The store files live outside session state, so memory survives conversation
restarts and rollbacks. ``build_run_snapshot`` renders a frozen injection
block once per run; tool writes land on disk immediately and take effect on
the next run.
"""

from __future__ import annotations

import json
import os
import re
from pycat.models.session_paths import resolve_project_data_root
from pathlib import Path
from typing import Any, Mapping, Sequence

from pycat.core.memory.store import (
    MemoryOperation,
    MemorySnapshot,
    MemoryStore,
    TARGET_MEMORY,
    VALID_TARGETS,
)
from pycat.models.conversation import Conversation
from pycat.models.session_paths import normalize_work_dir
from pycat.core.memory.evolution import MemoryEvolutionLedger
from pycat.core.llm.token_budget import estimate_tokens
from pycat.core.persistence import exclusive_file_lock
from pycat.core.config.io import load_settings_dict
from pycat.models.contracts.config import AppConfig

GLOBAL_MEMORY_DIR = Path.home() / ".pycat" / "memory"


def memory_enabled(conversation: Conversation | Mapping | None) -> bool:
    if isinstance(conversation, Mapping):
        nested = conversation.get("settings")
        settings = nested if isinstance(nested, Mapping) else conversation
    else:
        settings = getattr(conversation, "settings", None)
    if not isinstance(settings, Mapping):
        return True
    return bool(settings.get("memory_enabled", True))


def _memory_root(data_dir):
    return Path(data_dir) / "memory" if data_dir else GLOBAL_MEMORY_DIR


class MemoryService:
    """Stateless helpers shared by the runtime, tools, and inspector."""

    @staticmethod
    def legacy_project_memory(*, data_dir: str | Path | None = None) -> dict:
        store = MemoryStore(_memory_root(data_dir) / "MEMORY.md", _memory_root(data_dir) / "USER.md")
        return store.describe("memory")

    @staticmethod
    def assign_legacy_project_memory(work_dir: str, *, data_dir: str | Path | None = None) -> tuple[bool, str]:
        if not normalize_work_dir(work_dir):
            return False, "请先选择接收旧项目记忆的工作区。"
        legacy = _memory_root(data_dir) / "MEMORY.md"
        try:
            with exclusive_file_lock(legacy):
                source = MemoryService.legacy_project_memory(data_dir=data_dir)
                if not source["readable"]:
                    return False, source["error"]
                if not source["records"]:
                    return False, "没有待分配的旧项目记忆。"
                ok, note = MemoryService.store_for(work_dir, data_dir=data_dir).apply_batch("memory", [
                    {"op": "add", "content": row["text"], "origin": "legacy", "sources": row["sources"]}
                    for row in source["records"]], operation_id="legacy-" + source["digest"])
                if not ok:
                    return ok, note
                os.replace(legacy, legacy.with_name(f"MEMORY.assigned-{source['digest'][:16]}.md"))
                return True, "旧项目记忆已分配到当前工作区，原文已保留。"
        except OSError as exc:
            return False, str(exc)

    @staticmethod
    def store_for(work_dir: str | Path | None, *, data_dir: str | Path | None = None) -> MemoryStore:
        """Build the store for a conversation workspace.

        ``memory`` targets ``<work_dir>/.pycat/memory/MEMORY.md``; without a
        workspace project memory is unavailable. Legacy global MEMORY.md is
        preserved for explicit assignment. ``user`` always targets USER.md.
        """
        work_dir_text = normalize_work_dir(work_dir)
        if work_dir_text:
            memory_path = resolve_project_data_root(work_dir_text, data_dir=data_dir) / "memory" / "MEMORY.md"
        else:
            memory_path = _memory_root(data_dir) / "MEMORY.md"
        config = AppConfig.from_dict(load_settings_dict(data_dir=_memory_root(data_dir).parent))
        return MemoryStore(memory_path=memory_path, user_path=_memory_root(data_dir) / "USER.md",
                           memory_char_limit=config.memory_char_limit, user_char_limit=config.user_memory_char_limit,
                           project_enabled=bool(work_dir_text))

    @staticmethod
    def build_run_snapshot(conversation: Conversation | Mapping | None, *, token_limit: int = 2048, data_dir: str | Path | None = None) -> str:
        """Frozen memory section injected once per run ('' when disabled/empty)."""
        if not memory_enabled(conversation):
            return ""
        if isinstance(conversation, Mapping):
            work_dir = conversation.get("work_dir")
        else:
            work_dir = getattr(conversation, "work_dir", None)
        data_dir = data_dir or (conversation.get("data_dir") if isinstance(conversation, Mapping) else getattr(conversation, "data_dir", None))
        store = MemoryService.store_for(work_dir, data_dir=data_dir)
        snapshot = store.safe_snapshot()
        messages = conversation.get("messages", []) if isinstance(conversation, Mapping) else getattr(conversation, "messages", [])
        prompt = next((str(m.get("content", "") if isinstance(m, Mapping) else m.content)
                       for m in reversed(messages) if (m.get("role") if isinstance(m, Mapping) else m.role) == "user"), "")
        terms = set(re.findall(r"[\w]+", prompt.casefold()))
        terms.update(prompt[i:i + 2] for i in range(len(prompt) - 1) if "\u4e00" <= prompt[i] <= "\u9fff")
        choices = [(target, text) for target, entries in (("user", snapshot.user), ("memory", snapshot.memory)) for text in entries]
        choices.sort(key=lambda item: -sum(term in item[1].casefold() for term in terms))
        selected = {"user": [], "memory": []}
        rendered = ""
        for target, entry in choices:
            trial = {key: list(values) for key, values in selected.items()}
            trial[target].append(entry)
            text = store.render(MemorySnapshot(tuple(trial["memory"]), tuple(trial["user"])))
            if estimate_tokens(text) <= max(0, min(2048, token_limit)):
                selected, rendered = trial, text
        return rendered

    @staticmethod
    def evolution_path_for(work_dir: str | Path | None, *, data_dir: str | Path | None = None) -> Path:
        """Return the workspace-owned durable self-evolution ledger path."""
        workspace = normalize_work_dir(work_dir)
        root = resolve_project_data_root(workspace, data_dir=data_dir) / "memory" if workspace else _memory_root(data_dir)
        return root / "evolution.sqlite3"

    @staticmethod
    def describe_evolution(work_dir: str | Path | None, *, data_dir: str | Path | None = None) -> dict[str, Any]:
        """Read-only Inspector projection; ledger state is not Conversation state."""
        return MemoryEvolutionLedger(MemoryService.evolution_path_for(work_dir, data_dir=data_dir)).snapshot()

    @staticmethod
    def handle_tool_action(
        work_dir: str | None,
        action: str,
        target: str = TARGET_MEMORY,
        content: str = "",
        old_text: str = "",
        new_text: str = "",
        operations: Sequence[Mapping[str, Any]] | None = None,
        conversation: Conversation | Mapping | None = None,
        entry_id: str = "",
        expected_digest: str | None = None,
    *, data_dir: str | Path | None = None) -> tuple[bool, str]:
        data_dir = data_dir or (conversation.get("data_dir") if isinstance(conversation, Mapping) else getattr(conversation, "data_dir", None))
        action = str(action or "").strip().lower()
        store = MemoryService.store_for(work_dir, data_dir=data_dir)
        target = str(target or TARGET_MEMORY).strip().lower()
        if target not in VALID_TARGETS:
            return False, f"unknown target: {target} (use {' or '.join(VALID_TARGETS)})."
        if target == TARGET_MEMORY and not normalize_work_dir(work_dir):
            return False, "project memory requires an active workspace; use target=user for user preferences."
        status = store.describe(target)
        if not status["readable"]:
            return False, f"memory storage cannot be read: {status['error']}"

        if action in {"add", "replace", "remove", "apply_batch"} and not memory_enabled(conversation):
            return False, "long-term memory is disabled for this conversation; no durable write was applied."

        if action == "read":
            records = status["records"]
            if entry_id:
                records = [entry for entry in records if entry["id"] == entry_id]
                if not records:
                    return False, "memory entry not found"
            return True, json.dumps({"target": target, "digest": status["digest"],
                "entries": records, "used_chars": status["used_chars"], "char_limit": status["char_limit"]}, ensure_ascii=False)

        if action in {"add", "replace", "remove"}:
            result = store.apply_batch(target, [{"op": action, "content": content, "old_text": old_text,
                                              "new_text": new_text, "entry_id": entry_id}], expected_digest=expected_digest)
            if result[0] and action == "remove":
                MemoryService._revoke_removed(store, target, status, work_dir, data_dir=data_dir)
            return result
        if action == "apply_batch":
            result = store.apply_batch(target, operations or [], expected_digest=expected_digest)
            if result[0]:
                MemoryService._revoke_removed(store, target, status, work_dir, data_dir=data_dir)
            return result
        return False, (
            "unknown action. Use read, add, replace, remove, or apply_batch."
        )

    @staticmethod
    def _revoke_removed(store, target, before, work_dir, *, data_dir: str | Path | None = None):
        remaining = {record["id"] for record in store.describe(target)["records"]}
        for record in before["records"]:
            if record["id"] not in remaining:
                for source in record["sources"]:
                    if source.get("conversation_id") and source.get("id"):
                        MemoryEvolutionLedger(MemoryService.evolution_path_for(source.get("workspace") or work_dir, data_dir=data_dir)).cancel(
                            source["conversation_id"], source_id=source["id"])

    @staticmethod
    def describe_targets(work_dir: str | None, *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
        """Entry listing for the inspector panel (read-only)."""
        store = MemoryService.store_for(work_dir, data_dir=data_dir)
        targets: list[dict[str, Any]] = []
        for target in VALID_TARGETS:
            if target != TARGET_MEMORY or store.project_enabled:
                targets.append(store.describe(target))
        return targets


__all__ = [
    "GLOBAL_MEMORY_DIR",
    "MemoryOperation",
    "MemoryService",
    "memory_enabled",
]
