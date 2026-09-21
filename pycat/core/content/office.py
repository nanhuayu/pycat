"""Bounded text extraction for modern Office Open XML packages."""
from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET


MAX_OFFICE_XML_BYTES = 8 * 1024 * 1024
MAX_OFFICE_TOTAL_XML_BYTES = 64 * 1024 * 1024
MAX_OFFICE_ZIP_ENTRIES = 4096

_WORD_EXTENSIONS = {".docx", ".docm", ".dotx", ".dotm"}
_EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_LEGACY_WORD_EXTENSIONS = {".doc"}
_LEGACY_EXCEL_EXTENSIONS = {".xls"}
_WORD_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/vnd.ms-word.template.macroenabled.12",
    "application/msword",
}
_EXCEL_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    "application/vnd.ms-excel.template.macroenabled.12",
    "application/vnd.ms-excel",
}
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class OfficeExtractionError(ValueError):
    """Raised when an Office attachment cannot be safely read."""


@dataclass(frozen=True)
class OfficeText:
    text: str
    truncated: bool = False


def is_office_attachment(name: str, mime: str = "") -> bool:
    suffix = PurePosixPath(str(name or "").replace("\\", "/")).suffix.lower()
    normalized_mime = str(mime or "").split(";", 1)[0].strip().lower()
    return (
        suffix in _WORD_EXTENSIONS
        or suffix in _EXCEL_EXTENSIONS
        or suffix in _LEGACY_WORD_EXTENSIONS
        or suffix in _LEGACY_EXCEL_EXTENSIONS
        or normalized_mime in _WORD_MIMES
        or normalized_mime in _EXCEL_MIMES
    )


def extract_office_text(
    source: bytes | bytearray | str | Path,
    *,
    name: str,
    mime: str = "",
    max_bytes: int,
) -> OfficeText:
    """Extract a bounded, model-readable view from a DOCX/XLSX package.

    Legacy binary ``.doc`` and ``.xls`` files are kept as input snapshots but
    are intentionally not parsed without a platform-specific converter.
    """

    if max_bytes <= 0:
        return OfficeText("", True)
    suffix = PurePosixPath(str(name or "").replace("\\", "/")).suffix.lower()
    normalized_mime = str(mime or "").split(";", 1)[0].strip().lower()
    if suffix in _LEGACY_WORD_EXTENSIONS or suffix in _LEGACY_EXCEL_EXTENSIONS:
        raise OfficeExtractionError("legacy Office formats require conversion to DOCX or XLSX")

    is_word = suffix in _WORD_EXTENSIONS or normalized_mime in _WORD_MIMES
    is_excel = suffix in _EXCEL_EXTENSIONS or normalized_mime in _EXCEL_MIMES
    if not (is_word or is_excel):
        raise OfficeExtractionError("unsupported Office format")

    package_source = (
        io.BytesIO(bytes(source))
        if isinstance(source, (bytes, bytearray))
        else Path(source)
    )
    try:
        with zipfile.ZipFile(package_source) as package:
            infos = package.infolist()
            if len(infos) > MAX_OFFICE_ZIP_ENTRIES:
                raise OfficeExtractionError("Office package contains too many entries")
            xml_size = sum(
                info.file_size
                for info in infos
                if info.filename.endswith(".xml") or info.filename.endswith(".rels")
            )
            if xml_size > MAX_OFFICE_TOTAL_XML_BYTES:
                raise OfficeExtractionError(
                    f"Office XML parts exceed {MAX_OFFICE_TOTAL_XML_BYTES} bytes"
                )
            if is_word:
                parts = _extract_docx(package)
            else:
                parts = _extract_xlsx(package)
    except OfficeExtractionError:
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise OfficeExtractionError(f"invalid Office package: {exc}") from exc

    text = "\n".join(part.strip("\n") for part in parts if part.strip())
    return _limit_text(text, max_bytes)


def _extract_docx(package: zipfile.ZipFile) -> list[str]:
    document = _parse_xml(_read_entry(package, "word/document.xml"))
    parts = _word_blocks(document)
    for entry in sorted(package.namelist()):
        if entry.startswith("word/header") or entry.startswith("word/footer"):
            if entry.endswith(".xml"):
                parts.extend(_word_blocks(_parse_xml(_read_entry(package, entry))))
    return parts


def _word_blocks(root: ET.Element) -> list[str]:
    body = root.find(f"{{{_WORD_NS}}}body")
    container = body if body is not None else root
    blocks: list[str] = []
    for child in list(container):
        tag = _local_name(child.tag)
        if tag == "p":
            value = _word_paragraph(child)
            if value.strip():
                blocks.append(value)
        elif tag == "tbl":
            blocks.extend(_word_table(child))
    return blocks


def _word_paragraph(node: ET.Element) -> str:
    chunks: list[str] = []
    for child in node.iter():
        tag = _local_name(child.tag)
        if tag == "t":
            chunks.append(child.text or "")
        elif tag == "tab":
            chunks.append("\t")
        elif tag in {"br", "cr"}:
            chunks.append("\n")
    return "".join(chunks).strip()


def _word_table(node: ET.Element) -> list[str]:
    rows: list[str] = []
    for row in list(node):
        if _local_name(row.tag) != "tr":
            continue
        cells: list[str] = []
        for cell in list(row):
            if _local_name(cell.tag) != "tc":
                continue
            values = [
                _word_paragraph(paragraph)
                for paragraph in cell.iter()
                if _local_name(paragraph.tag) == "p"
            ]
            cells.append(" ".join(value for value in values if value))
        if any(cells):
            rows.append("\t".join(cells))
    return rows


def _extract_xlsx(package: zipfile.ZipFile) -> list[str]:
    shared_strings = _read_shared_strings(package)
    sheets = _workbook_sheets(package)
    if not sheets:
        sheets = [
            (PurePosixPath(entry).stem, entry)
            for entry in sorted(package.namelist())
            if entry.startswith("xl/worksheets/") and entry.endswith(".xml")
        ]

    parts: list[str] = []
    for title, entry in sheets:
        root = _parse_xml(_read_entry(package, entry))
        rows = _sheet_rows(root, shared_strings)
        if rows:
            parts.append(f"[Sheet: {title}]")
            parts.extend(rows)
    return parts


def _workbook_sheets(package: zipfile.ZipFile) -> list[tuple[str, str]]:
    try:
        workbook = _parse_xml(_read_entry(package, "xl/workbook.xml"))
        relationships = _parse_xml(_read_entry(package, "xl/_rels/workbook.xml.rels"))
    except (KeyError, OfficeExtractionError):
        return []

    targets = {
        relationship.attrib.get("Id", ""): _normalize_package_path(relationship.attrib.get("Target", ""))
        for relationship in relationships
        if _local_name(relationship.tag) == "Relationship"
    }
    result: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{{{_SHEET_NS}}}sheet"):
        relation_id = sheet.attrib.get(f"{{{_REL_NS}}}id", "")
        target = targets.get(relation_id)
        if target:
            result.append((sheet.attrib.get("name", "Sheet"), target))
    return result


def _read_shared_strings(package: zipfile.ZipFile) -> list[str]:
    try:
        root = _parse_xml(_read_entry(package, "xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.iter(f"{{{_SHEET_NS}}}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{{{_SHEET_NS}}}t")))
    return values


def _sheet_rows(root: ET.Element, shared_strings: list[str]) -> list[str]:
    rows: list[str] = []
    for row in root.iter(f"{{{_SHEET_NS}}}row"):
        cells: list[str] = []
        for cell in list(row):
            if _local_name(cell.tag) != "c":
                continue
            reference = cell.attrib.get("r", "cell")
            value = _cell_value(cell, shared_strings)
            if value:
                cells.append(f"{reference}={value}")
        if cells:
            rows.append(" | ".join(cells))
    return rows


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    kind = cell.attrib.get("t", "")
    value_node = cell.find(f"{{{_SHEET_NS}}}v")
    value = value_node.text if value_node is not None and value_node.text is not None else ""
    formula_node = cell.find(f"{{{_SHEET_NS}}}f")
    formula = (formula_node.text or "") if formula_node is not None else ""
    if kind == "s":
        try:
            value = shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""
    elif kind == "inlineStr":
        value = "".join(node.text or "" for node in cell.iter(f"{{{_SHEET_NS}}}t"))
    elif kind == "b":
        value = "TRUE" if value == "1" else "FALSE"
    if formula:
        return f"={formula} -> {value}" if value else f"={formula}"
    return value


def _read_entry(package: zipfile.ZipFile, name: str) -> bytes:
    info = package.getinfo(name)
    if info.file_size > MAX_OFFICE_XML_BYTES:
        raise OfficeExtractionError(f"Office XML part exceeds {MAX_OFFICE_XML_BYTES} bytes")
    with package.open(info) as stream:
        data = stream.read(MAX_OFFICE_XML_BYTES + 1)
    if len(data) > MAX_OFFICE_XML_BYTES:
        raise OfficeExtractionError(f"Office XML part exceeds {MAX_OFFICE_XML_BYTES} bytes")
    return data


def _parse_xml(data: bytes) -> ET.Element:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise OfficeExtractionError("Office XML contains unsupported declarations")
    return ET.fromstring(data)


def _normalize_package_path(target: str) -> str:
    value = str(target or "").replace("\\", "/")
    if value.startswith("/"):
        value = value[1:]
    if not value.startswith("xl/"):
        value = posixpath.join("xl", value)
    return posixpath.normpath(value)


def _limit_text(text: str, max_bytes: int) -> OfficeText:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return OfficeText(text, False)
    return OfficeText(encoded[:max_bytes].decode("utf-8", errors="ignore"), True)


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]
