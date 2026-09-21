"""Prepare a pinned, verified Windows x64 ripgrep bundle for development/builds.

This explicit build step is never invoked by an application search. Generated
binaries and licenses are ignored by Git; the upstream integrity manifest is not.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / 'pycat/assets/tools/ripgrep'
REQUIRED_FILES = {'rg.exe', 'COPYING', 'LICENSE-MIT', 'UNLICENSE'}
MAX_DOWNLOAD = 16 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024


def download_archive(url: str) -> bytes:
    if not url.startswith('https://'):
        raise ValueError('ripgrep download must use HTTPS')
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError('ripgrep archive exceeds download limit')
    return data


def verified(destination: Path, manifest: dict) -> bool:
    try:
        if json.loads((destination / 'manifest.json').read_text(encoding='utf-8')) != manifest:
            return False
        for name, expected in manifest['files'].items():
            with (destination / name).open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                    return False
        return True
    except (OSError, ValueError):
        return False


def prepare(destination: Path, *, manifest_path=RESOURCE / 'manifest.json', download=download_archive) -> Path:
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if manifest.get('platform') != 'windows-x64' or set(manifest.get('files', {})) != REQUIRED_FILES:
        raise ValueError('Invalid ripgrep platform or file manifest')
    if verified(destination, manifest):
        return destination
    data = download(manifest['url'])
    if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != manifest['archive_sha256']:
        raise ValueError('ripgrep archive SHA256 mismatch or size limit exceeded')
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name, expected in manifest['files'].items():
            try:
                entry = archive.getinfo(manifest['archive_root'] + '/' + name)
            except KeyError as exc:
                raise ValueError(f'ripgrep archive is missing {name}') from exc
            if entry.file_size > MAX_FILE:
                raise ValueError(f'ripgrep file exceeds limit: {name}')
            content = archive.read(entry)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f'ripgrep file SHA256 mismatch: {name}')
            files[name] = content
    # Never extract arbitrary members. Validate the complete bundle before writes.
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (destination / name).write_bytes(content)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8', newline='\n')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=RESOURCE / 'windows-x64')
    args = parser.parse_args()
    print(prepare(args.destination))
