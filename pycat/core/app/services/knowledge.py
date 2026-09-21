"""Application use cases for memory and project material projections."""
from __future__ import annotations


from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.llm.token_budget import estimate_tokens
from pycat.core.content.references import material_rows, content_identity
from pycat.core.memory.service import MemoryService, memory_enabled
from pycat.core.memory.review import MemoryReviewService
from pycat.models.contracts.content import ContentRef, MaterialPage
from pycat.models.session_paths import has_active_workspace


class KnowledgeService:
    def __init__(self, *, wiki_service, worker, capability_executor, resolver, on_change, data_dir: str | None = None) -> None:
        self.data_dir = data_dir
        self.wiki = wiki_service
        self.worker = worker
        self.executor = capability_executor
        self.resolver = resolver
        self._changed = on_change

    def memory_snapshot(self, work_dir: str, *, enabled: bool = True) -> dict:
        targets = MemoryService.describe_targets(work_dir, data_dir=self.data_dir)
        evolution = MemoryService.describe_evolution(work_dir, data_dir=self.data_dir)
        count = sum(len(target["records"]) for target in targets)
        readable = evolution["available"] and all(target["readable"] for target in targets)
        if not enabled:
            status = "未启用"
        elif not readable:
            status = "存储不可读"
        elif evolution["inflight"]:
            status = "正在整理"
        elif evolution["failure_count"]:
            prefix = "部分已保存" if evolution["published_job_count"] else "整理待处理"
            status = f"{prefix} · {evolution['attention_count']} 项待处理"
        elif evolution["pending_count"]:
            status = "等待整理"
        elif count:
            status = f"已记住 {count} 项"
        else:
            log = evolution.get("review_log") or []
            status = "最近整理无新增" if log and log[0].get("outcome") == "no_change" else "暂无记忆"
        return {"workspace": work_dir, "targets": targets, "evolution": evolution, "count": count,
                "readable": readable, "enabled": enabled, "status": status,
                "unassigned_count": len(MemoryService.legacy_project_memory(data_dir=self.data_dir)["records"]) if work_dir else 0}

    def assign_legacy_memory(self, conversation) -> tuple[bool, str]:
        if not memory_enabled(conversation):
            return False, "请先启用会话记忆。"
        result = MemoryService.assign_legacy_project_memory(conversation.work_dir, data_dir=self.data_dir)
        if result[0]:
            self._changed(conversation.work_dir, conversation.id, ("memory",))
        return result

    def edit_memory(self, conversation, target: str, *, entry_id: str = "", text: str = "",
                    expected_digest: str, forget: bool = False) -> tuple[bool, str]:
        result = MemoryService.handle_tool_action(work_dir=conversation.work_dir,
                action="remove" if forget else "replace" if entry_id else "add", target=target,
                content=text, new_text=text, entry_id=entry_id, expected_digest=expected_digest,
                conversation=conversation, data_dir=self.data_dir)
        if result[0]:
            self._changed(conversation.work_dir, conversation.id, ("memory",))
        return result

    def retry_memory(self, work_dir: str) -> int:
        count = self.worker.retry(work_dir)
        self._changed(work_dir, "", ("memory",))
        return count

    def source_fragment(self, ref: ContentRef) -> str:
        text = MemoryReviewService.read_fragment(ref.to_dict(), data_dir=self.data_dir)
        if not text:
            raise ValueError("来源片段不存在，请重新核实。")
        return text

    def materials(self, conversation, *, kind: str = "all", query: str = "", offset: int = 0,
                  limit: int = 50, reindex: bool = False) -> MaterialPage:
        if kind not in {"all", "artifact", "wiki", "file"}:
            raise ValueError("unknown material filter")
        terms = query.casefold().split()
        rows = [] if kind == "wiki" else material_rows(conversation)
        rows = [row for row in rows if all(term in (
            row["title"] + " " + row["summary"] + " " + str((row["ref"] or {}).get("ref", ""))
        ).casefold() for term in terms)]
        artifacts = [row for row in rows if row["kind"] == "artifact"] if kind in {"all", "artifact"} else []
        files = [row for row in rows if row["kind"] == "file"] if kind in {"all", "file"} else []
        wiki_count = self.wiki.count(conversation.work_dir, query, reindex=reindex) if (
            kind in {"all", "wiki"} and has_active_workspace(conversation.work_dir)) else 0
        size, start = min(50, max(1, limit)), max(0, offset)
        page = artifacts[start:start + size]
        wiki_offset = max(0, start - len(artifacts))
        if len(page) < size and wiki_offset < wiki_count:
            for row in self.wiki.search(conversation.work_dir, query, offset=wiki_offset, limit=size - len(page)):
                page.append({**row, "key": content_identity(ContentRef.from_dict(row["ref"])),
                             "kind": "wiki", "roles": ["知识"], "scope": conversation.work_dir})
        if len(page) < size:
            file_offset = max(0, start - len(artifacts) - wiki_count)
            # Files follow all knowledge, including when the requested page begins in it.
            if start + len(page) >= len(artifacts) + wiki_count:
                page.extend(files[file_offset:file_offset + size - len(page)])
        return MaterialPage(page, len(artifacts) + wiki_count + len(files), start, size)

    async def promote_artifact(self, conversation, ref: ContentRef, *, provider) -> dict:
        resolved = self.resolver.resolve_content(conversation, ref)
        text = resolved.path.read_text(encoding="utf-8")
        if estimate_tokens(text) > 8000:
            raise ValueError("成果超过一次整理的输入上限，请先整理为一份精简的成果。")
        result = await self.executor.run_capability(provider=provider, capability_id="wiki_synthesize", message=text,
                                                    conversation=conversation)
        if result.validation_error or not isinstance(result.parsed, dict):
            raise ValueError(result.validation_error or "未得到有效的项目知识")
        # Re-check the artifact after the model call before pinning an exact version.
        self.resolver.resolve_content(conversation, ref)
        archive = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=self.data_dir)
        record = archive.write_original(kind="history", title=ref.name, content=text, source="artifact_version")
        source = ContentRef(id=record.id, name=ref.name, mime="text/markdown", size=record.size, digest=record.digest,
                            ref=f"archive:{record.id}", kind="archive", source="artifact", workspace=conversation.work_dir,
                            conversation_id=conversation.id)
        ok, page_id = self.wiki.apply(conversation.work_dir, {**result.parsed, "sources": [source.to_dict()]})
        if not ok:
            raise ValueError(page_id)
        self._changed(conversation.work_dir, conversation.id, ("wiki",))
        return self.wiki.read(conversation.work_dir, page_id)

    def save_knowledge(self, work_dir: str, page: dict) -> tuple[bool, str]:
        result = self.wiki.apply(work_dir, page)
        if result[0]:
            self._changed(work_dir, "", ("wiki",))
        return result

    def delete_knowledge(self, work_dir: str, page: dict) -> None:
        self.wiki.delete(work_dir, page["id"], expected_digest=page["digest"])
        self._changed(work_dir, "", ("wiki",))
