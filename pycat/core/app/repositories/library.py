"""One SQLite index for library topics, references, favorites and overlays."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pycat.models.contracts.content import ContentRef, MaterialPage
from pycat.models.contracts.library import LibraryItem, LibraryTopic


def _now():
    return datetime.now(timezone.utc).isoformat()


class LibraryRepository:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / 'library'
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.path = self.root / 'index.sqlite3'
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS topics (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    parent_id TEXT REFERENCES topics(id) ON DELETE SET NULL);
                CREATE INDEX IF NOT EXISTS topics_parent ON topics(parent_id);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    topic_id TEXT REFERENCES topics(id) ON DELETE SET NULL,
                    favorite INTEGER NOT NULL DEFAULT 0, owned_name TEXT,
                    ref TEXT NOT NULL, updated_at TEXT NOT NULL, evidence INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS items_topic ON items(topic_id);
                CREATE INDEX IF NOT EXISTS items_favorite ON items(favorite);
                CREATE TABLE IF NOT EXISTS annotations (
                    item_id TEXT REFERENCES items(id) ON DELETE CASCADE, digest TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(item_id, digest));
                CREATE TABLE IF NOT EXISTS view_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10)
            try:
                with db:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA foreign_keys=ON')
                    yield db
            finally:
                db.close()

    @staticmethod
    def _topic_exists(db, identifier):
        if identifier and not db.execute('SELECT 1 FROM topics WHERE id=?', (identifier,)).fetchone():
            raise ValueError('Topic not found')

    def topics(self, parent_id='', *, offset=0, limit=200):
        with self.transaction() as db:
            rows = db.execute('''SELECT t.*, EXISTS(SELECT 1 FROM topics c WHERE c.parent_id=t.id) AS children
                FROM topics t WHERE parent_id IS ? ORDER BY title COLLATE NOCASE, id LIMIT ? OFFSET ?''',
                (parent_id or None, min(200, max(1, limit)), max(0, offset)))
            return [LibraryTopic(row['id'], row['title'], row['parent_id'] or '', bool(row['children'])) for row in rows]

    def create_topic(self, title, parent_id=''):
        identifier = uuid.uuid4().hex
        with self.transaction() as db:
            self._topic_exists(db, parent_id)
            db.execute('INSERT INTO topics VALUES (?,?,?)', (identifier, title, parent_id or None))
        return LibraryTopic(identifier, title, parent_id)

    def update_topic(self, identifier, *, title=None, parent_id=None):
        with self.transaction() as db:
            self._topic_exists(db, identifier)
            if parent_id is not None:
                self._topic_exists(db, parent_id)
                cycle = db.execute('''WITH RECURSIVE subtree(id) AS (
                    SELECT id FROM topics WHERE id=? UNION ALL
                    SELECT t.id FROM topics t JOIN subtree s ON t.parent_id=s.id)
                    SELECT 1 FROM subtree WHERE id=?''', (identifier, parent_id)).fetchone()
                if cycle:
                    raise ValueError('Topic move would create a cycle')
                db.execute('UPDATE topics SET parent_id=? WHERE id=?', (parent_id or None, identifier))
            if title is not None:
                db.execute('UPDATE topics SET title=? WHERE id=?', (title, identifier))

    def delete_topic(self, identifier):
        with self.transaction() as db:
            self._topic_exists(db, identifier)
            # Promote immediate children; never delete a subtree or source files.
            db.execute('UPDATE topics SET parent_id=(SELECT parent_id FROM topics WHERE id=?) WHERE parent_id=?',
                       (identifier, identifier))
            db.execute('UPDATE items SET topic_id=(SELECT parent_id FROM topics WHERE id=?) WHERE topic_id=?',
                       (identifier, identifier))
            db.execute('DELETE FROM topics WHERE id=?', (identifier,))

    @staticmethod
    def _item(row):
        return LibraryItem(row['id'], row['title'], ContentRef.from_dict(json.loads(row['ref'])),
                           row['topic_id'] or '', bool(row['favorite']), bool(row['owned_name']), row['updated_at'], bool(row['evidence']))

    def get(self, identifier):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('Library item not found')
            return self._item(row)

    def find_identity(self, identity):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM items WHERE identity=?', (identity,)).fetchone()
            return self._item(row) if row else None

    def owned_path(self, identifier):
        with self.transaction() as db:
            row = db.execute('SELECT owned_name FROM items WHERE id=?', (identifier,)).fetchone()
            if not row or not row['owned_name']:
                raise ValueError('Library item is a reference')
            path = (self.root / 'files' / row['owned_name']).resolve()
            if not path.is_relative_to((self.root / 'files').resolve()):
                raise ValueError('Invalid library path')
            return path

    def add(self, ref, *, identity, title, topic_id='', favorite=False, identifier=None, owned_name=None, evidence=False):
        with self.transaction() as db:
            self._topic_exists(db, topic_id)
            row = db.execute('SELECT * FROM items WHERE identity=?', (identity,)).fetchone()
            if row:
                if favorite:
                    db.execute('UPDATE items SET favorite=1 WHERE id=?', (row['id'],))
                return self._item(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())
            identifier = identifier or uuid.uuid4().hex
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)',
                       (identifier, identity, title, topic_id or None, int(favorite), owned_name,
                        json.dumps(ref.to_dict(), ensure_ascii=False), _now(), int(evidence)))
        return self.get(identifier)

    def update_item(self, identifier, *, favorite=None, topic_id=None, ref=None):
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM items WHERE id=?', (identifier,)).fetchone():
                raise ValueError('Library item not found')
            if topic_id is not None:
                self._topic_exists(db, topic_id)
                db.execute('UPDATE items SET topic_id=? WHERE id=?', (topic_id or None, identifier))
            if favorite is not None:
                db.execute('UPDATE items SET favorite=? WHERE id=?', (int(favorite), identifier))
            if ref is not None:
                db.execute('UPDATE items SET ref=? WHERE id=?', (json.dumps(ref.to_dict(), ensure_ascii=False), identifier))
            db.execute('UPDATE items SET updated_at=? WHERE id=?', (_now(), identifier))
        return self.get(identifier)

    def items(self, *, topic_id='', favorites=False, query='', offset=0, limit=100):
        limit, offset = min(200, max(1, int(limit))), max(0, int(offset))
        clauses, arguments = [], []
        if topic_id:
            clauses.append('topic_id IN (SELECT id FROM subtree)')
            arguments.append(topic_id)
        if favorites:
            clauses.append('favorite=1')
        if query:
            clauses.append("title LIKE ? ESCAPE '\\'")
            arguments.append('%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        prefix = '''WITH RECURSIVE subtree(id) AS (SELECT id FROM topics WHERE id=? UNION ALL
            SELECT t.id FROM topics t JOIN subtree s ON t.parent_id=s.id) ''' if topic_id else ''
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.transaction() as db:
            total = db.execute(prefix + 'SELECT COUNT(*) FROM items' + where, arguments).fetchone()[0]
            rows = db.execute(prefix + 'SELECT * FROM items' + where + ' ORDER BY updated_at DESC, id LIMIT ? OFFSET ?',
                              [*arguments, limit, offset])
            return MaterialPage([self._item(row).to_dict() for row in rows], total, offset, limit)

    def annotations(self, identifier, digest):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM annotations WHERE item_id=? AND digest=?', (identifier, digest)).fetchone()
            return json.loads(row[0]) if row else []

    def set_annotations(self, identifier, digest, annotations):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO annotations VALUES (?,?,?)',
                       (identifier, digest, json.dumps(annotations, ensure_ascii=False)))

    def remove(self, identifier):
        with self.transaction() as db:
            db.execute('DELETE FROM items WHERE id=?', (identifier,))
            db.execute('DELETE FROM view_state WHERE key=?', ('reading:' + identifier,))

    def view_state(self, key):
        with self.transaction() as db:
            row = db.execute('SELECT value FROM view_state WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_view_state(self, key, value):
        encoded = json.dumps(value, ensure_ascii=False)
        if len(key) > 1024 or len(encoded.encode('utf-8')) > 32768:
            raise ValueError('View metadata exceeds its budget')
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO view_state VALUES (?,?)', (key, encoded))
