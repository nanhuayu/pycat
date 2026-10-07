"""Compact old debug JSONL into a separate directory and verify every record.

Run from the checkout: python -m scripts.compact_debug_jsonl --source PATH.
Sources are never replaced. No gzip, redaction, or historical timing inference.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import itertools
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pycat.core.observability.stream import StreamTraceWriter, iter_stream_packets

MAX_LINE_BYTES = 32 * 1024 * 1024


def _encode(value, *, canonical=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=canonical,
                      separators=(",", ":")).encode("utf-8")


def _lines(path, limit, digest):
    with path.open("rb") as source:
        while raw := source.readline(limit + 1):
            if len(raw) > limit:
                raise ValueError(f"line exceeds {limit} bytes; source is retained")
            digest.update(raw)
            yield raw


def _packet(raw):
    try:
        return {"payload": json.loads(raw)}
    except json.JSONDecodeError:
        # Preserve whitespace, line endings and malformed/control text verbatim.
        return {"raw": raw.decode("utf-8")}


def _format(path, first):
    value = _packet(first).get("payload") if first else None
    if (isinstance(value, dict) and value.get("type") == "stream"
            and {"version", "metadata", "time"}.issubset(value)):
        if value["version"] != 1:
            raise ValueError("unsupported source stream capture version")
        return "compact_stream"
    return "index" if path.name == "events.jsonl" else "legacy_stream"


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _update(digest, packet):
    digest.update(_encode(packet, canonical=True) + b"\n")


def _scan(path, limit, *, kind=None):
    physical, semantic = hashlib.sha256(), hashlib.sha256()
    lines = iter(_lines(path, limit, physical))
    first = next(lines, b"")
    kind = kind or _format(path, first)
    lines = itertools.chain([first], lines) if first else lines
    unavailable = True
    if kind == "compact_stream":
        packets = iter_stream_packets(json.loads(line) for line in lines)
    else:
        packets = (_packet(line) for line in lines)
    count = 0
    for packet in packets:
        if kind == "compact_stream":
            unavailable = unavailable and packet["time"] is None
            packet = {key: packet[key] for key in ("payload", "raw") if key in packet}
        _update(semantic, packet)
        count += 1
    return {"format": kind, "records": count, "semantic_sha256": semantic.hexdigest(),
            "sha256": physical.hexdigest(), "arrival_times_unavailable": unavailable}


def _verify(expected, actual):
    if (expected["records"] != actual["records"]
            or expected["semantic_sha256"] != actual["semantic_sha256"]):
        raise ValueError("output does not match the source records")
    if expected["format"] == "compact_stream":
        if expected["sha256"] != actual["sha256"]:
            raise ValueError("output does not match the existing compact capture")
    elif actual["format"] == "compact_stream" and not actual["arrival_times_unavailable"]:
        raise ValueError("output contains arrival times not recorded by the source")


def _unchanged(source, before):
    if _signature(source) != before:
        raise RuntimeError("source changed during compaction; output is not published")


async def compact_file(source: Path, destination: Path, *, max_line_bytes=MAX_LINE_BYTES):
    """Publish one independently verified file, retaining its original source."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("symlink files are not compaction targets")
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination:
        raise ValueError("source and destination must differ")
    if max_line_bytes < 1:
        raise ValueError("max_line_bytes must be positive")
    before = _signature(source)
    output_limit = max_line_bytes * 2 + 64 * 1024
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary, writer = None, None
    published, successful = False, False
    try:
        if destination.exists():
            expected = _scan(source, max_line_bytes)
            actual = _scan(destination, output_limit)
            _verify(expected, actual)
            status = "verified_existing"
        else:
            fd, name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".partial",
                                        dir=destination.parent)
            os.close(fd)
            temporary = Path(name)
            physical, semantic = hashlib.sha256(), hashlib.sha256()
            lines = iter(_lines(source, max_line_bytes, physical))
            first = next(lines, b"")
            kind = _format(source, first)
            lines = itertools.chain([first], lines) if first else lines
            count = 0
            if kind == "legacy_stream":
                writer = StreamTraceWriter(temporary, redact=copy.deepcopy, clock=lambda: None,
                                          metadata={"source_file": source.name,
                                                    "source_bytes": before[0],
                                                    "timing": "not_recorded", "imported": True})
                for raw in lines:
                    packet = _packet(raw)
                    _update(semantic, packet)
                    count += 1
                    if "payload" in packet:
                        await writer.record(packet["payload"])
                    else:
                        await writer.record_raw(packet["raw"])
                summary = await writer.finish("imported")
                writer = None
                if summary["capture_error"]:
                    raise RuntimeError(f"stream capture failed: {summary['capture_error']}")
            elif kind == "index":
                with temporary.open("wb") as target:
                    for raw in lines:
                        packet = _packet(raw)
                        _update(semantic, packet)
                        count += 1
                        target.write(_encode(packet["payload"]) + b"\n" if "payload" in packet else raw)
            else:
                with temporary.open("wb") as target:
                    def copied_rows():
                        for raw in lines:
                            target.write(raw)
                            yield json.loads(raw)
                    for frame in iter_stream_packets(copied_rows()):
                        _update(semantic, {key: frame[key] for key in ("payload", "raw") if key in frame})
                        count += 1
            expected = {"format": kind, "records": count,
                        "semantic_sha256": semantic.hexdigest(), "sha256": physical.hexdigest()}
            _unchanged(source, before)
            actual = _scan(temporary, output_limit, kind="index" if kind == "index" else None)
            _verify(expected, actual)
            status = "copied_compact" if kind == "compact_stream" else "converted"
            if kind != "compact_stream" and temporary.stat().st_size >= before[0]:
                # Compaction must not enlarge small/unknown captures.
                copied_hash = hashlib.sha256()
                with temporary.open("wb") as target:
                    for raw in _lines(source, max_line_bytes, copied_hash):
                        target.write(raw)
                if copied_hash.hexdigest() != expected["sha256"]:
                    raise RuntimeError("source changed while retaining original encoding")
                actual = _scan(temporary, output_limit, kind=kind)
                _verify(expected, actual)
                status = "kept_original"
            _unchanged(source, before)
            # Atomic no-overwrite publication; both names are on the output filesystem.
            os.link(temporary, destination)
            published = True
        _unchanged(source, before)
        result = {"file": source.name, "status": status, "verified": True,
                  "records": expected["records"], "source_bytes": before[0],
                  "output_bytes": destination.stat().st_size, "source_mtime_ns": before[1],
                  "source_sha256": expected["sha256"], "output_sha256": actual["sha256"],
                  "semantic_sha256": expected["semantic_sha256"],
                  "source_format": expected["format"], "output_format": actual["format"]}
        successful = True
        return result
    finally:
        try:
            if writer is not None:
                await writer.finish("error")
        finally:
            if temporary is not None:
                if published and not successful and destination.exists() and destination.samefile(temporary):
                    destination.unlink()
                temporary.unlink(missing_ok=True)


def _summary(results, total):
    verified = [row for row in results if row.get("verified")]
    source_bytes = sum(row["source_bytes"] for row in verified)
    output_bytes = sum(row["output_bytes"] for row in verified)
    return {"total_files": total, "processed_files": len(results),
            "verified_files": len(verified), "failed_files": len(results) - len(verified),
            "source_bytes": source_bytes, "output_bytes": output_bytes,
            "saved_bytes": source_bytes - output_bytes,
            "saved_percent": round(100 * (1 - output_bytes / source_bytes), 2) if source_bytes else 0}


def _write_report(path, report):
    fd, name = tempfile.mkstemp(prefix="report.", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def _run(source, output, files, max_line_bytes):
    report = {"schema_version": 1, "source_dir": str(source), "output_dir": str(output),
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "verification": "Ordered JSON values and exact non-JSON text; original bytes hashed separately",
              "timing": "Legacy arrival times are not recorded; imported records use null",
              "files": []}
    start, last_progress = time.perf_counter(), 0
    try:
        for path in files:
            try:
                result = await compact_file(path, output / path.name, max_line_bytes=max_line_bytes)
            except Exception as exc:
                result = {"file": path.name, "status": "failed", "verified": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            report["files"].append(result)
            now = time.perf_counter()
            if now - last_progress >= 5 or len(report["files"]) == len(files):
                report["summary"] = _summary(report["files"], len(files))
                report["elapsed_seconds"] = round(now - start, 3)
                _write_report(output / "report.json", report)
                print(json.dumps({"progress": report["summary"],
                                  "elapsed_seconds": report["elapsed_seconds"]}), flush=True)
                last_progress = now
        report["status"] = "completed"
    except BaseException:
        report["status"] = "interrupted"
        raise
    finally:
        report["summary"] = _summary(report["files"], len(files))
        report["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(output / "report.json", report)
    print(json.dumps({"report": str(output / "report.json"), "summary": report["summary"]}), flush=True)
    return 1 if report["summary"]["failed_files"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Debug directory containing JSONL files")
    parser.add_argument("--output", type=Path, help="Separate directory; default: sibling <source>-compact")
    parser.add_argument("--limit", type=int, default=0, help="Largest N files first; 0 processes all")
    parser.add_argument("--max-line-mib", type=int, default=32, help="Oversized lines fail safely, without truncation")
    args = parser.parse_args(argv)
    try:
        source = args.source.resolve(strict=True)
        output = (args.output or source.with_name(source.name + "-compact")).resolve()
        if not source.is_dir() or output.is_relative_to(source):
            raise ValueError("output must be separate from the source directory")
        if args.limit < 0 or args.max_line_mib < 1:
            raise ValueError("limits must be positive; --limit 0 means all files")
        output.mkdir(parents=True, exist_ok=True)
        report_path = output / "report.json"
        if report_path.exists():
            if report_path.is_symlink():
                raise ValueError("output report must not be a symlink")
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            if (not isinstance(previous, dict) or previous.get("schema_version") != 1
                    or not isinstance(previous.get("source_dir"), str)
                    or Path(previous["source_dir"]).resolve() != source):
                raise ValueError("output report belongs to a different source directory")
        files = sorted((path for path in source.glob("*.jsonl") if path.is_file()),
                       key=lambda path: (-path.stat().st_size, path.name))
        if args.limit:
            files = files[:args.limit]
        return asyncio.run(_run(source, output, files, args.max_line_mib * 1024 * 1024))
    except (OSError, ValueError) as exc:
        print(f"Compaction error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Compaction interrupted; originals retained and completed outputs can be verified on rerun.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
