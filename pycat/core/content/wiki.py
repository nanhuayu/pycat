"""Workspace knowledge pages with evidence, atomic publication and a small index."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from pycat.core.content.markdown import parse_frontmatter, render_frontmatter
from pycat.core.persistence import atomic_write_text, exclusive_file_lock
from pycat.core.security.threats import first_threat_message
from pycat.models.contracts.content import ContentRef
from pycat.models.conversation import Conversation
from pycat.models.session_paths import normalize_work_dir, resolve_project_data_root
from pycat.models.workspace import workspace_identity

_HEADER = re.compile(r"\A<!-- pycat-wiki-v1 (.*?) -->\r?\n", re.DOTALL)
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
MAX_PAGE_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


class WikiService:
    """Owns pages only; artifacts, memory and archives retain their own owners."""

    def __init__(self, resolver) -> None:
        self.resolver = resolver
        self._cache: OrderedDict[str, tuple[int, list[dict]]] = OrderedDict()
        self._lock = threading.RLock()

    def root(self, work_dir: str) -> Path:
        if not normalize_work_dir(work_dir):
            raise ValueError("project knowledge requires an active workspace")
        return resolve_project_data_root(work_dir, data_dir=getattr(self.resolver, "data_dir", None)).resolve() / "wiki"

    def path_for(self, work_dir: str, page_id: str) -> Path:
        if not _ID.fullmatch(str(page_id)):
            raise ValueError("invalid knowledge page id")
        root = self.root(work_dir).resolve()
        path = (root / f"{page_id}.md").resolve()
        path.relative_to(root)
        return path

    @staticmethod
    def _read(path: Path) -> dict:
        if path.stat().st_size > MAX_PAGE_BYTES:
            raise ValueError("knowledge page exceeds 1 MiB")
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        legacy = _HEADER.match(text)
        if legacy:
            # Deserialize old documents here only; every publication writes OKF.
            data = json.loads(legacy[1])
            if not isinstance(data, dict) or data.get("id") != path.stem:
                raise ValueError("knowledge page identity mismatch")
            if any(not isinstance(data.get(key), str) or not data[key] for key in ("title", "summary")) or not isinstance(data.get("sources"), list):
                raise ValueError("knowledge metadata fields are unreadable")
            data["validation"] = "unverified" if data.get("validation") == "supported" else data.get("validation", "unverified")
            return {**data, "body": text[legacy.end():], "digest": hashlib.sha256(raw).hexdigest(), "_frontmatter": {}}
        metadata, body = parse_frontmatter(text, strict=True)
        if not isinstance(metadata.get("type"), str) or not metadata["type"].strip():
            raise ValueError("knowledge requires OKF frontmatter with a type")
        sources = metadata.get("sources", [])
        if not isinstance(sources, list) or any(not isinstance(ref, dict) or not ref.get("resource") for ref in sources):
            raise ValueError("knowledge sources require resource references")
        extension = metadata.get("pycat") or {}
        generated = metadata.get("generated") or {}
        verified = metadata.get("verified") or []
        if not isinstance(extension, dict) or not isinstance(generated, dict) or not isinstance(verified, (list, dict)):
            raise ValueError("knowledge provenance must use mappings and verification events")
        verified = [verified] if isinstance(verified, dict) else verified
        trust = "unverified"
        if verified:
            trust = "human-reviewed" if any(str(item.get("by", "")).startswith("human:") for item in verified if isinstance(item, dict)) else "machine-confirmed"
        return {"id": path.stem, "title": str(metadata.get("title") or path.stem),
                "summary": str(metadata.get("description") or ""), "body": body,
                "sources": [dict(ref.get("pycat") or ref) for ref in sources],
                "validation": extension.get("validation") or trust, "trust": trust,
                "status": metadata.get("status", "stable"), "updated_at": generated.get("at", ""),
                "operation_id": extension.get("operation_id", ""), "_frontmatter": metadata,
                "digest": hashlib.sha256(raw).hexdigest()}

    @staticmethod
    def _source_metadata(source: dict) -> dict:
        if source.get("resource") and not source.get("kind"):
            return dict(source)
        owner = str(source.get("conversation_id") or "")
        resource = f"pycat:{source.get('kind', 'archive')}/{owner}/{source.get('id', '')}"
        return {"resource": resource, "pycat": source}

    def _source_errors(self, work_dir: str, sources: list[dict]) -> list[str]:
        errors = []
        conversation = Conversation(work_dir=work_dir)
        for source in sources:
            if source.get("resource") and not source.get("kind"):
                continue
            try:
                ref = ContentRef.from_dict(source)
                if not ref.digest or not ref.workspace:
                    raise ValueError("evidence requires a pinned digest and workspace")
                if workspace_identity(ref.workspace) != workspace_identity(work_dir):
                    raise ValueError("evidence belongs to another workspace")
                self.resolver.resolve_content(conversation, ref)
            except (OSError, ValueError) as exc:
                errors.append(f"{source.get('name') or source.get('id')}: {exc}")
        return errors

    @staticmethod
    def _source_keys(sources) -> set[tuple]:
        return {tuple(str(ref.get(key, "")) for key in ("kind", "workspace", "conversation_id", "id", "digest", "locator", "resource", "ref"))
                for ref in sources}

    def _index(self, work_dir: str, *, reindex: bool = False) -> list[dict]:
        root = self.root(work_dir)
        key = str(root)
        stamp = root.stat().st_mtime_ns if root.exists() else 0
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == stamp and not reindex:
                self._cache.move_to_end(key)
                return cached[1]
            pages = self._load_index(root, stamp) if not reindex else None
            if pages is None:
                pages = []
                for path in root.glob("*.md") if root.exists() else ():
                    try:
                        pages.append(self._read(path))
                    except (OSError, ValueError) as exc:
                        pages.append({"id": path.stem, "title": path.stem, "summary": f"存储不可读：{exc}",
                                      "body": "", "digest": "", "validation": "stale", "sources": [], "error": str(exc)})
                if root.exists():
                    self._write_index(root, stamp, pages=pages)
            self._cache[key] = (stamp, pages)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
            return pages

    @staticmethod
    def _load_index(root: Path, stamp: int) -> list[dict] | None:
        path = root.parent / "wiki-index.sqlite3"
        if not path.exists():
            return None
        try:
            db = sqlite3.connect(path, timeout=1)
            try:
                version = db.execute("SELECT value FROM metadata WHERE name='stamp-v2'").fetchone()
                if version and version[0] == str(stamp):
                    return [json.loads(row[0]) for row in db.execute("SELECT payload FROM pages")]
            finally:
                db.close()
        except (OSError, ValueError, sqlite3.Error):
            pass
        return None

    @staticmethod
    def _write_index(root: Path, stamp: int, *, pages=None, page=None, deleted: str = "") -> None:
        """Disposable query cache; failure never rolls back canonical Markdown."""
        target = root.parent / "wiki-index.sqlite3"
        temporary = None
        try:
            if pages is not None:
                fd, name = tempfile.mkstemp(prefix=".wiki-index-", suffix=".sqlite3", dir=root.parent)
                os.close(fd)
                temporary = Path(name)
            db = sqlite3.connect(temporary or target, timeout=1)
            try:
                with db:
                    db.execute("CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                    db.execute("CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    if pages is not None:
                        db.execute("DELETE FROM pages")
                        db.executemany("INSERT INTO pages VALUES(?,?)", [(item["id"], json.dumps(item, ensure_ascii=False)) for item in pages])
                    if page is not None:
                        db.execute("INSERT OR REPLACE INTO pages VALUES(?,?)", (page["id"], json.dumps(page, ensure_ascii=False)))
                    if deleted:
                        db.execute("DELETE FROM pages WHERE id=?", (deleted,))
                    db.execute("INSERT OR REPLACE INTO metadata VALUES('stamp-v2',?)", (str(stamp),))
            finally:
                db.close()
            if temporary is not None:
                os.replace(temporary, target)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Knowledge index will be rebuilt: %s", exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def count(self, work_dir: str, query: str = "", *, reindex: bool = False) -> int:
        terms = str(query or "").casefold().split()
        return sum(all(term in " ".join(str(page.get(key) or "").casefold()
                   for key in ("title", "summary", "body")) for term in terms)
                   for page in self._index(work_dir, reindex=reindex))

    def search(self, work_dir: str, query: str = "", *, limit: int = 50, offset: int = 0, reindex: bool = False) -> list[dict]:
        if not normalize_work_dir(work_dir):
            return []
        terms = str(query or "").casefold().split()
        rows = []
        for page in self._index(work_dir, reindex=reindex):
            title, summary, body = (str(page.get(key) or "").casefold() for key in ("title", "summary", "body"))
            if terms and not all(term in title + " " + summary + " " + body for term in terms):
                continue
            score = sum(4 * title.count(term) + 2 * summary.count(term) + bool(term in body) for term in terms)
            rows.append((score, page))
        rows.sort(key=lambda item: (item[0], item[1].get("updated_at", ""), item[1]["id"]), reverse=True)
        result = []
        for _, page in rows[max(0, offset):max(0, offset) + min(1000, max(1, limit))]:
            row = {key: value for key, value in page.items() if key not in {"body", "operation_id", "_frontmatter"}}
            row["ref"] = self.content_ref(work_dir, page).to_dict()
            result.append(row)
        return result

    def content_ref(self, work_dir: str, page: dict) -> ContentRef:
        return ContentRef(id=page["id"], name=page["title"], mime="text/markdown", size=len(page.get("body", "")),
                          digest=page.get("digest", ""), ref=f"wiki:{page['id']}", kind="wiki", source="wiki",
                          status=page.get("validation", "unverified"), workspace=workspace_identity(work_dir))

    def review_context(self, work_dir: str, evidence: str, *, char_budget: int = 16000) -> list[dict]:
        """Supply a few relevant complete pages through the existing index."""
        if not normalize_work_dir(work_dir):
            return []
        def terms(text):
            words = set(re.findall(r"[a-z0-9_]{3,}", text.casefold()))
            for run in re.findall(r"[\u4e00-\u9fff]+", text):
                words.update(run[index:index + 2] for index in range(len(run) - 1))
            return words
        wanted = terms(evidence)
        ranked = []
        for page in self._index(work_dir):
            if page.get("error") or page.get("status") == "deprecated":
                continue
            score = len(wanted & terms(page["title"])) * 4 + len(wanted & terms(page["summary"]))
            if score:
                ranked.append((score, page))
        result = []
        for _, page in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:3]:
            complete = len(page["body"]) <= char_budget
            result.append({key: page[key] for key in ("id", "title", "summary", "digest", "validation")}
                          | {"body": page["body"] if complete else "", "complete": complete})
            if complete:
                char_budget -= len(page["body"])
        return result

    def read(self, work_dir: str, page_id: str) -> dict:
        page = self._read(self.path_for(work_dir, page_id))
        errors = self._source_errors(work_dir, page.get("sources", []))
        page["source_errors"] = errors
        if errors:
            page["validation"] = "stale"
        page["ref"] = self.content_ref(work_dir, page).to_dict()
        return page

    def apply(self, work_dir: str, payload: dict, *, operation_id: str = "") -> tuple[bool, str]:
        try:
            title, summary, body = (str(payload.get(key) or "").strip() for key in ("title", "summary", "body"))
            if not title or not body:
                raise ValueError("provide a title and a complete Markdown body")
            if not summary:
                summary = title
            threat = first_threat_message("\n".join((title, summary, body)), scope="strict")
            if threat:
                raise ValueError(threat)
            sources = payload.get("sources")
            if not isinstance(sources, list) or not 1 <= len(sources) <= 16 or any(not isinstance(ref, dict) for ref in sources):
                raise ValueError("provide 1–16 versioned source references")
            slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:64].rstrip("-")
            page_id = str(payload.get("id") or slug or "note-" + hashlib.sha256(title.encode()).hexdigest()[:12])
            path = self.path_for(work_dir, page_id)
            # The lock is workspace-local so simultaneous source promotions deduplicate.
            with exclusive_file_lock(self.root(work_dir) / ".publish"):
                self._index(work_dir)
                existing = self._read(path) if path.exists() else None
                if existing and operation_id and existing.get("operation_id") == operation_id:
                    return True, page_id
                if payload.get("expected_digest") is not None and (existing or {}).get("digest", "") != payload["expected_digest"]:
                    raise ValueError("knowledge changed; reload before editing")
                if existing and not payload.get("expected_digest"):
                    if (existing["title"], existing["summary"], existing["body"].strip(), self._source_keys(existing["sources"])) == (title, summary, body, self._source_keys(sources)):
                        return True, page_id
                    raise ValueError("knowledge already exists; read its digest before updating")
                errors = self._source_errors(work_dir, sources)
                if errors:
                    raise ValueError("; ".join(errors))
                validation = str(payload.get("validation") or "unverified")
                if validation not in {"unverified", "conflicted", "stale"}:
                    raise ValueError("invalid knowledge validation state")
                metadata = dict((existing or {}).get("_frontmatter") or {})
                metadata.pop("verified", None)  # Authorship and source availability are not verification.
                metadata.update({"type": metadata.get("type") or "Reference", "title": title, "description": summary,
                                 "sources": [self._source_metadata(ref) for ref in sources],
                                 "generated": {"by": "process:pycat", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                 "status": "draft"})
                extension = dict(metadata.get("pycat") or {})
                extension.pop("validation", None)
                extension.pop("operation_id", None)
                if validation != "unverified":
                    extension["validation"] = validation
                if operation_id:
                    extension["operation_id"] = operation_id
                metadata.pop("pycat", None)
                if extension:
                    metadata["pycat"] = extension
                text = render_frontmatter(metadata) + body + "\n"
                if len(text.encode("utf-8")) > MAX_PAGE_BYTES:
                    raise ValueError("knowledge page and metadata exceed 1 MiB; split into linked topics")
                atomic_write_text(path, text)
                with self._lock:
                    self._write_index(self.root(work_dir), self.root(work_dir).stat().st_mtime_ns, page=self._read(path))
                    self._cache.pop(str(self.root(work_dir)), None)
                return True, page_id
        except (OSError, ValueError, TypeError) as exc:
            return False, str(exc)

    def delete(self, work_dir: str, page_id: str, *, expected_digest: str) -> None:
        path = self.path_for(work_dir, page_id)
        with exclusive_file_lock(self.root(work_dir) / ".publish"):
            self._index(work_dir)
            if self._read(path)["digest"] != expected_digest:
                raise ValueError("knowledge changed; reload before deleting")
            path.unlink()
            with self._lock:
                self._write_index(self.root(work_dir), self.root(work_dir).stat().st_mtime_ns, deleted=page_id)
                self._cache.pop(str(self.root(work_dir)), None)
