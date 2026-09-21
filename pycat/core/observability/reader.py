"""Bounded, read-only access to existing trace files for SDK and GUI."""
from __future__ import annotations

import json
import math
from pathlib import Path

from pycat.core.observability.debug_trace import redact_debug_payload

MAX_EVENTS = 2000
MAX_TRACE_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024


def read_trace_events(path: Path, *, request_ids=(), cursor: int | None = None,
                      limit: int = MAX_EVENTS, max_bytes: int = MAX_TRACE_BYTES) -> dict:
    """None selects a bounded tail; a byte cursor selects a forward page."""
    limit = max(1, min(MAX_EVENTS, int(limit)))
    budget = max(1, min(MAX_TRACE_BYTES, int(max_bytes)))
    result = {"events": [], "next_cursor": cursor or 0, "has_more": False, "truncated": False,
              "invalid_lines": 0, "incomplete_tail": False, "status": "ok", "note": ""}
    try:
        with Path(path).open("rb") as source:
            size = source.seek(0, 2)
            start = max(0, size - budget) if cursor is None else int(cursor)
            if not 0 <= start <= size:
                raise ValueError("trace cursor is outside the current file")
            source.seek(start)
            raw = source.read(budget)
    except FileNotFoundError:
        return {**result, "status": "missing", "note": "尚未记录运行事件，或记录已移除。"}
    except OSError as exc:
        return {**result, "status": "unavailable", "note": f"读取失败：{exc}"}
    tail = cursor is None
    result["truncated"] = start > 0 if tail else False
    position = start
    for index, line in enumerate(raw.splitlines(keepends=True)):
        end = position + len(line)
        if tail and start and index == 0:
            position = end
            continue  # Tail may start inside a record.
        complete = line.endswith(b"\n")
        try:
            payload = json.loads(line) if line.strip() else None
        except (ValueError, UnicodeError):
            if not complete and end == size:
                result["incomplete_tail"] = True
                break
            if not complete and end < size:
                result["truncated"] = True
                break
            result["invalid_lines"] += 1
            position = end
            continue
        if isinstance(payload, dict) and (not request_ids or payload.get("request_id") in request_ids):
            result["events"].append(payload)
        position = end
        if not tail and len(result["events"]) >= limit:
            break
    if tail and len(result["events"]) > limit:
        result["events"] = result["events"][-limit:]
        result["truncated"] = True
    result["next_cursor"] = position
    result["has_more"] = position < size
    notes = []
    if result["truncated"]:
        notes.append("显示有界预览；完整日志保留在原文件中。")
    if result["invalid_lines"]:
        notes.append(f"有 {result['invalid_lines']} 行无法解析。")
    if result["incomplete_tail"]:
        notes.append("末行尚不完整，可稍后从 next_cursor 重读。")
    if request_ids and not result["events"]:
        notes.append("当前读取范围没有本次运行事件。")
    result["note"] = " ".join(notes)
    return result


def read_trace_payload(debug_dir: Path, relative: str | None, *, as_json: bool = False,
                       max_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """Never follow references outside this conversation's debug directory."""
    def result(status, note="", content=None):
        return {"status": status, "note": note, "content": content}

    if not relative:
        return result("not_captured", "attachment not recorded")
    root = Path(debug_dir).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        return result("invalid_path", "invalid attachment path")
    try:
        with target.open("rb") as source:
            budget = max(1, min(MAX_PAYLOAD_BYTES, int(max_bytes)))
            raw = source.read(budget + 1)
        if len(raw) > budget:
            return result("too_large", "attachment exceeds preview limit; open the debug folder for the original")
        text = raw.decode("utf-8")
        return result("ok", content=json.loads(text) if as_json else text)
    except FileNotFoundError:
        return result("missing", "attachment file missing")
    except (UnicodeError, ValueError) as exc:
        return result("invalid_json", f"attachment cannot be parsed: {exc}")
    except OSError as exc:
        return result("unavailable", f"attachment read failed: {exc}")


def payload_has_gaps(value) -> bool:
    """Captured placeholders must not be mistaken for reusable original input."""
    if isinstance(value, dict):
        return (value.get("truncated") is True or value.get("redacted") is True
                or value.get("type") == "image_ref"
                or any(payload_has_gaps(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return any(payload_has_gaps(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        return True
    return isinstance(value, str) and any(marker in value for marker in
        ("<redacted", "<max-depth>", "[image_ref "))


def read_trace_node(debug_dir: Path, events: list[dict], *, archive_reader=None,
                    max_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """Project one node; the application supplies bounded Archive access.

    Each part preserves source, availability and capture gaps. No live state is
    substituted for historical evidence, and no tool is executed here.
    """
    def mapping(value):
        return value if isinstance(value, dict) else {}

    def part(result, source):
        content = redact_debug_payload(result.get("content"))
        status = result["status"]
        note = result.get("note", "")
        if status == "ok" and payload_has_gaps(content):
            status, note = "partial", "包含脱敏、图片引用或裁剪内容；不代表原始输入完整。"
        return {"source": source, "status": status, "note": note, "content": content}

    refs = {}
    for event in events:
        refs.update({str(k): str(v) for k, v in mapping(event.get("refs")).items() if v})
    first, last = (events[0], events[-1]) if events else ({}, {})
    kind = first.get("kind") or last.get("kind") or ""
    request = part(read_trace_payload(debug_dir, refs.get("request"), as_json=True, max_bytes=max_bytes), "trace")
    response = part(read_trace_payload(debug_dir, refs.get("response"), as_json=True, max_bytes=max_bytes), "trace")
    raw, model, tool = response, response, None
    if kind == "tool":
        name = str(first.get("tool_name") or last.get("tool_name") or first.get("name") or "")
        call_id = next((str(e["tool_call_id"]) for e in events if e.get("tool_call_id")), "")
        payload = response["content"] if isinstance(response["content"], dict) else {}
        archive = mapping(payload.get("archive"))
        content_id = str(archive.get("content_id") or "")
        for event in reversed(events):
            content_id = content_id or str(mapping(event.get("data")).get("content_id")
                                          or mapping(event.get("refs")).get("content_id") or "")

        def archived(which):
            if archive_reader and content_id:
                value = part(archive_reader(which, content_id), "archive")
                if which == "input" and value["content"] is not None:
                    recorded = mapping(value["content"])
                    recorded_id = str(recorded.get("tool_call_id") or "")
                    captured_args = next((mapping(e.get("data"))["args"] for e in events
                                          if "args" in mapping(e.get("data"))), None)
                    comparable = isinstance(captured_args, dict) and not payload_has_gaps(captured_args)
                    if (call_id and recorded_id and recorded_id != call_id
                            or recorded.get("tool_name") and recorded["tool_name"] != name
                            or comparable and recorded.get("arguments", recorded) != captured_args):
                        return {"source": "archive", "status": "mismatched_call", "content": None,
                                "note": "Archive 输入属于其它工具调用，未用作本步骤参数。"}
                    if not call_id or not recorded_id or not comparable:
                        value.update(status="partial", note="Archive 调用身份或历史参数无法完整核对，请检查后复测。")
                return value
            return {"source": "archive", "status": "missing", "content": None, "note": "Archive 内容不存在。"}

        if request["status"] != "ok":
            fallback = archived("input")
            if fallback["content"] is not None:
                request = {**fallback, "note": " ".join(filter(None, (request["status"], fallback["note"])))}
            elif request["content"] is None:
                for event in events:
                    args = mapping(event.get("data")).get("args")
                    if args is not None:
                        request = {"source": "event", "status": "partial",
                                   "note": f"{request['status']}; {fallback['status']}; 只有有界事件参数，请核对补齐。",
                                   "content": redact_debug_payload({"arguments": args})}
                        break
                else:
                    request["note"] = " ".join(filter(None, (request["note"], fallback["note"])))

        raw_content = mapping(payload.get("raw_result")).get("content")
        raw = part({"status": "ok", "content": raw_content}, "trace") if raw_content is not None else {
            **response, "content": None}
        if raw["status"] != "ok" or raw["content"] is None:
            fallback = archived("original")
            if fallback["content"] is not None or raw["content"] is None:
                raw = {**fallback, "note": " ".join(filter(None, (raw["note"], fallback["note"])))}

        model_content = mapping(payload.get("model_result")).get("content")
        if model_content is not None:
            model = part({"status": "ok", "content": model_content}, "trace")
        else:
            model = {"source": "event", "status": "partial", "note": "未捕获模型实际收到的结果，仅有事件摘要。",
                     "content": redact_debug_payload({"status": last.get("status"), "summary": last.get("summary"),
                                                       "data": last.get("data", {})})}
        value = request["content"]
        # Current Archive input is wrapped like Trace; older records may be flat.
        arguments = None
        if isinstance(value, dict):
            if "arguments" in value and (request["source"] != "archive" or "tool_name" in value):
                arguments = value["arguments"]
            elif request["source"] == "archive":
                arguments = value
        valid = isinstance(arguments, dict) and not payload_has_gaps(arguments)
        tool = {"name": name, "arguments": arguments if isinstance(arguments, dict) else None,
                "arguments_status": "ok" if valid and request["status"] == "ok" else "partial" if arguments is not None else "missing",
                "note": request["note"]}
    return {"status": "ok" if events else "not_found", "run_id": first.get("request_id", ""),
            "node_id": first.get("node_id", ""), "kind": kind, "refs": refs,
            "request": request, "raw_result": raw, "model_result": model, "tool": tool,
            "events": [{k: v for k, v in e.items() if not k.startswith("_")} for e in events]}
