"""One authorized candidate set for ripgrep and the bounded Python fallback."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pathspec import GitIgnoreSpec

MAX_FILE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 10
RIPGREP_BUNDLE = Path(__file__).resolve().parents[3] / 'assets/tools/ripgrep/windows-x64/rg.exe'


def find_ripgrep() -> str | None:
    if sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64') and RIPGREP_BUNDLE.is_file():
        return str(RIPGREP_BUNDLE)
    return shutil.which('rg')


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _candidate_batches(root, context, glob, deadline):
    def check_time():
        if time.monotonic() >= deadline:
            raise TimeoutError('File search timed out; narrow the path or glob')

    def load_rules(folder, inherited):
        rules = list(inherited)
        for name in ('.gitignore', '.ignore', '.rgignore'):
            path = folder / name
            try:
                if not _is_link(path) and path.stat().st_size <= 65536:
                    authorized = context.resolve_read_path(str(path))
                    rules.append((folder, GitIgnoreSpec.from_lines(authorized.read_text(encoding='utf-8').splitlines())))
            except (OSError, ValueError, UnicodeError):
                pass
        return rules

    def ignored(path, directory, rules):
        result = False
        for base, spec in rules:
            decision = spec.check_file(path.relative_to(base).as_posix() + ('/' if directory else '')).include
            if decision is not None:
                result = decision
        return result

    def matches(path):
        relative = path.relative_to(root).as_posix()
        return not glob or fnmatch.fnmatchcase(relative, glob) or (glob.startswith('**/') and fnmatch.fnmatchcase(relative, glob[3:]))

    glob = glob.replace('\\', '/')
    if glob and (Path(glob).anchor or '..' in Path(glob).parts):
        raise ValueError('Search glob must stay relative to the requested directory.')
    if root.is_file():
        if root.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('Search supports files up to 2 MiB; use file__read with a line range for this file.')
        if not glob or fnmatch.fnmatchcase(root.name, glob) or (glob.startswith('**/') and fnmatch.fnmatchcase(root.name, glob[3:])):
            yield [root]
        return

    if glob and not any(char in glob for char in '*?['):
        # A literal relative filename cannot match any unrelated subtree.
        folder, rules = root, []
        parts = Path(glob).parts
        for index, name in enumerate(parts):
            check_time()
            rules = load_rules(folder, rules)
            path = folder / name
            try:
                if name.startswith('.') or _is_link(path):
                    return
                authorized = context.resolve_read_path(str(path))
                directory = authorized.is_dir()
                if ignored(path, directory, rules):
                    return
                if index == len(parts) - 1:
                    if authorized.is_file() and authorized.stat().st_size <= MAX_FILE_BYTES:
                        yield [authorized]
                    return
                if not directory:
                    return
                folder = authorized
            except (OSError, ValueError):
                return
        return

    def walk(folder, inherited):
        check_time()
        rules = load_rules(folder, inherited)
        with os.scandir(folder) as entries:
            entries = sorted(entries, key=lambda entry: entry.name)
        directories, batch, size = [], [], 0
        for entry in entries:
            check_time()
            try:
                if entry.name.startswith('.') or entry.is_symlink():
                    continue
                path = Path(entry.path)
                directory = entry.is_dir(follow_symlinks=False)
                # Reject unrelated filenames before resolving/stat'ing them on Windows.
                if (not directory and not matches(path)) or _is_link(path):
                    continue
                if ignored(path, directory, rules):
                    continue
                authorized = context.resolve_read_path(str(path))
                if directory:
                    directories.append(authorized)
                elif authorized.is_file() and authorized.stat().st_size <= MAX_FILE_BYTES:
                    batch.append(authorized)
                    size += len(str(authorized)) + 3
                    if len(batch) >= 128 or size >= 18000:
                        yield batch
                        batch, size = [], 0
            except (OSError, ValueError):
                continue
        if batch:
            yield batch
        for directory in directories:
            yield from walk(directory, rules)
    yield from walk(root, [])


def _python_matches(paths, context, query, limit, deadline):
    matches = []
    for path in paths:
        if time.monotonic() >= deadline:
            raise TimeoutError('File search timed out; narrow the path or glob')
        try:
            authorized = context.resolve_read_path(str(path))
            with authorized.open('rb') as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES or b'\x00' in raw:
                continue
            for number, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), 1):
                if query in line:
                    matches.append({'path': context.display_path(path).replace('\\', '/'), 'line': number, 'text': line[:300]})
                    if len(matches) >= limit:
                        return matches
        except (OSError, ValueError):
            continue
    return matches


async def _rg_matches(executable, paths, root, context, query, regex, limit):
    args = [executable, '--json', '--no-config', '--sort', 'path', '--color=never', '--no-ignore', '--hidden', '--max-filesize', str(MAX_FILE_BYTES)]
    if not regex:
        args.append('--fixed-strings')
    targets = [str(context.resolve_read_path(str(path))) for path in paths]
    args.extend(['--', query, *(targets or ['-'])])
    process = await asyncio.create_subprocess_exec(
        *args, cwd=str(root), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=MAX_FILE_BYTES * 7, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    matches = []
    try:
        while raw := await process.stdout.readline():
            event = json.loads(raw)
            if event.get('type') != 'match':
                continue
            data = event['data']
            path = data['path'].get('text')
            line = data['lines'].get('text')
            if path is None or line is None:
                continue
            authorized = context.resolve_read_path(path)
            matches.append({'path': context.display_path(authorized).replace('\\', '/'), 'line': data['line_number'], 'text': line.rstrip('\r\n')[:300]})
            if len(matches) >= limit:
                return matches
        error = await process.stderr.read(8192)
        if await process.wait() not in (0, 1):
            raise ValueError('rg search failed: ' + error.decode('utf-8', errors='replace')[:500])
        return matches
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def search_files(*, root, context, query, regex=False, glob='', limit=50):
    executable = find_ripgrep()
    if regex and not executable:
        raise ValueError('Regex search requires ripgrep (rg); use literal text or install ripgrep.')
    deadline = time.monotonic() + TIMEOUT_SECONDS
    candidates = _candidate_batches(root, context, glob, deadline)
    directory = root.parent if root.is_file() else root
    matches = []
    searched = False
    timed_out = False
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            while paths := await asyncio.to_thread(next, candidates, []):
                searched = True
                remaining = limit + 1 - len(matches)
                if executable:
                    try:
                        found = await _rg_matches(executable, paths, directory, context, query, regex, remaining)
                    except FileNotFoundError:
                        if regex:
                            raise ValueError('Regex search requires ripgrep (rg); the executable is no longer available.') from None
                        executable = None
                        found = await asyncio.to_thread(_python_matches, paths, context, query, remaining, deadline)
                else:
                    found = await asyncio.to_thread(_python_matches, paths, context, query, remaining, deadline)
                matches.extend(found)
                if len(matches) > limit:
                    break
            if regex and not searched:
                await _rg_matches(executable, [], directory, context, query, True, 1)
    except TimeoutError:
        if not matches:
            raise
        timed_out = True
    result = {'matches': matches[:limit], 'truncated': timed_out or len(matches) > limit, 'backend': 'rg' if executable else 'python'}
    if timed_out:
        result.update(timed_out=True, warning='Partial results: search timed out; narrow the path or glob to search the remaining files.')
    return result
