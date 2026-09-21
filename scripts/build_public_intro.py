"""Export reviewed user-facing material, without copying the development tree."""
from __future__ import annotations

import argparse
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    "README.md",
    "README_en.md",
    "LICENSE",
    "pycat/assets/pycat.svg",
    "docs/product/user-guide.md",
    "docs/product/user-guide_zh.md",
    "docs/product/developers.md",
    "docs/product/developers_zh.md",
    "media/pycat-hero.png",
    "media/pycat-architecture.png",
    "media/pycat-workflow.png",
    "media/pycat-connected.png",
    "media/screenshots/workspace.png",
    "media/screenshots/svg-preview.png",
    "media/screenshots/models.png",
    "media/screenshots/modes.png",
    "media/screenshots/settings.png",
    "media/screenshots/capture-ocr.png",
    "media/screenshots/ssh-workspace.png",
    "media/screenshots/wechat-image.jpg",
    "media/screenshots/image-generation.png",
    "media/screenshots/run-inspector.png",
)
LINKS = (
    re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)"),
    re.compile(r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    re.compile(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", re.MULTILINE),
)


def collect(root: Path) -> dict[str, bytes]:
    """Check files and local links before making an export or changing output."""
    root = root.resolve()
    files = {}
    for name in PUBLIC_FILES:
        path = root / name
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f"Missing or redirected public file: {name}")
        files[name] = path.read_bytes()

    for name, data in files.items():
        if not name.endswith(".md"):
            continue
        content = data.decode("utf-8")
        for pattern in LINKS:
            for match in pattern.finditer(content):
                target = match.group(1).strip("<>")
                url = urlsplit(target)
                if url.scheme in ("https", "http", "mailto"):
                    continue
                if url.scheme or url.netloc or "\\" in target or url.path.startswith("/"):
                    raise ValueError(f"Non-public link in {name}: {target}")
                if not url.path:
                    continue
                local = (root / name).parent / unquote(url.path)
                try:
                    relative = local.resolve().relative_to(root).as_posix()
                except ValueError:
                    relative = ""
                if relative not in files:
                    raise ValueError(f"Non-public link in {name}: {target}")
    return files


def build(root: Path) -> Path:
    """Write a presentation ZIP; this is not an application or source release."""
    files = collect(root)
    root = root.resolve()
    output_dir = root / "release"
    if output_dir.resolve() != output_dir:
        raise ValueError("Presentation output directory must not be redirected")
    output_dir.mkdir(exist_ok=True)
    output = output_dir / "pycat-introduction.zip"
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(f"pycat-introduction/{name}", data)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def write_directory(root: Path, directory: Path) -> int:
    """Add the reviewed, linked material to an existing portable distribution."""
    files = collect(root)
    directory = directory.absolute()
    targets = {name: directory / name for name in files}
    for name, path in targets.items():
        if path.resolve() != path:
            raise ValueError(f"Redirected public destination: {name}")
    for name, path in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[name])
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Validate without writing a ZIP")
    mode.add_argument("--directory", type=Path, help="Add reviewed material to a portable distribution")
    args = parser.parse_args()
    if args.check:
        print(f"Public introduction OK: {len(collect(ROOT))} reviewed files")
    elif args.directory is not None:
        print(f"Public introduction copied: {write_directory(ROOT, args.directory)} reviewed files")
    else:
        print(build(ROOT))


if __name__ == "__main__":
    main()
