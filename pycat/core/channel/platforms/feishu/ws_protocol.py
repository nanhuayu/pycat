from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

UTF_8 = "utf-8"

GEN_ENDPOINT_URI = "/callback/ws/endpoint"
DEVICE_ID = "device_id"
SERVICE_ID = "service_id"

OK = 0
SYSTEM_BUSY = 1
FORBIDDEN = 403
AUTH_FAILED = 514
INTERNAL_ERROR = 1000040343
NO_CREDENTIAL = 1000040344
EXCEED_CONN_LIMIT = 1000040350

HEADER_TYPE = "type"
HEADER_MESSAGE_ID = "message_id"
HEADER_SUM = "sum"
HEADER_SEQ = "seq"
HEADER_TRACE_ID = "trace_id"
HEADER_BIZ_RT = "biz_rt"
HEADER_HANDSHAKE_STATUS = "handshake-status"
HEADER_HANDSHAKE_MSG = "handshake-msg"
HEADER_HANDSHAKE_AUTH_ERRCODE = "handshake-autherrcode"


class FrameType(Enum):
    CONTROL = 0
    DATA = 1


class MessageType(Enum):
    EVENT = "event"
    CARD = "card"
    PING = "ping"
    PONG = "pong"


@dataclass(slots=True)
class FeishuFrameHeader:
    key: str
    value: str


@dataclass(slots=True)
class FeishuFrame:
    SeqID: int = 0
    LogID: int = 0
    service: int = 0
    method: int = 0
    headers: list[FeishuFrameHeader] = field(default_factory=list)
    payload_encoding: str = ""
    payload_type: str = ""
    payload: bytes = b""
    LogIDNew: str = ""

    def add_header(self, key: str, value: str) -> FeishuFrameHeader:
        header = FeishuFrameHeader(str(key), str(value))
        self.headers.append(header)
        return header

    def header_value(self, key: str, default: str = "") -> str:
        for header in self.headers:
            if header.key == key:
                return str(header.value or "")
        return default


class FeishuFrameDecodeError(ValueError):
    pass


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint value must be non-negative")
    chunks = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            chunks.append(byte | 0x80)
        else:
            chunks.append(byte)
            return bytes(chunks)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift >= 64:
            break
    raise FeishuFrameDecodeError("invalid protobuf varint")


def _encode_key(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _encode_uint64_field(field_number: int, value: int) -> bytes:
    return _encode_key(field_number, 0) + _encode_varint(int(value))


def _encode_int32_field(field_number: int, value: int) -> bytes:
    value = int(value)
    if value < 0:
        value = (1 << 64) + value
    return _encode_key(field_number, 0) + _encode_varint(value)


def _encode_length_delimited_field(field_number: int, value: bytes) -> bytes:
    return _encode_key(field_number, 2) + _encode_varint(len(value)) + value


def _encode_string_field(field_number: int, value: str) -> bytes:
    return _encode_length_delimited_field(field_number, str(value).encode(UTF_8))


def _encode_header(header: FeishuFrameHeader) -> bytes:
    return _encode_string_field(1, header.key) + _encode_string_field(2, header.value)


def encode_frame(frame: FeishuFrame) -> bytes:
    encoded = bytearray()
    encoded += _encode_uint64_field(1, frame.SeqID)
    encoded += _encode_uint64_field(2, frame.LogID)
    encoded += _encode_int32_field(3, frame.service)
    encoded += _encode_int32_field(4, frame.method)
    for header in frame.headers:
        encoded += _encode_length_delimited_field(5, _encode_header(header))
    if frame.payload_encoding:
        encoded += _encode_string_field(6, frame.payload_encoding)
    if frame.payload_type:
        encoded += _encode_string_field(7, frame.payload_type)
    if frame.payload:
        encoded += _encode_length_delimited_field(8, bytes(frame.payload))
    if frame.LogIDNew:
        encoded += _encode_string_field(9, frame.LogIDNew)
    return bytes(encoded)


def _skip_field(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, offset = _decode_varint(data, offset)
        return offset
    if wire_type == 1:
        return min(len(data), offset + 8)
    if wire_type == 2:
        length, offset = _decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise FeishuFrameDecodeError("truncated length-delimited field")
        return end
    if wire_type == 5:
        return min(len(data), offset + 4)
    raise FeishuFrameDecodeError(f"unsupported protobuf wire type: {wire_type}")


def _decode_header(data: bytes) -> FeishuFrameHeader:
    key = ""
    value = ""
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type != 2:
            offset = _skip_field(data, offset, wire_type)
            continue
        length, offset = _decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise FeishuFrameDecodeError("truncated header field")
        raw = data[offset:end]
        offset = end
        if field_number == 1:
            key = raw.decode(UTF_8, errors="ignore")
        elif field_number == 2:
            value = raw.decode(UTF_8, errors="ignore")
    return FeishuFrameHeader(key=key, value=value)


def decode_frame(data: bytes | bytearray | memoryview) -> FeishuFrame:
    raw = bytes(data)
    frame = FeishuFrame()
    offset = 0
    while offset < len(raw):
        tag, offset = _decode_varint(raw, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if field_number in {1, 2, 3, 4} and wire_type == 0:
            value, offset = _decode_varint(raw, offset)
            if field_number == 1:
                frame.SeqID = value
            elif field_number == 2:
                frame.LogID = value
            elif field_number == 3:
                frame.service = value
            else:
                frame.method = value
            continue

        if field_number in {5, 6, 7, 8, 9} and wire_type == 2:
            length, offset = _decode_varint(raw, offset)
            end = offset + length
            if end > len(raw):
                raise FeishuFrameDecodeError("truncated frame field")
            payload = raw[offset:end]
            offset = end
            if field_number == 5:
                frame.headers.append(_decode_header(payload))
            elif field_number == 6:
                frame.payload_encoding = payload.decode(UTF_8, errors="ignore")
            elif field_number == 7:
                frame.payload_type = payload.decode(UTF_8, errors="ignore")
            elif field_number == 8:
                frame.payload = payload
            else:
                frame.LogIDNew = payload.decode(UTF_8, errors="ignore")
            continue

        offset = _skip_field(raw, offset, wire_type)

    return frame


def build_ping_frame(service_id: int) -> FeishuFrame:
    frame = FeishuFrame(service=int(service_id), method=FrameType.CONTROL.value, SeqID=0, LogID=0)
    frame.add_header(HEADER_TYPE, MessageType.PING.value)
    return frame


def build_response_payload(code: int, data: bytes | str | None = None) -> bytes:
    payload: dict[str, object] = {"code": int(code)}
    if data is not None:
        payload["data"] = data.decode(UTF_8, errors="ignore") if isinstance(data, bytes) else str(data)
    return json.dumps(payload, ensure_ascii=False).encode(UTF_8)


def build_response_frame(source_frame: FeishuFrame, *, code: int, biz_rt_ms: int = 0, data: bytes | str | None = None) -> FeishuFrame:
    source_frame.add_header(HEADER_BIZ_RT, str(max(0, int(biz_rt_ms))))
    source_frame.payload = build_response_payload(code, data=data)
    return source_frame


def headers_to_dict(headers: Iterable[FeishuFrameHeader]) -> dict[str, str]:
    return {header.key: header.value for header in headers}


__all__ = [
    "AUTH_FAILED",
    "DEVICE_ID",
    "EXCEED_CONN_LIMIT",
    "FORBIDDEN",
    "GEN_ENDPOINT_URI",
    "HEADER_BIZ_RT",
    "HEADER_HANDSHAKE_AUTH_ERRCODE",
    "HEADER_HANDSHAKE_MSG",
    "HEADER_HANDSHAKE_STATUS",
    "HEADER_MESSAGE_ID",
    "HEADER_SEQ",
    "HEADER_SUM",
    "HEADER_TRACE_ID",
    "HEADER_TYPE",
    "INTERNAL_ERROR",
    "MessageType",
    "NO_CREDENTIAL",
    "OK",
    "SERVICE_ID",
    "SYSTEM_BUSY",
    "UTF_8",
    "FeishuFrame",
    "FeishuFrameDecodeError",
    "FeishuFrameHeader",
    "FrameType",
    "build_ping_frame",
    "build_response_frame",
    "build_response_payload",
    "decode_frame",
    "encode_frame",
    "headers_to_dict",
]
