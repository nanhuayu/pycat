"""Extract, validate and compile the native Qt application catalog.

Requires the GUI extra for pylupdate and QTranslator. Compilation uses Qt's
lrelease (or pyside6-lrelease), supplied explicitly or found on PATH.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from string import Formatter

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "pycat/assets/translations/pycat_en.ts"


def messages(path: Path) -> list[tuple[tuple[str, str, str], str, str]]:
    result = []
    for context in ET.parse(path).findall("context"):
        for message in context.findall("message"):
            translation = message.find("translation")
            result.append(((context.findtext("name", ""), message.findtext("source", ""),
                            message.findtext("comment", "")),
                           "" if translation is None else "".join(translation.itertext()),
                           "unfinished" if translation is None else translation.get("type", "")))
    return result


def placeholders(text: str) -> Counter:
    return Counter((field, spec, conversion) for _, field, spec, conversion in Formatter().parse(text)
                   if field is not None)


def validate(path: Path) -> list[str]:
    """Check only owned UI messages, never user content or untranslated pages."""
    errors, seen = [], set()
    for key, target, state in messages(path):
        context, source, _ = key
        label = f"{context}: {source!r}"
        if key in seen:
            errors.append(f"Duplicate message: {label}")
        seen.add(key)
        if state or not target.strip():
            errors.append(f"Unfinished translation: {label}")
            continue
        try:
            if placeholders(source) != placeholders(target):
                errors.append(f"Changed format placeholders: {label}")
        except ValueError as exc:
            errors.append(f"Invalid format string: {label}: {exc}")
        # Translators may change filter labels, but not file matching patterns.
        if re.findall(r"\([^)]*\*[^)]*\)", source) != re.findall(r"\([^)]*\*[^)]*\)", target):
            errors.append(f"Changed file filter: {label}")
    return errors


def extract(destination: Path) -> None:
    subprocess.run([sys.executable, "-m", "PyQt6.lupdate.pylupdate", str(ROOT / "pycat/gui"),
                    "--ts", str(destination), "--no-obsolete", "--no-summary"], cwd=ROOT, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Update TS sources with pylupdate6; keep translations")
    parser.add_argument("--compile", action="store_true", help="Compile a validated TS into its shipped QM")
    parser.add_argument("--lrelease", help="Path to Qt lrelease or pyside6-lrelease")
    args = parser.parse_args(argv)
    if args.update:
        extract(CATALOG)
        tree = ET.parse(CATALOG)
        for location in tree.findall(".//location"):
            location.set("filename", location.get("filename", "").replace("\\", "/"))
        ET.indent(tree, space="    ")
        tree.write(CATALOG, encoding="utf-8", xml_declaration=True)
        if not args.compile:
            print("Translation sources updated. Review the TS, then compile and check.")
            return 0

    errors = validate(CATALOG)
    # pylupdate writes relative source locations; Windows paths on different
    # drives cannot be made relative. Keep the disposable TS on the same drive.
    with tempfile.TemporaryDirectory(prefix="pycat-translations-", dir=CATALOG.parent) as directory:
        extracted = Path(directory) / "extracted.ts"
        extract(extracted)
        expected = {key for key, _, _ in messages(extracted)}
        actual = {key for key, _, _ in messages(CATALOG)}
        for key in sorted(expected - actual):
            errors.append(f"Missing source: {key}")
        for key in sorted(actual - expected):
            errors.append(f"Obsolete source: {key}")
    if errors:
        print("\n".join(errors))
        return 1

    compiled = CATALOG.with_suffix(".qm")
    if args.compile:
        compiler = args.lrelease or shutil.which("lrelease") or shutil.which("pyside6-lrelease")
        if not compiler:
            parser.error("Qt lrelease is required to compile; supply --lrelease PATH.")
        subprocess.run([compiler, str(CATALOG), "-qm", str(compiled)], check=True)

    from PyQt6.QtCore import QCoreApplication, QTranslator
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    if not translator.load(str(compiled)):
        print(f"Cannot load compiled catalog: {compiled}")
        return 1
    for (context, source, comment), target, _ in messages(CATALOG):
        # QTranslator's char* source overload expects UTF-8 bytes in PyQt;
        # QCoreApplication.translate handles this conversion at normal call sites.
        if translator.translate(context, source.encode("utf-8"), comment or None) != target:
            errors.append(f"Compiled catalog is stale: {context}: {source!r}")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Translation check passed: {len(actual)} messages; sources, placeholders and QM agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
