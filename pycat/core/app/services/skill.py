"""Workspace-aware skill discovery and declared invocation service."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.config import get_global_subdir
from pycat.core.persistence import atomic_write_bytes, atomic_write_text, exclusive_file_lock
from pycat.core.security.threats import first_threat_message
from pycat.core.skills import (
    Skill,
    SkillInvocationSpec,
    SkillsManager,
    resolve_skill_invocation_spec,
)
from pycat.core.skills import manage as skill_manage
from pycat.core.skills.manifest import bundled_skill_dir
from pycat.core.skills.usage import SkillUsageStore, usage_path
from pycat.models.contracts.tooling import FilesystemScope, ToolSelectionPolicy
from pycat.models.conversation import Conversation, Message
from pycat.models.session_paths import resolve_project_data_root

MAX_IMPORT_MEMBERS = 256
MAX_IMPORT_FILE_BYTES = 8 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 32 * 1024 * 1024


class SkillService:
    """Workspace-aware skill operations for command and UI layers."""

    def __init__(self, *, data_dir: str | None = None):
        self.data_dir = data_dir

    def _candidate_path(self, proposal_id: str, work_dir: str | None, scope: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16,64}", str(proposal_id)):
            raise ValueError("invalid skill proposal id")
        root = skill_manage.managed_root(scope, str(work_dir or ""), data_dir=self.data_dir)
        path = root / ".staging" / proposal_id
        if any(skill_manage.is_link_or_reparse(parent) for parent in (root, path.parent, path,
                path / "manifest.json", path / "baseline.md", path / "SKILL.md")):
            raise ValueError("candidate directory cannot be a symbolic link or junction")
        return path

    def stage_candidate(self, *, name: str, description: str, content: str, action: str,
                        scope: str, work_dir: str, proposal_id: str = "", sources=(), reason: str = "") -> tuple[bool, str]:
        try:
            error = skill_manage.validate_skill_payload(name, description, content)
            if error:
                raise ValueError(error)
            if action not in {"create", "patch"}:
                raise ValueError("candidate action must be create or patch")
            root = skill_manage.managed_root(scope, work_dir, data_dir=self.data_dir)
            active = root / name / "SKILL.md"
            if skill_manage.is_link_or_reparse(active.parent) or skill_manage.is_link_or_reparse(active):
                raise ValueError("active skill cannot be a symbolic link or junction")
            proposal_id = proposal_id or uuid.uuid4().hex
            path = self._candidate_path(proposal_id, work_dir, scope)
            candidate = skill_manage.build_skill_markdown(name, description, content).encode("utf-8")
            if (path / "manifest.json").exists():
                previous = self.candidate_detail(proposal_id, work_dir=work_dir, scope=scope)
                if previous["candidate_digest"] != hashlib.sha256(candidate).hexdigest():
                    raise ValueError("proposal id already belongs to different content")
                return True, proposal_id
            usage = SkillUsageStore(root=root, scope=scope)
            if usage.get(name).get("pinned"):
                raise ValueError("pinned skills require explicit manual editing")
            if action == "patch" and (not active.exists() or not usage.is_agent_created(name)):
                raise ValueError("patch denied for non-agent or missing managed skill")
            if action == "create" and active.exists():
                raise ValueError("skill already exists")
            with exclusive_file_lock(path / "manifest.json"):
                if (path / "manifest.json").exists():
                    return True, proposal_id
                baseline = active.read_bytes() if active.exists() else b""
                manifest = {"id": proposal_id, "name": name, "scope": scope, "action": action, "description": description,
                            "sources": list(sources), "reason": reason, "status": "draft",
                            "base_digest": hashlib.sha256(baseline).hexdigest(), "candidate_digest": hashlib.sha256(candidate).hexdigest(),
                            "evaluation": {}}
                atomic_write_bytes(path / "baseline.md", baseline)
                atomic_write_bytes(path / "SKILL.md", candidate)
                atomic_write_text(path / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            return True, proposal_id
        except (OSError, ValueError) as exc:
            return False, str(exc)

    def list_candidates(self, work_dir: str | None) -> list[dict]:
        result = []
        for scope in ("project", "global") if work_dir and work_dir != "." else ("global",):
            root = skill_manage.managed_root(scope, str(work_dir or ""), data_dir=self.data_dir) / ".staging"
            for path in root.glob("*/manifest.json") if root.exists() else ():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._candidate_path(data["id"], work_dir, scope)
                    result.append(data)
                except (OSError, ValueError, KeyError):
                    result.append({"id": path.parent.name, "name": path.parent.name, "scope": scope,
                                   "status": "unreadable", "evaluation": {}})
        return result

    def candidate_detail(self, proposal_id: str, *, work_dir: str | None, scope: str) -> dict:
        path = self._candidate_path(proposal_id, work_dir, scope)
        for name in ("baseline.md", "SKILL.md", "manifest.json"):
            if (path / name).stat().st_size > 512 * 1024:
                raise ValueError("candidate file exceeds the review size limit")
        return {**json.loads((path / "manifest.json").read_text(encoding="utf-8")),
                "before": (path / "baseline.md").read_text(encoding="utf-8"),
                "after": (path / "SKILL.md").read_text(encoding="utf-8")}

    async def evaluate_candidate(self, proposal_id: str, *, work_dir: str | None, scope: str,
                                 suite: list[dict], runtime, provider, model: str, parent_policy,
                                 app_settings: dict | None = None, cancel_event=None) -> dict:
        """Compare old/new methods with explicit oracles in isolated fixture roots."""
        if not isinstance(suite, list) or not 4 <= len(suite) <= 20 or any(not isinstance(case, dict) for case in suite):
            raise ValueError("evaluation requires 4–20 cases including normal, failure, inapplicable and heldout cases")
        if not {"normal", "failure", "inapplicable"} <= {case.get("kind") for case in suite} or not any(c.get("split") == "heldout" for c in suite):
            raise ValueError("evaluation requires normal, failure, inapplicable and separate heldout cases")
        prompts = [str(case.get("prompt") or "").strip() for case in suite]
        if len(set(prompts)) != len(prompts) or not all(prompts):
            raise ValueError("evaluation prompts must be non-empty and distinct")
        for case in suite:
            for field in ("contains", "excludes"):
                if not isinstance(case.get(field, []), list) or any(not isinstance(value, str) or not value for value in case.get(field, [])):
                    raise ValueError("output oracles must be arrays of non-empty strings")
            for field in ("files", "files_equal"):
                values = case.get(field) or {}
                if not isinstance(values, dict) or len(values) > 32 or sum(len(str(value)) for value in values.values()) > 262144:
                    raise ValueError("fixture files exceed the evaluation limit")
                for relative in values:
                    path_value = PurePosixPath(str(relative).replace("\\", "/"))
                    if not relative or path_value.is_absolute() or ".." in path_value.parts or ":" in str(relative):
                        raise ValueError("fixture paths must stay inside the isolated workspace")
            if not case.get("contains") and not case.get("files_equal"):
                raise ValueError("each evaluation case requires an explicit output or file oracle")
            if any(not isinstance(value, str) or not value for value in case.get("contains", [])):
                raise ValueError("output oracles must be non-empty strings")
        path = self._candidate_path(proposal_id, work_dir, scope)
        detail = self.candidate_detail(proposal_id, work_dir=work_dir, scope=scope)
        candidate_digest = hashlib.sha256(detail["after"].encode()).hexdigest()
        if candidate_digest != detail["candidate_digest"] or hashlib.sha256(detail["before"].encode()).hexdigest() != detail["base_digest"]:
            raise ValueError("candidate or baseline changed; create a new proposal")
        rows = []
        for number, case in enumerate(suite):
            for variant in ("before", "after"):
                if cancel_event is not None and cancel_event.is_set():
                    raise ValueError("evaluation cancelled; active skill unchanged")
                with tempfile.TemporaryDirectory(prefix="pycat-skill-eval-") as temporary:
                    root = Path(temporary).resolve()
                    for relative, content in (case.get("files") or {}).items():
                        target = (root / relative).resolve()
                        target.relative_to(root)
                        atomic_write_text(target, str(content))
                    conv = Conversation(work_dir=str(root), mode="chat", model=model)
                    conv.settings.update({"memory_enabled": False, "evaluation": True,
                        "session_instructions": "Evaluate this method only within the supplied fixture workspace.\n" + detail[variant]})
                    conv.add_message(Message(role="user", content=case["prompt"]))
                    policy = RunPolicyBuilder.build(conversation=conv, app_settings=app_settings or {}, source="evaluation",
                        tool_selection=parent_policy.tool_selection.intersect(ToolSelectionPolicy(allowed_tools={
                            "file__read", "file__ls", "file__grep", "file__write", "file__edit", "file__patch", "file__delete"})),
                        tool_permissions=parent_policy.tool_permissions, filesystem_scope=FilesystemScope(), max_turns=min(8, parent_policy.max_turns))
                    started = time.monotonic()
                    result = await asyncio.wait_for(runtime.run(provider=provider, conversation=conv, policy=policy, cancel_event=cancel_event), timeout=180)
                    output = str(getattr(result.final_message, "content", "") or "")
                    passed = str(getattr(result.status, "value", result.status)) == "completed"
                    passed = passed and all(text in output for text in case.get("contains", []))
                    passed = passed and all(text not in output for text in case.get("excludes", []))
                    for relative, expected in (case.get("files_equal") or {}).items():
                        target = (root / relative).resolve()
                        target.relative_to(root)
                        passed = passed and target.is_file() and target.read_text(encoding="utf-8") == str(expected)
                    rows.append({"case": number + 1, "kind": case["kind"], "split": case.get("split", "train"),
                                 "variant": variant, "passed": bool(passed), "output": output[:2000],
                                 "elapsed_ms": round((time.monotonic() - started) * 1000),
                                 "tokens": conv.total_tokens or None,
                                 "tool_calls": sum(len(message.tool_calls or []) for message in conv.messages)})
        before = sum(row["passed"] for row in rows if row["variant"] == "before")
        after = sum(row["passed"] for row in rows if row["variant"] == "after")
        report = {"passed": after == len(suite) and after > before, "before_passed": before, "after_passed": after,
                  "case_count": len(suite), "candidate_digest": candidate_digest, "base_digest": detail["base_digest"], "cases": rows,
                  "model": model, "cost": {variant: {"elapsed_ms": sum(row["elapsed_ms"] for row in rows if row["variant"] == variant),
                      "tokens": sum(row["tokens"] for row in rows if row["variant"] == variant) if all(row["tokens"] is not None for row in rows if row["variant"] == variant) else None}
                      for variant in ("before", "after")}}
        with exclusive_file_lock(path / "manifest.json"):
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            if hashlib.sha256((path / "SKILL.md").read_bytes()).hexdigest() != candidate_digest:
                raise ValueError("candidate changed during evaluation")
            manifest.update(status="passed" if report["passed"] else "failed", evaluation=report)
            atomic_write_text(path / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return report

    def publish_candidate(self, proposal_id: str, *, work_dir: str | None, scope: str, rollback: bool = False) -> tuple[bool, str]:
        try:
            path = self._candidate_path(proposal_id, work_dir, scope)
            with exclusive_file_lock(path / "manifest.json"):
                data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
                evaluation = data.get("evaluation") or {}
                candidate = (path / "SKILL.md").read_bytes()
                baseline = (path / "baseline.md").read_bytes()
                digest = hashlib.sha256(candidate).hexdigest()
                if hashlib.sha256(baseline).hexdigest() != data["base_digest"] or digest != data["candidate_digest"]:
                    raise ValueError("candidate or baseline changed; create and evaluate a new proposal")
                if not rollback and (not evaluation.get("passed") or evaluation.get("candidate_digest") != digest):
                    raise ValueError("a passing evaluation for this exact candidate is required")
                if rollback and data.get("status") not in {"published", "rolling_back", "rolled_back"}:
                    raise ValueError("only a published candidate can be rolled back")
                root = skill_manage.managed_root(scope, str(work_dir or ""), data_dir=self.data_dir)
                active = root / data["name"] / "SKILL.md"
                if skill_manage.is_link_or_reparse(active.parent) or skill_manage.is_link_or_reparse(active):
                    raise ValueError("active skill cannot be a symbolic link or junction")
                usage = SkillUsageStore(root=root, scope=scope)
                if usage.get(data["name"]).get("pinned"):
                    raise ValueError("pinned skill cannot be changed by candidate publication")
                with exclusive_file_lock(active):
                    current = active.read_bytes() if active.exists() else b""
                    expected = digest if rollback else data["base_digest"]
                    replacement = baseline if rollback else candidate
                    completed = "rolled_back" if rollback else "published"
                    preparing = "rolling_back" if rollback else "publishing"
                    if current == replacement and data["status"] == completed:
                        return True, completed
                    recovering = current == replacement and data["status"] == preparing
                    if not recovering and hashlib.sha256(current).hexdigest() != expected:
                        raise ValueError("active skill changed; rebase and evaluate the candidate again")
                    if not recovering:
                        data["status"] = preparing
                        atomic_write_text(path / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
                        if replacement:
                            atomic_write_bytes(active, replacement)
                        else:
                            active.unlink(missing_ok=True)
                    accounted = usage.record_patched(data["name"], by="agent") if current else usage.record_created(data["name"], created_by="agent")
                    if not accounted:
                        if current:
                            atomic_write_bytes(active, current)
                        else:
                            active.unlink(missing_ok=True)
                        raise ValueError("skill provenance update failed; publication restored")
                    data["status"] = completed
                    atomic_write_text(path / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
            return True, data["status"]
        except (OSError, ValueError, KeyError) as exc:
            return False, str(exc)

    def upsert_skill_content(
        self,
        name: str,
        *,
        description: str,
        content: str,
        work_dir: str | None = None,
        scope: str = "global",
        create_only: bool = False,
        created_by: str = "user",
    ) -> tuple[bool, str]:
        """Validated create/patch of a managed skill (tool + review channel)."""
        return skill_manage.upsert_skill(
            name,
            description=description,
            content=content,
            work_dir=str(work_dir or ""),
            scope=scope,
            create_only=create_only,
            created_by=created_by,
            data_dir=self.data_dir,
        )

    def write_skill_resource(
        self,
        name: str,
        relative_path: str,
        content: str,
        *,
        work_dir: str | None = None,
        scope: str = "global",
        remove: bool = False,
    ) -> tuple[bool, str]:
        return skill_manage.write_skill_resource(
            name,
            relative_path,
            content,
            work_dir=str(work_dir or ""),
            scope=scope,
            remove=remove,
            data_dir=self.data_dir,
        )

    def is_agent_created(
        self,
        name: str,
        *,
        work_dir: str | None = None,
        scope: str = "global",
    ) -> bool:
        return skill_manage.is_agent_created(
            name,
            work_dir=str(work_dir or ""),
            scope=scope,
            data_dir=self.data_dir,
        )


    def list_for_workdir(self, work_dir: str | None, *, include_disabled: bool = False) -> list:
        return SkillsManager(work_dir or "", include_disabled=include_disabled, data_dir=self.data_dir).list_skills()

    def get(
        self,
        skill_name: str,
        *,
        work_dir: str | None,
        include_disabled: bool = False,
    ) -> Skill | None:
        return SkillsManager(work_dir or "", include_disabled=include_disabled, data_dir=self.data_dir).get(skill_name)

    def exists(self, skill_name: str, *, work_dir: str | None) -> bool:
        return SkillsManager(work_dir or "", data_dir=self.data_dir).get(skill_name) is not None

    def copy_to_managed(self, name: str, *, scope: str = "global", work_dir: str | None) -> Path:
        skill = self.get(name, work_dir=work_dir, include_disabled=True)
        if skill is None:
            raise ValueError("技能不存在。")
        return self.import_managed(Path(skill.source).parent, scope=scope, work_dir=work_dir)

    @staticmethod
    def content_digest(root: Path) -> str:
        digest = hashlib.sha256()
        entries = []
        total = 0
        for path in root.rglob("*"):
            if path.parent == root and path.name in {".pycat-install.json", ".disabled"}:
                continue
            entries.append(path)
            if len(entries) > MAX_IMPORT_MEMBERS:
                raise ValueError("技能本地内容超过条目限制，请先整理本地修改。")
            if skill_manage.is_link_or_reparse(path):
                raise ValueError("技能包含符号链接或 junction。")
        for path in sorted(entries):
            if path.is_file():
                if path.stat().st_size > MAX_IMPORT_FILE_BYTES:
                    raise ValueError("技能本地内容超过大小限制，请先整理本地修改。")
                with path.open("rb") as stream:
                    content = stream.read(MAX_IMPORT_FILE_BYTES + 1)
                total += len(content)
                if len(content) > MAX_IMPORT_FILE_BYTES or total > MAX_IMPORT_TOTAL_BYTES:
                    raise ValueError("技能本地内容超过大小限制，请先整理本地修改。")
                relative = path.relative_to(root).as_posix().encode("utf-8")
                digest.update(len(relative).to_bytes(4, "big") + relative)
                digest.update(hashlib.sha256(content).digest())
        return digest.hexdigest()

    @staticmethod
    def installation_info(skill: Skill) -> dict:
        path = Path(skill.source).parent / ".pycat-install.json"
        if not path.is_file() or skill_manage.is_link_or_reparse(path) or path.stat().st_size > 8192:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def create_managed(
        self,
        name: str,
        *,
        description: str = "",
        scope: str = "global",
        work_dir: str | None,
    ) -> Path:
        skill_name = str(name or "").strip().lower()
        if not skill_manage.SKILL_NAME_RE.fullmatch(skill_name):
            raise ValueError("技能名称只能包含小写字母、数字和连字符，长度为 3-64 个字符。")
        root = self._scope_root(scope, work_dir)
        root.mkdir(parents=True, exist_ok=True)
        target = root / skill_name
        if target.exists():
            raise ValueError(f"技能已存在：{skill_name}")

        staging = root / f".{skill_name}.creating-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            text = (
                "---\n"
                f"name: {skill_name}\n"
                f"description: {self._yaml_scalar(description or '说明这个技能适合处理什么。')}\n"
                "mode: agent\n"
                "---\n\n"
                "# 使用说明\n\n"
                "写下触发场景、处理步骤和必要约束。\n"
            )
            (staging / "SKILL.md").write_text(text, encoding="utf-8")
            os.replace(staging, target)
            usage = SkillUsageStore(root=root, scope=str(scope or "global").strip().lower())
            if not usage.record_created(skill_name, created_by="user"):
                raise ValueError("技能 provenance 账本不可写，创建已回滚。")
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise
        return target / "SKILL.md"

    def import_managed(
        self,
        source: Path,
        *,
        scope: str = "global",
        work_dir: str | None,
        overwrite: bool = False,
        install_receipt: dict | None = None,
        expected_digest: str | None = None,
    ) -> Path:
        """Import a local skill directory or ``.zip`` archive into a managed root.

        The source is copied (never moved or mutated); external read-only roots
        stay untouched. Content passes the strict threat scan before publish,
        and the usage ledger records ``created_by="import"``.
        """
        source_path = Path(source).expanduser()
        if not self._path_exists(source_path):
            raise ValueError("导入来源不存在。")
        if self._is_link_or_reparse(source_path):
            raise ValueError("导入来源不能是符号链接或 junction。")
        root = self._scope_root(scope, work_dir)
        root.mkdir(parents=True, exist_ok=True)

        staging_parent = root / f".import-{uuid.uuid4().hex}"
        staging_parent.mkdir(parents=False, exist_ok=False)
        backup_path: Path | None = None
        try:
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                staged_root = self._extract_skill_zip(source_path, staging_parent)
            elif source_path.is_file() and source_path.suffix.lower() == ".md":
                staged_root = self._stage_skill_file(source_path, staging_parent)
            elif source_path.is_dir():
                staged_root = self._stage_skill_dir(source_path, staging_parent)
            else:
                raise ValueError("只支持 .md 技能文件、包含 SKILL.md 的目录或 .zip 压缩包。")

            skill_name = staged_root.name
            if not skill_manage.SKILL_NAME_RE.fullmatch(skill_name):
                raise ValueError("技能名称只能包含小写字母、数字和连字符，长度为 3-64 个字符。")
            if not (staged_root / "SKILL.md").is_file():
                raise ValueError("导入内容缺少 SKILL.md 入口文件。")

            self._scan_skill_tree(staged_root)
            receipt_path = staged_root / ".pycat-install.json"
            receipt_path.unlink(missing_ok=True)
            if install_receipt is not None:
                receipt = {**install_receipt, "content_digest": self.content_digest(staged_root)}
                atomic_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))

            target = root / skill_name
            target_exists = self._path_exists(target)
            if target_exists and not overwrite:
                raise FileExistsError(f"技能已存在：{skill_name}")
            if target_exists and self._is_link_or_reparse(target):
                raise ValueError("目标技能目录不能是符号链接或 junction。")
            if expected_digest is not None:
                if not target_exists or self.content_digest(target) != expected_digest:
                    raise ValueError("技能存在本地修改，请先保留或移走修改后再更新。")
                if (target / ".disabled").exists():
                    (staged_root / ".disabled").write_text("", encoding="utf-8")
            usage = SkillUsageStore(root=root, scope=str(scope or "global").strip().lower())
            usage_file = usage_path(root)
            usage_before = self._read_optional_bytes(usage_file)
            if target_exists:
                backup_path = staging_parent / f".replaced-{uuid.uuid4().hex}"
                os.replace(target, backup_path)
            try:
                os.replace(staged_root, target)
                if not usage.record_created(skill_name, created_by="import"):
                    raise ValueError("技能 provenance 账本不可写，导入已回滚。")
            except Exception as exc:
                rollback_errors: list[str] = []
                try:
                    self._remove_path(target)
                except Exception as rollback_exc:
                    rollback_errors.append(f"新目录清理失败：{rollback_exc}")
                if backup_path is not None and self._path_exists(backup_path):
                    try:
                        os.replace(backup_path, target)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"旧目录恢复失败：{rollback_exc}")
                try:
                    self._restore_optional_bytes(usage_file, usage_before)
                except Exception as rollback_exc:
                    rollback_errors.append(f"provenance 账本恢复失败：{rollback_exc}")
                if rollback_errors:
                    raise RuntimeError(
                        "技能导入回滚未完整完成：" + "；".join(rollback_errors)
                    ) from exc
                raise
            if backup_path is not None:
                self._remove_path(backup_path, ignore_errors=True)
            return target / "SKILL.md"
        finally:
            self._remove_path(staging_parent, ignore_errors=True)

    def _stage_skill_dir(self, source: Path, staging_parent: Path) -> Path:
        if self._is_link_or_reparse(source):
            raise ValueError("导入来源不能是符号链接或 junction。")
        source_name = source.name or source.resolve().name
        self._check_import_component(source_name)
        # The staging directory lives below the managed root.  Refuse a
        # source that contains it (for example importing the managed root
        # itself), otherwise os.walk would copy the staging tree into itself.
        try:
            staging_parent.resolve(strict=False).relative_to(source.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("导入来源不能包含目标管理目录。")
        staged = staging_parent / source_name
        staged.mkdir(parents=False, exist_ok=False)
        member_count = 0
        total_bytes = 0
        for current, directories, files in os.walk(source, topdown=True, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(source)
            destination_dir = staged / relative
            destination_dir.mkdir(parents=True, exist_ok=True)
            directories.sort()
            files.sort()
            for directory_name in directories:
                source_dir = current_path / directory_name
                if self._is_link_or_reparse(source_dir):
                    raise ValueError(f"导入内容包含符号链接或 junction：{source_dir.name}")
                self._check_import_component(directory_name)
                member_count += 1
                self._check_import_member_count(member_count)
            for file_name in files:
                source_file = current_path / file_name
                if self._is_link_or_reparse(source_file):
                    raise ValueError(f"导入内容包含符号链接或 junction：{source_file.name}")
                self._check_import_component(file_name)
                mode = os.lstat(source_file).st_mode
                if not stat.S_ISREG(mode):
                    raise ValueError(f"导入内容包含不支持的文件类型：{source_file.name}")
                member_count += 1
                self._check_import_member_count(member_count)
                size = source_file.stat().st_size
                self._check_import_file_size(size, source_file.name)
                total_bytes += size
                self._check_import_total_size(total_bytes)
                shutil.copyfile(source_file, destination_dir / file_name)
        return staged

    def _stage_skill_file(self, source: Path, staging_parent: Path) -> Path:
        """Stage a single ``.md`` skill file into a directory named after its stem."""
        skill_name = source.stem.strip().lower()
        if not skill_manage.SKILL_NAME_RE.fullmatch(skill_name):
            raise ValueError(
                "技能文件名只能包含小写字母、数字和连字符，长度为 3-64 个字符。"
            )
        self._check_import_component(skill_name)
        staged = staging_parent / skill_name
        staged.mkdir(parents=False, exist_ok=False)
        size = source.stat().st_size
        self._check_import_file_size(size, source.name)
        self._check_import_total_size(size)
        shutil.copyfile(source, staged / "SKILL.md")
        return staged

    def _extract_skill_zip(self, archive: Path, staging_parent: Path) -> Path:
        """Safely extract one skill zip with bounded, non-linking writes."""

        entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        seen: set[str] = set()
        total_bytes = 0
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                raw_name = str(info.filename or "")
                if raw_name == "__MACOSX" or raw_name.startswith("__MACOSX/"):
                    continue
                parts = self._safe_zip_parts(raw_name)
                key = "/".join(part.casefold() for part in parts)
                if key in seen:
                    raise ValueError(f"压缩包包含重复路径：{raw_name}")
                seen.add(key)
                self._check_import_member_count(len(entries) + 1)
                mode = (int(info.external_attr) >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ValueError(f"压缩包包含符号链接：{raw_name}")
                file_type = stat.S_IFMT(mode)
                if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError(f"压缩包包含不支持的文件类型：{raw_name}")
                if info.is_dir() or raw_name.endswith("/"):
                    entries.append((info, parts))
                    continue
                size = int(info.file_size)
                self._check_import_file_size(size, raw_name)
                total_bytes += size
                self._check_import_total_size(total_bytes)
                entries.append((info, parts))

            if not entries:
                raise ValueError("压缩包为空。")

            staging_root = staging_parent.resolve()
            for info, parts in entries:
                destination = staging_parent.joinpath(*parts)
                try:
                    destination.resolve(strict=False).relative_to(staging_root)
                except ValueError as exc:
                    raise ValueError(f"压缩包包含不安全路径：{info.filename}") from exc
                if info.is_dir() or str(info.filename or "").endswith("/"):
                    if destination.exists() and not destination.is_dir():
                        raise ValueError(f"压缩包路径冲突：{info.filename}")
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    destination.parent.resolve(strict=False).relative_to(staging_root)
                except ValueError as exc:
                    raise ValueError(f"压缩包包含不安全路径：{info.filename}") from exc
                written = 0
                with zf.open(info, "r") as source_handle, destination.open("wb") as target_handle:
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_IMPORT_FILE_BYTES:
                            raise ValueError(f"压缩包单文件大小超过 {MAX_IMPORT_FILE_BYTES} 字节：{info.filename}")
                        target_handle.write(chunk)
                if written != int(info.file_size):
                    raise ValueError(f"压缩包文件大小校验失败：{info.filename}")

        extracted = sorted(staging_parent.iterdir(), key=lambda item: item.name.casefold())
        if len(extracted) == 1 and extracted[0].is_dir():
            return extracted[0]
        if any(item.name == "SKILL.md" for item in extracted):
            wrapped = staging_parent / archive.stem.lower()
            wrapped.mkdir(parents=False, exist_ok=False)
            for item in extracted:
                os.replace(item, wrapped / item.name)
            return wrapped
        raise ValueError("压缩包必须只包含一个技能目录，或顶层直接包含 SKILL.md。")

    @staticmethod
    def _safe_zip_parts(raw_name: str) -> tuple[str, ...]:
        normalized = str(raw_name or "").replace("\\", "/")
        if not normalized or "\x00" in normalized:
            raise ValueError(f"压缩包包含不安全路径：{raw_name}")
        if normalized.startswith("/"):
            raise ValueError(f"压缩包包含绝对路径：{raw_name}")
        path = PurePosixPath(normalized)
        parts = path.parts
        if not parts or path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"压缩包包含不安全路径：{raw_name}")
        # A colon is an alternate-data-stream separator on Windows.  Reject
        # it in every component, not only the first one, so a cross-platform
        # import cannot create an ambiguous destination such as
        # ``skill/docs/readme.txt:payload``.
        if any(":" in part for part in parts):
            raise ValueError(f"压缩包包含不安全路径：{raw_name}")
        return tuple(parts)

    @staticmethod
    def _check_import_component(name: str) -> None:
        """Reject filename components with cross-platform path semantics."""
        text = str(name or "")
        if (
            not text
            or text in {".", ".."}
            or "\x00" in text
            or ":" in text
            or "/" in text
            or "\\" in text
        ):
            raise ValueError(f"导入内容包含不安全路径：{name}")

    @staticmethod
    def _check_import_member_count(count: int) -> None:
        if int(count) > MAX_IMPORT_MEMBERS:
            raise ValueError(f"导入内容条目数超过 {MAX_IMPORT_MEMBERS} 个")

    @staticmethod
    def _check_import_file_size(size: int, name: str) -> None:
        if int(size) < 0 or int(size) > MAX_IMPORT_FILE_BYTES:
            raise ValueError(
                f"导入内容单文件大小超过 {MAX_IMPORT_FILE_BYTES} 字节：{name}"
            )

    @staticmethod
    def _check_import_total_size(size: int) -> None:
        if int(size) > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError(f"导入内容总大小超过 {MAX_IMPORT_TOTAL_BYTES} 字节")

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        return skill_manage.is_link_or_reparse(path)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return path.exists() or SkillService._is_link_or_reparse(path)

    @classmethod
    def _remove_path(cls, path: Path, *, ignore_errors: bool = False) -> None:
        try:
            if cls._is_link_or_reparse(path) or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except FileNotFoundError:
            return
        except Exception:
            if not ignore_errors:
                raise

    @staticmethod
    def _read_optional_bytes(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"技能 provenance 账本无法读取：{exc}") from exc

    @classmethod
    def _restore_optional_bytes(cls, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(path, content)

    def _scan_skill_tree(self, staged_root: Path) -> None:
        """Threat-scan text files of a staged skill tree (strict scope)."""
        for path in sorted(staged_root.rglob("*")):
            if self._is_link_or_reparse(path):
                raise ValueError(f"导入内容包含符号链接或 junction：{path.name}")
            if not path.is_file():
                continue
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".pdf", ".onnx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            threat = first_threat_message(text, scope="strict")
            if threat:
                raise ValueError(f"导入内容未通过安全检查（{path.name}）：{threat}")

    def set_managed_enabled(self, skill: Skill, enabled: bool, *, work_dir: str | None) -> None:
        if skill.source_scope == "bundled":
            source = Path(skill.source).resolve()
            if source != (bundled_skill_dir() / skill.name / "SKILL.md").resolve() or not source.is_file():
                raise ValueError("无效的内置技能。")
            marker = get_global_subdir("skills", data_dir=self.data_dir) / ".bundled-disabled" / skill.name
            if enabled:
                marker.unlink(missing_ok=True)
            else:
                atomic_write_text(marker, "disabled\n")
            return
        root = self._managed_root(skill, work_dir)
        marker = root / ".disabled"
        if enabled:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            return
        marker.write_text("disabled by PyCat settings\n", encoding="utf-8")

    def delete_managed(self, skill: Skill, *, work_dir: str | None) -> None:
        root = self._managed_root(skill, work_dir)
        staged = root.parent / f".{root.name}.deleting-{uuid.uuid4().hex}"
        os.replace(root, staged)
        try:
            shutil.rmtree(staged)
        except Exception:
            try:
                os.replace(staged, root)
            except Exception:
                pass
            raise

    def _scope_root(self, scope: str, work_dir: str | None) -> Path:
        value = str(scope or "global").strip().lower()
        if value == "global":
            return get_global_subdir("skills", data_dir=self.data_dir).resolve()
        if value == "project":
            clean_work_dir = str(work_dir or "").strip()
            if not clean_work_dir:
                raise ValueError("没有工作区时不能创建项目技能。")
            return (resolve_project_data_root(clean_work_dir, data_dir=self.data_dir) / "skills").resolve()
        raise ValueError("技能范围只能是 global 或 project。")

    def _managed_root(self, skill: Skill, work_dir: str | None) -> Path:
        if skill is None or bool(getattr(skill, "read_only", False)):
            raise ValueError("外部技能为只读，不能修改。")
        source = Path(str(getattr(skill, "source", "") or "")).expanduser().resolve()
        root = source.parent
        if not (root / "SKILL.md").is_file():
            raise ValueError("技能入口文件不存在。")
        allowed = [get_global_subdir("skills", data_dir=self.data_dir).resolve()]
        if str(work_dir or "").strip():
            allowed.append((resolve_project_data_root(work_dir, data_dir=self.data_dir) / "skills").resolve())
        for base in allowed:
            try:
                root.relative_to(base)
            except ValueError:
                continue
            return root
        raise ValueError("技能路径不在 PyCat 管理目录内。")

    @staticmethod
    def _yaml_scalar(value: str) -> str:
        text = " ".join(str(value or "").split())
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def get_invocation_spec(
        self,
        skill_name: str,
        *,
        work_dir: str | None,
        fallback_mode: str = "agent",
    ) -> SkillInvocationSpec | None:
        skill = self.get(skill_name, work_dir=work_dir)
        if skill is None:
            return None
        return resolve_skill_invocation_spec(skill, fallback_mode=fallback_mode)
