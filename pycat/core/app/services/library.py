"""Library metadata and explicitly imported bytes, with no transcript or memory store."""
from __future__ import annotations

import math
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from pycat.core.content.mime import guess_mime, is_text_mime
from pycat.core.content.resolver import ResolvedContent, SessionContentResolver
from pycat.models.contracts.content import ContentRef
from pycat.models.conversation import Conversation
from pycat.models.workspace import WorkspaceLocation


class LibraryService:
    IMPORT_LIMIT = 128 * 1024 * 1024

    def __init__(self, repository, *, content):
        self.repository, self.content = repository, content
        self.on_change = None

    def _changed(self, result=None):
        if self.on_change is not None:
            self.on_change('', '', ('library',))
        return result

    @staticmethod
    def _title(title):
        value = str(title).strip()
        if not value or len(value) > 256:
            raise ValueError('Title must contain 1–256 characters')
        return value

    def topics(self, parent_id='', **page):
        return self.repository.topics(parent_id, **page)

    def create_topic(self, title, parent_id=''):
        return self._changed(self.repository.create_topic(self._title(title), parent_id))

    def rename_topic(self, identifier, title):
        self.repository.update_topic(identifier, title=self._title(title))
        self._changed()

    def move_topic(self, identifier, parent_id=''):
        self.repository.update_topic(identifier, parent_id=parent_id)
        self._changed()

    def delete_topic(self, identifier):
        self.repository.delete_topic(identifier)
        self._changed()

    def items(self, **query):
        return self.repository.items(**query)

    def get(self, identifier):
        return self.repository.get(identifier)

    def view_state(self, key):
        return self.repository.view_state(key)

    def save_view_state(self, key, value):
        self.repository.save_view_state(key, value)

    def find(self, ref, *, evidence=False):
        return self.repository.find_identity(self.identity(ref) + (':evidence:' + ref.digest if evidence else ''))

    def item_for_path(self, path):
        candidate = Path(path).resolve()
        if not candidate.is_relative_to((self.repository.root / 'files').resolve()):
            return None
        try:
            item = self.get(candidate.name[:32])
            return item if item.owned and self.repository.owned_path(item.id) == candidate else None
        except ValueError:
            return None

    def move_item(self, identifier, topic_id=''):
        return self._changed(self.repository.update_item(identifier, topic_id=topic_id))

    def set_favorite(self, identifier, favorite):
        return self._changed(self.repository.update_item(identifier, favorite=bool(favorite)))

    def add_reference(self, ref, *, title='', topic_id='', favorite=False, evidence=False):
        if not isinstance(ref, ContentRef):
            ref = ContentRef.from_dict(ref)
        if ref.kind not in {'file', 'workspace', 'wiki', 'artifact', 'archive', 'input', 'library'}:
            raise ValueError('Unsupported library reference')
        if ref.kind == 'library' and not evidence:
            item = self.get(ref.id)
            if favorite:
                item = self.set_favorite(item.id, True)
            return item
        # Workspace/files and Wiki follow their current source. Evidence remains versioned.
        identity = self.identity(ref)
        if evidence:
            if not ref.digest:
                raise ValueError('Evidence requires a content version')
            identity += ':evidence:' + ref.digest
        return self._changed(self.repository.add(ref, identity=identity, title=self._title(title or ref.name),
                                                topic_id=topic_id, favorite=favorite, evidence=evidence))

    @staticmethod
    def identity(ref):
        if ref.kind == 'library':
            return 'library:' + ref.id
        if ref.kind == 'file' or (ref.kind == 'workspace' and not WorkspaceLocation.parse(ref.workspace).is_remote):
            path = Path(ref.ref) if ref.kind == 'file' else Path(ref.workspace) / ref.ref.removeprefix('workspace:')
            return 'file:' + os.path.normcase(str(path.resolve()))
        version = '' if ref.kind in {'workspace', 'wiki', 'library'} else ref.digest
        return '\0'.join((ref.kind, ref.workspace, ref.conversation_id if ref.kind in {'input', 'artifact', 'archive'} else '', ref.ref, version))

    def add_file(self, source, *, topic_id='', favorite=False):
        path = Path(source).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError('Select a file')
        ref = ContentRef(str(path), path.name, guess_mime(path.name), path.stat().st_size, '',
                         str(path), kind='file', source='library')
        return self.add_reference(ref, topic_id=topic_id, favorite=favorite)

    def create_file(self, name, *, topic_id='', favorite=False):
        """Create an empty UTF-8 document in the existing owned-file store."""
        name = self._title(name)
        if any(character in name for character in '/\\:*?"<>|') or any(ord(c) < 32 for c in name):
            raise ValueError('Enter a file name, not a path')
        if not Path(name).suffix:
            name = self._title(name + '.md')
        if not is_text_mime(guess_mime(name)):
            raise ValueError('Choose a text format such as .md, .txt or .csv')
        with self._owned_destination(name) as (identifier, destination):
            item = self._publish_owned(identifier, name, destination, topic_id=topic_id, favorite=favorite)
        return self._changed(item)

    @contextmanager
    def _owned_destination(self, name):
        identifier = uuid.uuid4().hex
        suffix = re.sub(r'[^A-Za-z0-9._-]', '', ''.join(Path(name).suffixes)[-32:])
        directory = self.repository.root / 'files'
        directory.mkdir(exist_ok=True)
        destination = directory / (identifier + suffix)
        # Reserve atomically before taking responsibility for cleanup.
        destination.touch(exist_ok=False)
        try:
            yield identifier, destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def _publish_owned(self, identifier, name, destination, *, topic_id, favorite):
        ref = ContentRef(identifier, name, guess_mime(name), destination.stat().st_size,
                         SessionContentResolver._digest(destination), f'library:{identifier}',
                         kind='library', source='library')
        return self.repository.add(ref, identity=f'library:{identifier}', identifier=identifier,
                                   title=self._title(name), topic_id=topic_id,
                                   favorite=favorite, owned_name=destination.name)

    def import_file(self, source, *, topic_id='', favorite=False, name=''):
        path = Path(source).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size > self.IMPORT_LIMIT:
            raise ValueError('Import requires a file no larger than 128 MiB')
        display_name = Path(name or path.name).name
        with self._owned_destination(display_name) as (identifier, destination):
            before = path.stat()
            with path.open('rb') as origin, destination.open('wb') as output:
                copied = 0
                while chunk := origin.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > self.IMPORT_LIMIT:
                        raise ValueError('File grew beyond the import budget')
                    output.write(chunk)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('File changed during import; no copy was published')
            if destination.stat().st_size > self.IMPORT_LIMIT:
                raise ValueError('File grew during import')
            item = self._publish_owned(identifier, display_name, destination, topic_id=topic_id, favorite=favorite)
        return self._changed(item)

    def resolve(self, identifier):
        item = self.get(identifier)
        if item.owned:
            path = self.repository.owned_path(identifier)
            if not path.is_file():
                raise FileNotFoundError(str(path))
            return ResolvedContent(path, replace(item.ref, size=path.stat().st_size,
                                                 digest=SessionContentResolver._digest(path)))
        ref = item.ref
        if ref.kind == 'file':
            path = Path(ref.ref).resolve(strict=True)
            if not path.is_file():
                raise ValueError('Source is not a file')
            digest = SessionContentResolver._digest(path)
            if item.evidence:
                SessionContentResolver._check_digest(ref.digest, digest)
            return ResolvedContent(path, replace(ref, digest=digest, size=path.stat().st_size))
        current = ref.kind in {'workspace', 'wiki'} and not item.evidence
        return SessionContentResolver(self.content).resolve_content(
            Conversation(id=ref.conversation_id, work_dir=ref.workspace),
            replace(ref, digest='') if current else ref, verify_digest=not current)

    def refresh_owned(self, identifier):
        resolved = self.resolve(identifier)
        if self.get(identifier).owned:
            self.repository.update_item(identifier, ref=resolved.ref)
            self._changed()
        return resolved

    def remove(self, identifier):
        # Removing a link never deletes the original. Imported bytes belong to this index.
        item = self.get(identifier)
        path = self.repository.owned_path(identifier) if item.owned else None
        self.repository.remove(identifier)
        if path is not None:
            path.unlink(missing_ok=True)
        self._changed()

    def annotations(self, identifier, digest):
        return self.repository.annotations(identifier, digest)

    def set_annotations(self, identifier, digest, annotations):
        self.get(identifier)
        if not digest or len(annotations) > 100:
            raise ValueError('Annotations require a content version and at most 100 regions')
        normalized = []
        for entry in annotations:
            rect = entry.get('rect', [])
            if len(rect) != 4 or any(type(n) not in {int, float} or not math.isfinite(n) for n in rect):
                raise ValueError('Invalid annotation rectangle')
            x, y, width, height = rect
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
                raise ValueError('Annotation rectangle must fit within the image')
            color = str(entry.get('color') or '#2684ff')
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                raise ValueError('Invalid annotation color')
            normalized.append({'rect': list(rect), 'note': str(entry.get('note') or '')[:4000], 'color': color})
        self.repository.set_annotations(identifier, digest, normalized)
        self._changed()
