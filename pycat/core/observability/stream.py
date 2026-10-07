"""Self-contained, bounded stream diagnostics; never used to build model input."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

logger = logging.getLogger(__name__)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _assign(value: Any, path: list, replacement: Any) -> None:
    try:
        for key in path[:-1]:
            value = value[key]
        value[path[-1]] = replacement
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("invalid stream field path") from exc


def _fragment_paths(payload: Any) -> list[list]:
    """Select string fields structurally; all other provider fields stay recorded."""
    if not isinstance(payload, dict):
        return []
    paths = []
    choices = payload.get("choices")
    if isinstance(choices, list):
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict) or choice.get("finish_reason"):
                return []
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            for key in ("content", "reasoning_content", "refusal"):
                if isinstance(delta.get(key), str):
                    paths.append(["choices", i, "delta", key])
            for j, tool in enumerate(delta.get("tool_calls") or []):
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
                    if isinstance(tool["function"].get("arguments"), str):
                        paths.append(["choices", i, "delta", "tool_calls", j, "function", "arguments"])
        return paths
    kind = str(payload.get("type") or "")
    if kind.startswith("response.") and kind.endswith(".delta") and isinstance(payload.get("delta"), str):
        return [["delta"]]
    if kind == "content_block_delta" and isinstance(payload.get("delta"), dict):
        return [["delta", key] for key in ("text", "thinking", "partial_json", "signature")
                if isinstance(payload["delta"].get(key), str)]
    if payload.get("done") is False:
        if isinstance(payload.get("message"), dict):
            return [["message", key] for key in ("content", "thinking")
                    if isinstance(payload["message"].get(key), str)]
        if isinstance(payload.get("response"), str):
            return [["response"]]
    return []


class StreamTraceWriter:
    """One capture owns its timer and serialized writes on the calling asyncio loop.

    Buffered JSONL records are flushed incrementally. Cancellation waits for a
    pending disk write before closing its handle; capture failures stay diagnostic.
    """

    def __init__(self, path: Path, *, redact: Callable, clock: Callable = time.time,
                 max_packets: int = 256, max_bytes: int = 64 * 1024,
                 flush_seconds: float = 0.25, metadata: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("ab")
        self._redact = redact
        self._clock = clock
        self._max_packets = max(1, max_packets)
        self._max_bytes = max(1, max_bytes)
        self._flush_seconds = max(0.01, flush_seconds)
        self._rows = [{"type": "stream", "version": 1, "time": self._now(),
                       "metadata": redact(metadata or {})}]
        self._pending = None
        self._size = 0
        self._packets = 0
        self._queued_packets = 0
        self._shared: set[str] = set()
        self._closed = False
        self._finish_task = None
        self._error = ""
        self._lock = asyncio.Lock()
        self._timer = asyncio.create_task(self._flush_periodically())

    def _now(self) -> int | None:
        timestamp = self._clock()
        return None if timestamp is None else int(timestamp * 1000)

    def _seal(self) -> None:
        if self._pending is not None:
            self._rows.append(self._pending)
            self._pending = None

    def _share_fields(self, payload: Any) -> list[dict]:
        refs = []
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
            return refs
        for field, value in list(payload["response"].items()):
            data = _json_bytes(value)
            if len(data) < 1024:
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest not in self._shared:
                # Bound the per-capture dictionary. New values remain inline at capacity.
                if len(self._shared) >= 128:
                    continue
                self._seal()
                self._shared.add(digest)
                self._rows.append({"type": "shared", "digest": digest, "value": value})
                self._size += len(data)
            payload["response"][field] = None
            refs.append({"path": ["response", field], "digest": digest})
        return refs

    async def record(self, payload: Any) -> None:
        if self._closed or self._error:
            return
        try:
            clean = self._redact(payload)
            now, index = self._now(), self._packets
            self._packets += 1
            self._queued_packets += 1
            paths = _fragment_paths(clean)
            if paths:
                fragments = []
                for path in paths:
                    value = clean
                    for key in path:
                        value = value[key]
                    fragments.append(value)
                    _assign(clean, path, "")
                fields = {key: clean.pop(key) for key in
                          ("sequence_number", "obfuscation", "created_at", "created") if key in clean}
                pending = self._pending
                if (pending is None or pending["template"] != clean or pending["paths"] != paths
                        or list(pending["fields"]) != list(fields)
                        or (pending["time0"] is None) != (now is None)):
                    self._seal()
                    pending = {"type": "delta_run", "index0": index, "time0": now,
                               "template": clean, "paths": paths, "values": [[] for _ in paths],
                               "dt": [], "fields": {key: [] for key in fields}}
                    self._pending = pending
                    self._size += len(_json_bytes(clean))
                else:
                    previous = self._last_packet_time
                    pending["dt"].append(None if now is None or previous is None else now - previous)
                for column, fragment in zip(pending["values"], fragments):
                    column.append(fragment)
                    self._size += len(fragment.encode("utf-8")) + 16
                for key, value in fields.items():
                    pending["fields"][key].append(value)
                self._size += len(_json_bytes(fields))
            else:
                self._seal()
                refs = self._share_fields(clean)
                self._rows.append({"type": "event", "index": index, "time": now,
                                   "payload": clean, "refs": refs})
                self._size += len(_json_bytes(clean))
            self._last_packet_time = now
            if self._queued_packets >= self._max_packets or self._size >= self._max_bytes:
                await self._flush()
        except Exception as exc:
            self._fail(exc)

    async def record_raw(self, text: str) -> None:
        if self._closed or self._error:
            return
        try:
            self._seal()
            clean = self._redact(text)
            self._rows.append({"type": "raw", "index": self._packets,
                               "time": self._now(), "raw": clean})
            self._packets += 1
            self._queued_packets += 1
            self._size += len(_json_bytes(clean))
            if self._queued_packets >= self._max_packets or self._size >= self._max_bytes:
                await self._flush()
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        self._error = str(self._redact(f"{type(exc).__name__}: {exc}"))
        logger.debug("Stream diagnostic capture failed: %s", self._error)

    def _write(self, rows: list[dict]) -> None:
        data = b"\n".join(_json_bytes(row) for row in rows) + b"\n"
        self._file.write(data)
        self._file.flush()

    async def _flush(self) -> None:
        async with self._lock:
            self._seal()
            rows, self._rows = self._rows, []
            self._size = 0
            self._queued_packets = 0
            if not rows:
                return
            job = asyncio.create_task(asyncio.to_thread(self._write, rows))
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(job)
                except Exception as exc:
                    self._fail(exc)
                raise
            except Exception as exc:
                self._fail(exc)

    async def _flush_periodically(self) -> None:
        while True:
            await asyncio.sleep(self._flush_seconds)
            await self._flush()

    async def finish(self, status: str) -> dict:
        if self._finish_task is None:
            self._closed = True
            self._finish_task = asyncio.create_task(self._finish(status))
        try:
            return await asyncio.shield(self._finish_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._finish_task)
            raise

    async def _finish(self, status: str) -> dict:
        self._timer.cancel()
        try:
            await self._timer
        except asyncio.CancelledError:
            pass
        self._seal()
        self._rows.append({"type": "capture_end", "status": status,
                           "packets": self._packets, "capture_error": self._error})
        try:
            await self._flush()
        finally:
            self._file.close()
        return {"packets": self._packets, "capture_error": self._error}


def iter_stream_packets(records: Iterable[dict]) -> Iterator[dict]:
    """Expand recorded JSON fields and arrival times lazily, without executing them."""
    shared = {}
    for row in records:
        kind = row.get("type")
        if kind == "stream":
            if row.get("version") != 1:
                raise ValueError("unsupported stream capture version")
            shared = {}
        elif kind == "shared":
            if hashlib.sha256(_json_bytes(row["value"])).hexdigest() != row.get("digest"):
                raise ValueError("stream shared value digest mismatch")
            shared[row["digest"]] = row["value"]
        elif kind == "event":
            payload = copy.deepcopy(row["payload"])
            for ref in row.get("refs", []):
                if ref["digest"] not in shared:
                    raise ValueError("missing stream shared value")
                _assign(payload, ref["path"], copy.deepcopy(shared[ref["digest"]]))
            yield {"index": row["index"], "time": row["time"], "payload": payload}
        elif kind == "raw":
            yield {"index": row["index"], "time": row["time"], "raw": row["raw"]}
        elif kind == "delta_run":
            columns, paths, gaps = row["values"], row["paths"], row["dt"]
            count = len(columns[0]) if columns else 0
            fields = row.get("fields", {})
            if (not count or len(columns) != len(paths) or len(gaps) != count - 1
                    or any(len(column) != count for column in columns)
                    or any(len(column) != count for column in fields.values())):
                raise ValueError("invalid stream delta run lengths")
            timestamp = row["time0"]
            for index in range(count):
                if index:
                    gap = gaps[index - 1]
                    timestamp = None if timestamp is None or gap is None else timestamp + gap
                payload = copy.deepcopy(row["template"])
                for path, column in zip(paths, columns):
                    _assign(payload, path, column[index])
                payload.update({key: column[index] for key, column in fields.items()})
                yield {"index": row["index0"] + index, "time": timestamp, "payload": payload}
        elif kind != "capture_end":
            raise ValueError("unsupported stream capture record")
