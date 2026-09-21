"""Durable source jobs and publication receipts; never a second text store."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

REVIEW_LEASE_SECONDS = 600
RETRY_DELAYS = (60, 300, 1800)
EXTRACTOR_VERSION = "2"
TERMINAL_STATES = {"applied", "no_change", "failed", "cancelled"}


def failure_code(reason: str) -> str:
    """Classify existing domain errors without adding another recovery protocol."""
    text = str(reason).lower()
    if "changed; reload" in text or "changed while" in text:
        return "version_conflict"
    if "memory limit" in text:
        return "capacity"
    if any(value in text for value in ("invalid", "exactly the selected", "no entry matched", "multiple distinct",
                                      "entry exceeds", "8000 tokens", "permission", "operations is empty")):
        return "invalid_plan"
    return "transient"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MemoryEvolutionLedger:
    """SQLite serializes claims; prepared receipts bridge file publication and SQL."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, digest TEXT NOT NULL,
                    payload TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending', ready_at REAL NOT NULL,
                    lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    plan TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, ready_at);
                CREATE TABLE IF NOT EXISTS captures (conversation_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    digest TEXT NOT NULL, PRIMARY KEY(conversation_id,source_id,digest));
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, domain TEXT NOT NULL,
                    target TEXT NOT NULL, payload TEXT NOT NULL, before_digest TEXT NOT NULL,
                    after_digest TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'prepared');
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                if "reason_code" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                    db.execute("ALTER TABLE jobs ADD COLUMN reason_code TEXT NOT NULL DEFAULT ''")
                yield db
        finally:
            db.close()

    def enqueue(self, source: Mapping[str, Any], *, ready_at: float | None = None) -> bool:
        return bool(self.enqueue_many([source], ready_at=ready_at))

    def enqueue_many(self, sources, *, ready_at: float | None = None, capture: tuple = ()) -> int:
        """All fragments of one source commit together, with one durable flush."""
        rows = []
        for source in sources:
            identity = [source.get(key, "") for key in ("workspace", "conversation_id", "id", "digest", "locator")]
            if not all(identity[index] for index in (1, 2, 3)):
                raise ValueError("a source requires conversation_id, id and digest")
            key = hashlib.sha256(_json([*identity, EXTRACTOR_VERSION]).encode()).hexdigest()
            rows.append((key, str(source["conversation_id"]), str(source["id"]), str(source["digest"]), _json(dict(source))))
        if not rows:
            return 0
        now = time.time()
        count = 0
        with self._connect() as db:
            for row in rows:
                added = db.execute("INSERT OR IGNORE INTO sources(id,conversation_id,source_id,digest,payload) VALUES(?,?,?,?,?)", row).rowcount
                if added:
                    db.execute("INSERT INTO jobs(id,source_id,ready_at,updated_at) VALUES(?,?,?,?)",
                               (row[0], row[0], now + 30 if ready_at is None else ready_at, now))
                    count += 1
            if capture:
                db.execute("INSERT OR IGNORE INTO captures(conversation_id,source_id,digest) VALUES(?,?,?)", capture)
        return count

    def captured(self, conversation_id: str, source_id: str, digest: str) -> bool:
        if not self.path.exists():
            return False
        with self._connect() as db:
            return db.execute("SELECT 1 FROM captures WHERE conversation_id=? AND source_id=? AND digest=?",
                              (conversation_id, source_id, digest)).fetchone() is not None

    def reserve(self, *, now: float | None = None, limit: int = 4) -> list[dict[str, Any]]:
        """One workspace lease and at most four fragments per bounded request."""
        if not self.path.exists():
            return []
        now = time.time() if now is None else now
        with self._connect() as db:
            db.execute("UPDATE jobs SET state='retry_wait', ready_at=?, lease='' WHERE state='running' AND lease_until<=?", (now, now))
            if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                return []
            rows = db.execute(
                "SELECT jobs.*, sources.payload FROM jobs JOIN sources ON jobs.source_id=sources.id "
                "WHERE jobs.state IN ('pending','retry_wait') AND ready_at<=? AND revoked=0 "
                "ORDER BY ready_at, jobs.rowid LIMIT ?", (now, min(4, max(1, limit))),
            ).fetchall()
            lease = uuid.uuid4().hex
            result = []
            for row in rows:
                db.execute("UPDATE jobs SET state='running', lease=?, lease_until=?, attempts=attempts+1, updated_at=? WHERE id=?",
                           (lease, now + REVIEW_LEASE_SECONDS, now, row["id"]))
                result.append({**dict(row), "source": json.loads(row["payload"]), "lease": lease,
                               "attempts": row["attempts"] + 1, "state": "running"})
            return result

    def is_live(self, job: Mapping[str, Any]) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM jobs JOIN sources ON jobs.source_id=sources.id "
                              "WHERE jobs.id=? AND state='running' AND lease=? AND revoked=0",
                              (job["id"], job["lease"])).fetchone() is not None

    @contextmanager
    def publication_guard(self, job: Mapping[str, Any]):
        """Linearize revocation against file publication, without nesting SQL writes."""
        with self._connect() as db:
            live = db.execute("SELECT 1 FROM jobs JOIN sources ON jobs.source_id=sources.id "
                              "WHERE jobs.id=? AND state='running' AND lease=? AND revoked=0",
                              (job["id"], job["lease"])).fetchone() is not None
            yield live

    def save_plan(self, job: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
        with self._connect() as db:
            return bool(db.execute("UPDATE jobs SET plan=? WHERE id=? AND lease=? AND state='running'",
                                   (_json(dict(plan)), job["id"], job["lease"])).rowcount)

    def invalidate_plan(self, job: Mapping[str, Any], operation_id: str = "") -> None:
        """A CAS conflict needs fresh evidence and current facts on the next retry."""
        with self._connect() as db:
            if db.execute("UPDATE jobs SET plan='' WHERE id=? AND lease=? AND state='running'", (job["id"], job["lease"])).rowcount:
                db.execute("DELETE FROM operations WHERE job_id=? AND state='prepared'" + (" AND id=?" if operation_id else ""),
                           (job["id"], operation_id) if operation_id else (job["id"],))

    def applied_targets(self, job: Mapping[str, Any]) -> set[tuple[str, str]]:
        with self._connect() as db:
            return {(row[0], row[1]) for row in db.execute(
                "SELECT domain,target FROM operations WHERE job_id=? AND state='applied'", (job["id"],))}

    def finish(self, job: Mapping[str, Any], state: str, *, reason: str = "", reason_code: str = "", now: float | None = None) -> bool:
        if state not in TERMINAL_STATES | {"retry_wait"}:
            raise ValueError(f"unsupported job state: {state}")
        if state == "no_change" and reason not in {"empty_input", "no_durable_fact", "duplicate"}:
            raise ValueError("no_change requires an explicit reason")
        now = time.time() if now is None else now
        attempts = int(job.get("attempts", 1))
        ready_at = now
        if state == "retry_wait":
            reason_code = reason_code or failure_code(reason)
            max_retries = 1 if reason_code in {"version_conflict", "capacity", "invalid_plan"} else len(RETRY_DELAYS)
            if attempts > max_retries:
                state = "failed"
            else:
                ready_at += RETRY_DELAYS[max(0, attempts - 1)]
        with self._connect() as db:
            return bool(db.execute(
                "UPDATE jobs SET state=?,reason=?,reason_code=?,ready_at=?,lease='',lease_until=0,updated_at=? "
                "WHERE id=? AND lease=? AND state='running'",
                (state, str(reason)[:1000], reason_code, ready_at, now, job["id"], job["lease"]),
            ).rowcount)

    def cancel(self, conversation_id: str, *, source_id: str = "") -> int:
        if not self.path.exists():
            return 0
        with self._connect() as db:
            where = "conversation_id=?" + (" AND source_id=?" if source_id else "")
            params = (conversation_id, source_id) if source_id else (conversation_id,)
            db.execute(f"UPDATE sources SET revoked=1 WHERE {where}", params)
            db.execute("UPDATE operations SET payload='{}' WHERE job_id IN "
                       f"(SELECT id FROM sources WHERE {where})", params)
            db.execute(f"UPDATE jobs SET plan='' WHERE source_id IN (SELECT id FROM sources WHERE {where})", params)
            return db.execute("UPDATE jobs SET state='cancelled',lease='',reason='source revoked' "
                              "WHERE state IN ('pending','running','retry_wait','failed') AND source_id IN "
                              f"(SELECT id FROM sources WHERE {where})", params).rowcount

    def retry(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect() as db:
            return db.execute("UPDATE jobs SET state='pending',ready_at=0,attempts=0,reason='',reason_code='' "
                              "WHERE state IN ('failed','retry_wait') AND source_id IN "
                              "(SELECT id FROM sources WHERE revoked=0)").rowcount

    def prepare_operation(self, job: Mapping[str, Any], domain: str, target: str,
                          payload: Mapping[str, Any], *, before: str = "", after: str = "") -> dict[str, Any]:
        encoded = _json(dict(payload))
        key = hashlib.sha256(_json([job["id"], domain, target, encoded]).encode()).hexdigest()
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO operations(id,job_id,domain,target,payload,before_digest,after_digest) VALUES(?,?,?,?,?,?,?)",
                       (key, job["id"], domain, target, encoded, before, after))
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone())

    def commit_operation(self, operation_id: str) -> bool:
        with self._connect() as db:
            return bool(db.execute("UPDATE operations SET state='applied' WHERE id=?", (operation_id,)).rowcount)

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {"available": True, "pending_count": 0, "inflight": False,
                                 "failure_count": 0, "attention_count": 0, "published_job_count": 0,
                                 "reason_counts": {}, "last_error": "", "review_log": [], "path": str(self.path)}
        if not self.path.exists():
            return result
        try:
            with self._connect() as db:
                counts = dict(db.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
                result.update(pending_count=sum(counts.get(x, 0) for x in ("pending", "running", "retry_wait")),
                              inflight=bool(counts.get("running")), failure_count=counts.get("failed", 0) + counts.get("retry_wait", 0))
                result["attention_count"] = result["failure_count"]
                result["published_job_count"] = db.execute("SELECT count(DISTINCT job_id) FROM operations WHERE state='applied'").fetchone()[0]
                for code, reason, count in db.execute("SELECT reason_code,reason,count(*) FROM jobs WHERE state IN ('failed','retry_wait') GROUP BY reason_code,reason"):
                    code = code or failure_code(reason)
                    result["reason_counts"][code] = result["reason_counts"].get(code, 0) + count
                result["review_log"] = [dict(row) for row in db.execute(
                    "SELECT state AS outcome,reason AS summary,reason_code,updated_at AS at FROM jobs ORDER BY updated_at DESC LIMIT 20")]
                error = db.execute("SELECT reason FROM jobs WHERE state IN ('failed','retry_wait') ORDER BY updated_at DESC LIMIT 1").fetchone()
                result["last_error"] = error[0] if error else ""
        except (OSError, sqlite3.Error) as exc:
            result.update(available=False, last_error=f"memory jobs database unavailable: {exc}")
        return result
