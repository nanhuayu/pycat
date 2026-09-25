"""Application-owned curation worker. Wakeups carry no transcript or live client.

The runtime synchronously persists source identities only. One daemon processes
bounded requests; shutdown cancels its request and leaves durable claims for
lease recovery. No QWidget, alternate agent loop, or run-count scheduling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from itertools import islice
from pathlib import Path

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.memory.evolution import MemoryEvolutionLedger, failure_code
from pycat.core.memory.review import MemoryReviewService, source_fragments
from pycat.core.memory.service import MemoryService, memory_enabled
from pycat.models.contracts.tooling import ToolDescriptor
from pycat.models.session_paths import normalize_work_dir, resolve_session_root

logger = logging.getLogger(__name__)


def workspace_key(work_dir: str) -> str:
    value = normalize_work_dir(work_dir)
    if value.startswith("ssh://"):
        return value
    return os.path.normcase(str(Path(value).expanduser().resolve())) if value else ""


class CurationWorker:
    def __init__(self, review: MemoryReviewService | None, *, conversation_loader=None,
                 provider_resolver=None, settings_provider=None, on_change=None, data_dir: str | None = None) -> None:
        self.data_dir = data_dir
        self.review = review
        self._load = conversation_loader
        self._provider = provider_resolver
        self._settings = settings_provider or (lambda: {})
        self._changed = on_change or (lambda *_: None)
        self._workspaces: set[str] = set()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._reconcile_iterators = {}
        self._next_reconcile: dict[str, float] = {}

    @staticmethod
    def capture_metadata(conversation, policy) -> dict:
        settings = conversation.settings or {}
        if not memory_enabled(conversation) or settings.get("evaluation") or settings.get("parent_session_id"):
            return {}
        if str(policy.source) in {"capability", "sub_task", "evaluation"}:
            return {}
        permissions = {}
        for domain, tool, category in (("memory", "state__memory", "state"), ("wiki", "state__wiki", "state"),
                                       ("skill", "skill__manage", "skills")):
            descriptor = ToolDescriptor(tool, "", "", category)
            permissions[domain] = policy.tool_permissions.resolve(tool, category).action == "allow" and policy.tool_selection.allows(descriptor)
        if not normalize_work_dir(conversation.work_dir):
            permissions["wiki"] = False
        return {"enabled": True, "provider_id": str(conversation.provider_id or ""),
                "model": str(conversation.model or ""), "permissions": permissions,
                "generation": int(settings.get("memory_generation", 0))}

    def capture(self, conversation, record) -> int:
        if not memory_enabled(conversation) or (conversation.settings or {}).get("evaluation"):
            return 0
        return self._enqueue_record(workspace_key(conversation.work_dir), str(conversation.id), record)

    def _enqueue_record(self, work_dir: str, conversation_id: str, record) -> int:
        config = record.metadata.get("memory_capture") or {}
        if not config.get("enabled"):
            return 0
        ledger = MemoryEvolutionLedger(MemoryService.evolution_path_for(work_dir, data_dir=self.data_dir))
        if ledger.captured(conversation_id, record.id, record.digest):
            return 0
        store = SessionArchiveStore(work_dir, conversation_id, data_dir=self.data_dir)
        text = store.read_original(record)
        if not store.original_matches(record, text):
            raise ValueError("memory source archive changed")
        fragments = source_fragments(json.loads(text))
        sources = []
        # Empty turns still have an explainable receipt, without an LLM request.
        for locator, _text in fragments or [("empty", "")]:
            sources.append({"workspace": work_dir, "conversation_id": conversation_id,
                "id": record.id, "digest": record.digest, "locator": locator,
                "kind": "archive", "name": record.title, "terminal": record.metadata.get("terminal_status", ""),
                "user_message_id": record.metadata.get("user_message_id", ""),
                "provider_id": config.get("provider_id", ""), "model": config.get("model", ""),
                "generation": int(config.get("generation", 0)),
                "permissions": config.get("permissions", {})})
        added = ledger.enqueue_many(sources, capture=(conversation_id, record.id, record.digest))
        self.register(work_dir)
        if added:
            self._changed(work_dir, conversation_id, ("memory",))
            self._wake.set()
        return added

    def register(self, work_dir: str) -> None:
        with self._lock:
            self._workspaces.add(workspace_key(work_dir))

    def reconcile(self, work_dir: str, *, limit: int = 256) -> int:
        """Bounded startup compensation for source-published / enqueue-not-committed."""
        work_dir = workspace_key(work_dir)
        self.register(work_dir)
        root = resolve_session_root(work_dir, "_probe", data_dir=self.data_dir).parent
        count = scanned = 0
        if not root.exists():
            return 0
        iterator = self._reconcile_iterators.setdefault(work_dir, iter(root.glob("*/history/*/meta*.json")))
        batch = list(islice(iterator, max(1, limit)))
        if len(batch) < limit:
            self._reconcile_iterators.pop(work_dir, None)
            self._next_reconcile[work_dir] = time.monotonic() + 60
        for path in batch:
            scanned += 1
            try:
                session_id = path.parents[2].name
                store = SessionArchiveStore(work_dir, session_id, data_dir=self.data_dir)
                record = store.read_record(path.parent.name, kind="history")
                if record is None or record.source != "turn_capsule":
                    continue
                if path.name == "meta.pending.json":
                    store.write_record(record)
                if self._load is not None:
                    conversation = self._load(session_id)
                    if conversation is None or not memory_enabled(conversation):
                        continue
                    if int((record.metadata.get("memory_capture") or {}).get("generation", 0)) != int(conversation.settings.get("memory_generation", 0)):
                        continue
                count += self._enqueue_record(work_dir, session_id, record)
            except (OSError, ValueError) as exc:
                logger.warning("Memory source reconciliation failed: %s", exc)
        return count

    def _authorized(self, source: dict) -> bool:
        if self._stop.is_set():
            return False
        if self._load is None:
            return True
        conversation = self._load(source["conversation_id"])
        if conversation is None or not memory_enabled(conversation):
            return False
        if int(source.get("generation", 0)) != int(conversation.settings.get("memory_generation", 0)):
            return False
        if workspace_key(conversation.work_dir) != source["workspace"]:
            return False
        policy = RunPolicyBuilder.build(conversation=conversation, app_settings=self._settings())
        current = self.capture_metadata(conversation, policy).get("permissions", {})
        return all(not allowed or current.get(domain, False) for domain, allowed in source.get("permissions", {}).items())

    async def process_once(self, work_dir: str, *, now=None) -> int:
        ledger = MemoryEvolutionLedger(MemoryService.evolution_path_for(work_dir, data_dir=self.data_dir))
        jobs = ledger.reserve(now=now)
        if not jobs:
            return 0
        self._changed(work_dir, "", ("memory",))
        def defer(job, exc):
            logger.warning("Memory curation deferred: %s", exc)
            code = "invalid_plan" if isinstance(exc, json.JSONDecodeError) else failure_code(str(exc))
            if code != "transient":
                ledger.invalidate_plan(job)
            ledger.finish(job, "retry_wait", reason=str(exc), reason_code=code)

        try:
            valid = []
            for job in jobs:
                if not self._authorized(job["source"]):
                    ledger.cancel(job["source"]["conversation_id"], source_id=job["source"]["id"])
                elif job["source"].get("locator") == "empty":
                    ledger.finish(job, "no_change", reason="empty_input")
                else:
                    valid.append(job)
            if not valid:
                return len(jobs)
            if self.review is None:
                raise ValueError("memory review is unavailable")
            pending = [job for job in valid if not job.get("plan")]
            # Never send one conversation's evidence to another provider by batching.
            groups: dict[tuple, list] = {}
            for job in pending:
                source = job["source"]
                groups.setdefault((source.get("provider_id"), source.get("model")), []).append(job)
            for group in groups.values():
                try:
                    provider = self._provider(group[0]["source"]) if self._provider else None
                    plans = await asyncio.wait_for(self.review.extract(group, provider=provider), timeout=120)
                    for job in group:
                        if ledger.save_plan(job, plans[job["id"]]):
                            job["plan"] = json.dumps(plans[job["id"]], ensure_ascii=False)
                except Exception as exc:
                    for job in group:
                        defer(job, exc)
            for job in valid:
                if not job.get("plan") or not ledger.is_live(job):
                    continue
                try:
                    state, reason = self.review.apply(ledger, job, json.loads(job["plan"]), authorized=self._authorized)
                    code = failure_code(reason) if state in {"failed", "retry_wait"} else ""
                    if state == "retry_wait" and code != "transient":
                        ledger.invalidate_plan(job)
                    ledger.finish(job, state, reason=reason, reason_code=code)
                except Exception as exc:
                    defer(job, exc)
        except asyncio.CancelledError:
            for job in jobs:
                ledger.finish(job, "retry_wait", reason="application closed during curation")
            raise
        except Exception as exc:
            for job in jobs:
                defer(job, exc)
        finally:
            self._changed(work_dir, "", ("memory", "wiki", "skill"))
        return len(jobs)

    def start(self, workspaces=()) -> None:
        for work_dir in workspaces:
            self.register(work_dir)
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="pycat-curation", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            with self._lock:
                initial = tuple(self._workspaces)
            for work_dir in initial:
                if self._stop.is_set():
                    return
                self.reconcile(work_dir)
            while not self._stop.is_set():
                with self._lock:
                    workspaces = tuple(self._workspaces)
                for work_dir in workspaces:
                    if self._stop.is_set():
                        break
                    try:
                        if work_dir in self._reconcile_iterators or time.monotonic() >= self._next_reconcile.get(work_dir, 0):
                            self.reconcile(work_dir)
                        self._task = self._loop.create_task(self.process_once(work_dir))
                        self._loop.run_until_complete(self._task)
                    except asyncio.CancelledError:
                        break
                    except Exception as exc:
                        logger.warning("Curation worker unavailable for %s: %s", work_dir, exc)
                    finally:
                        self._task = None
                self._wake.wait(5)
                self._wake.clear()
        finally:
            self._loop.close()

    def retry(self, work_dir: str) -> int:
        self.register(work_dir)
        count = MemoryEvolutionLedger(MemoryService.evolution_path_for(work_dir, data_dir=self.data_dir)).retry()
        self._wake.set()
        return count

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        loop, task = self._loop, self._task
        if loop is not None and not loop.is_closed() and task is not None:
            loop.call_soon_threadsafe(task.cancel)
