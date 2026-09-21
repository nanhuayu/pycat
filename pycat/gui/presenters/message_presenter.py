"""Message & streaming presenter.

Extracts message sending, streaming control, and response handling
from MainWindow, reducing it by ~300 lines.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QMessageBox

from pycat.core.agent.run.control_messages import RESUME_INTERRUPTED_RUN
from pycat.core.app.services.conversation import ConversationRevisionResult
from pycat.core.channel.bindings import is_bound_channel_conversation
from pycat.core.content.attachments import extract_composer_text
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.context.history import (
    is_real_user_message,
    restartable_user_by_id,
    restartable_user_for_assistant,
)
from pycat.core.context.sections import extract_user_request
from pycat.gui.presenters.prompt_optimization_presenter import PromptOptimizationPresenter
from pycat.gui.presenters.streaming_message_presenter import StreamingMessagePresenter
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.models.contracts.agent import PersistenceError, RunEvent, RunStatus
from pycat.models.contracts.content import InputPreparationResult
from pycat.models.conversation import Conversation, Message
from pycat.models.model_ref import build_model_ref
from pycat.models.provider import Provider

if TYPE_CHECKING:
    from pycat.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class MessagePresenter:
    """Handles message sending, streaming, and response lifecycle."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host
        self._prompt_optimization_presenter = PromptOptimizationPresenter(host)
        self._streaming_presenter = StreamingMessagePresenter(host)
        self._submission_jobs: dict[str, BackgroundJob] = {}
        self._recovered_guidance: dict[str, tuple[str, ...]] = {}

    def is_submitting(self, conversation_id: str) -> bool:
        return str(conversation_id or "") in self._submission_jobs

    def _start_submission_job(
        self,
        conversation_id: str,
        job: BackgroundJob,
    ) -> None:
        self._submission_jobs[conversation_id] = job
        self._host.window_state_presenter.sync_input_enabled()
        self._host.window_state_presenter.sync_runtime_state(conversation_id)
        QThreadPool.globalInstance().start(job)

    def abandon_background_jobs(self) -> None:
        jobs = list(self._submission_jobs.items())
        self._submission_jobs.clear()
        for _conversation_id, job in jobs:
            job.abandon()

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, content: str, attachments: list, metadata: Optional[dict[str, Any]] = None, *, delegate_profile: str = '') -> None:
        host = self._host
        extra_metadata = dict(metadata or {})
        revision_request = extra_metadata.pop("conversation_revision", None)

        if host.current_conversation is not None:
            is_maintaining = getattr(
                getattr(host, "conversation_presenter", None),
                "is_maintaining",
                None,
            )
            if callable(is_maintaining) and is_maintaining(host.current_conversation.id):
                host.chat_view.show_notice(
                    "该会话正在处理，请稍候",
                    tone="warning",
                    timeout_ms=3000,
                    conversation_id=host.current_conversation.id,
                )
                return

        if revision_request and not host.current_conversation:
            return

        if not host.current_conversation:
            host.current_conversation = host.conversation_presenter.ensure_current_conversation_shell()

        if host.message_runtime.is_streaming(host.current_conversation.id):
            if revision_request:
                host.chat_view.show_notice(
                    "任务运行中不能编辑历史消息",
                    tone="warning",
                    timeout_ms=4000,
                    conversation_id=host.current_conversation.id,
                )
                return
            self._submit_runtime_guidance(content, attachments)
            return

        provider_id = host.input_area.get_selected_provider_id()
        model = host.input_area.get_selected_model()

        provider = self._find_provider(provider_id)
        if not provider:
            QMessageBox.warning(host, "错误", "请先在设置中配置服务商")
            return
        if not model:
            QMessageBox.warning(host, "错误", "请选择一个模型")
            return

        provider_metadata = {
            "provider_id": provider_id,
            "provider_name": getattr(provider, "name", ""),
            "model": model,
            "model_ref": build_model_ref(getattr(provider, "name", ""), model),
        }
        extra_metadata.update(provider_metadata)
        if extra_metadata.get('mentions'):
            from pycat.models.contracts.agent import MentionRef
            try:
                extra_metadata['mentions'] = host.services.run_service.resolve_mentions(
                    [MentionRef(**item) for item in extra_metadata['mentions']], work_dir=host.current_conversation.work_dir)
            except (ValueError, TypeError) as exc:
                host.chat_view.show_notice(str(exc), tone='error', conversation_id=host.current_conversation.id)
                return

        if isinstance(revision_request, dict):
            self._submit_revision(
                conversation=host.current_conversation,
                provider=provider,
                content=content,
                attachments=[
                    dict(item) for item in (attachments or []) if isinstance(item, dict)
                ],
                metadata=extra_metadata,
                revision_request=revision_request,
            )
            return

        host.current_conversation = host.conversation_presenter.seed_from_input(host.current_conversation)
        host.services.app_coordinator.update_provider_model(
            host.current_conversation,
            providers=host.providers,
            provider_id=provider_id,
            model=model,
        )
        host.inspector_panel.update_stats(host.current_conversation)
        host.services.conv_service.save(host.current_conversation)
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
        )

        # Empty input: if last message is user, just re-stream
        if not content and not attachments:
            if (
                host.current_conversation.messages
                and host.current_conversation.messages[-1].role == "user"
            ):
                self.start_streaming(provider)
            return

        user_message = Message(role="user", content=content)
        if extra_metadata:
            user_message.metadata.update(extra_metadata)
        user_message.metadata.update(provider_metadata)
        raw_attachments = [dict(item) for item in (attachments or []) if isinstance(item, dict)]
        if raw_attachments:
            self._prepare_and_commit_message(
                conversation=host.current_conversation,
                provider=provider,
                user_message=user_message,
                attachments=raw_attachments,
                composer_text=content,
            )
            return

        if not self._commit_message(host.current_conversation, provider, user_message, delegate_profile=delegate_profile):
            restore = getattr(host.input_area, "restore_failed_message", None)
            if callable(restore):
                restore(content, [])
            host.chat_view.show_notice(
                "消息保存失败，请重试",
                tone="error",
                timeout_ms=5000,
                conversation_id=host.current_conversation.id,
            )
            return
        confirm = getattr(host.input_area, "confirm_message_sent", None)
        if callable(confirm):
            confirm(content, [])

    def _submit_runtime_guidance(self, content: str, attachments: list) -> None:
        host = self._host
        conversation = host.current_conversation
        text = str(content or "").strip()
        if conversation is None:
            return
        if attachments:
            host.chat_view.show_notice(
                "运行中只能追加纯文本要求",
                tone="warning",
                timeout_ms=4000,
                conversation_id=conversation.id,
            )
            return
        if not text:
            return
        if text.startswith(("/", "#", "!")):
            host.chat_view.show_notice(
                "运行中不执行命令；请追加普通文本要求",
                tone="warning",
                timeout_ms=4000,
                conversation_id=conversation.id,
            )
            return
        if not host.message_runtime.submit_guidance(conversation.id, text):
            host.chat_view.show_notice(
                "当前任务已结束或待处理要求已满",
                tone="warning",
                timeout_ms=4000,
                conversation_id=conversation.id,
            )
            return
        confirm = getattr(host.input_area, "confirm_message_sent", None)
        if callable(confirm):
            confirm(text, [])
        host.window_state_presenter.sync_runtime_state(conversation.id)

    def _submit_revision(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        content: str,
        attachments: list[dict[str, Any]],
        metadata: dict[str, Any],
        revision_request: dict[str, Any],
    ) -> None:
        host = self._host
        conversation_id = str(conversation.id or "")
        target_message_id = str(revision_request.get("message_id") or "")
        expected_fingerprint = str(revision_request.get("fingerprint") or "")
        request_conversation_id = str(revision_request.get("conversation_id") or "")
        if (
            not conversation_id
            or request_conversation_id != conversation_id
            or not target_message_id
            or not expected_fingerprint
        ):
            host.chat_view.show_notice(
                "编辑请求已失效，请重新选择消息",
                tone="warning",
                timeout_ms=5000,
                conversation_id=conversation_id,
            )
            return
        if self.is_submitting(conversation_id):
            host.chat_view.show_notice(
                "该会话正在提交消息，请稍候",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation_id,
            )
            return
        if not str(content or "").strip() and not attachments:
            host.chat_view.show_notice(
                "消息正文和附件不能同时为空",
                tone="warning",
                timeout_ms=4000,
                conversation_id=conversation_id,
            )
            return
        if not self._confirm_turn_restart(
            conversation,
            target_message_id,
            action="编辑并重试",
            channel_action=self._is_external_message(conversation, target_message_id),
        ):
            return

        activity_token = host.services.conv_service.begin_lifecycle(
            conversation_id,
            "revision",
        )
        if activity_token is None:
            host.chat_view.show_notice(
                "该会话正在运行或处理，暂不能编辑",
                tone="warning",
                timeout_ms=4000,
                conversation_id=conversation_id,
            )
            return

        replacement_message_id = str(uuid.uuid4())
        conversation_snapshot = conversation.clone()
        job_holder: dict[str, BackgroundJob] = {}

        def operation():
            preparation = (
                host.services.content_service.prepare_inputs(
                    conversation_snapshot,
                    attachments,
                    message_id=replacement_message_id,
                )
                if attachments
                else InputPreparationResult()
            )
            job = job_holder.get("job")
            if preparation.failures or (job is not None and job.cancelled):
                return preparation, None
            revision = host.services.conv_service.replace_user_turn(
                conversation_id,
                target_message_id,
                content=content,
                content_refs=preparation.refs,
                metadata=metadata,
                expected_fingerprint=expected_fingerprint,
                user_message_id=replacement_message_id,
                activity_token=activity_token,
                allow_external=self._allows_channel_revisions(conversation_snapshot),
            )
            return preparation, revision

        def on_discard(result, _error) -> None:
            preparation, revision = self._turn_restart_job_result(result)
            if preparation is not None and not bool(revision and revision.ok):
                self._cleanup_prepared_inputs(
                    conversation_snapshot,
                    preparation.created_refs,
                )
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)

        job = BackgroundJob(operation, on_discard=on_discard)
        job_holder["job"] = job
        job.signals.finished.connect(
            lambda result,
            error,
            active_job=job: self._finish_turn_restart(
                conversation_id=conversation_id,
                job=active_job,
                activity_token=activity_token,
                conversation_snapshot=conversation_snapshot,
                provider=provider,
                target_message_id=target_message_id,
                edited_content=content,
                attachments=attachments,
                result=result,
                error=error,
            )
        )
        self._start_submission_job(conversation_id, job)

    def _finish_turn_restart(
        self,
        *,
        conversation_id: str,
        job: BackgroundJob,
        activity_token: str,
        conversation_snapshot: Conversation,
        provider: Provider,
        target_message_id: str,
        edited_content: str | None,
        attachments: list[dict[str, Any]],
        result,
        error,
        start_after: bool = True,
    ) -> None:
        host = self._host
        revised: Conversation | None = None
        active = self._submission_jobs.get(conversation_id)
        if active is not job:
            preparation, revision = self._turn_restart_job_result(result)
            if preparation is not None and not bool(revision and revision.ok):
                self._cleanup_prepared_inputs(
                    conversation_snapshot,
                    preparation.created_refs,
                )
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)
            return
        self._submission_jobs.pop(conversation_id, None)
        try:
            preparation, revision = self._turn_restart_job_result(result)
            if error is not None:
                if preparation is not None:
                    self._cleanup_prepared_inputs(
                        conversation_snapshot,
                        preparation.created_refs,
                    )
                self._show_revision_error(conversation_id, f"会话修订失败：{error}")
                return
            if preparation is None:
                self._show_revision_error(conversation_id, "修订请求不完整")
                return
            if preparation.failures:
                self._cleanup_prepared_inputs(
                    conversation_snapshot,
                    preparation.created_refs,
                )
                errors = {
                    failure.source: failure.error
                    for failure in preparation.failures
                }
                marker = getattr(host.input_area, "mark_attachment_errors", None)
                current_id = str(getattr(host.current_conversation, "id", "") or "")
                if callable(marker) and current_id == conversation_id:
                    marker(errors)
                detail = "；".join(
                    f"{self._attachment_label(failure.source)}：{failure.error}"
                    for failure in preparation.failures[:3]
                )
                self._show_revision_error(
                    conversation_id,
                    f"附件准备失败：{detail}",
                    timeout_ms=8000,
                )
                return
            if revision is None:
                self._cleanup_prepared_inputs(
                    conversation_snapshot,
                    preparation.created_refs,
                )
                self._show_revision_error(conversation_id, "修订已取消")
                return
            if not revision.ok or revision.conversation is None:
                self._cleanup_prepared_inputs(
                    conversation_snapshot,
                    preparation.created_refs,
                )
                self._show_revision_error(
                    conversation_id,
                    revision.error or "会话修订失败",
                )
                return
            revised = revision.conversation
            revised = self._publish_turn_restart(
                revised,
                target_message_id=target_message_id,
                edited_content=edited_content,
                attachments=attachments,
            )
            if revised is not None and start_after:
                state = self.start_streaming(provider, conversation=revised, activity_token=activity_token)
                if state is None:
                    self._show_revision_error(conversation_id, "消息已修订，但任务未能启动，请重试")
        finally:
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)
            host.window_state_presenter.sync_input_enabled()

    @staticmethod
    def _turn_restart_job_result(
        result,
    ) -> tuple[InputPreparationResult | None, ConversationRevisionResult | None]:
        if not isinstance(result, tuple) or len(result) != 2:
            return None, None
        preparation = result[0] if isinstance(result[0], InputPreparationResult) else None
        revision = result[1] if isinstance(result[1], ConversationRevisionResult) else None
        return preparation, revision

    def _publish_turn_restart(
        self,
        conversation: Conversation,
        *,
        target_message_id: str,
        edited_content: str | None,
        attachments: list[dict[str, Any]],
    ) -> Conversation:
        host = self._host
        conversation_id = str(conversation.id or "")
        conversations = host.services.conv_service.list_all()
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        is_current = bool(
            host.current_conversation
            and str(host.current_conversation.id or "") == conversation_id
        )
        if not is_current:
            return conversation
        confirm = getattr(host.input_area, "confirm_revision_sent", None)
        if edited_content is not None and callable(confirm):
            confirm(
                conversation_id,
                target_message_id,
                edited_content,
                attachments,
            )
        select = getattr(host.conversation_presenter, "select", None)
        if callable(select):
            select(conversation_id)
        selected = host.current_conversation
        if selected is not None and str(getattr(selected, "id", "") or "") == conversation_id:
            return selected
        return conversation

    def _show_revision_error(
        self,
        conversation_id: str,
        text: str,
        *,
        timeout_ms: int = 6000,
    ) -> None:
        self._host.chat_view.show_notice(
            str(text or "会话修订失败"),
            tone="error",
            timeout_ms=timeout_ms,
            conversation_id=conversation_id,
        )

    def _confirm_turn_restart(
        self,
        conversation: Conversation,
        target_user_message_id: str,
        *,
        action: str,
        destructive: bool = False,
        channel_action: bool = False,
    ) -> bool:
        conversation_id = str(conversation.id or "")
        real_users = [
            message
            for message in conversation.messages
            if is_real_user_message(message)
        ]
        target_index = next(
            (
                index
                for index, message in enumerate(real_users)
                if str(message.id or "") == str(target_user_message_id or "")
            ),
            -1,
        )
        following_turns = max(0, len(real_users) - target_index - 1) if target_index >= 0 else 0
        list_processes = getattr(
            getattr(self._host.services, "tool_manager", None),
            "list_processes",
            None,
        )
        process_count = 0
        try:
            if callable(list_processes):
                process_count = len(list_processes(conversation_id) or [])
        except Exception as exc:
            logger.debug("Failed to inspect background processes before revision: %s", exc)
        if following_turns <= 0 and process_count <= 0 and not destructive and not channel_action:
            return True
        details: list[str] = []
        if destructive:
            details.append(
                "将删除选中消息所属轮次"
                + (f"及后续 {following_turns} 轮对话。" if following_turns > 0 else "。")
            )
        elif following_turns > 0:
            details.append(f"将移除后续 {following_turns} 轮对话。")
        if process_count > 0:
            details.append(f"仍有 {process_count} 个 Shell 进程，操作不会停止这些进程。")
        details.append("已经执行的文件和工具操作不会撤销。")
        if channel_action:
            details.append("只修改 PyCat 记录，原频道消息不会被删除；编辑或重试会发送新的频道回复。")
        answer = QMessageBox.question(
            self._host,
            action,
            "\n".join(details) + f"\n\n是否继续{action}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _prepare_and_commit_message(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        user_message: Message,
        attachments: list[dict[str, Any]],
        composer_text: str,
    ) -> None:
        host = self._host
        conversation_id = str(conversation.id)
        if self.is_submitting(conversation_id):
            host.chat_view.show_notice(
                "正在准备附件，请稍候",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation_id,
            )
            self._restore_failed_submission(conversation_id, composer_text, attachments)
            return
        activity_token = host.services.conv_service.begin_lifecycle(conversation_id, "prepare-input")
        if activity_token is None:
            host.chat_view.show_notice(
                "该会话正在处理，请稍候",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation_id,
            )
            self._restore_failed_submission(conversation_id, composer_text, attachments)
            return

        def operation():
            return host.services.content_service.prepare_inputs(
                conversation,
                attachments,
                message_id=user_message.id,
            )

        def on_discard(result, _error) -> None:
            if isinstance(result, InputPreparationResult):
                self._cleanup_prepared_inputs(conversation, result.created_refs)
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)

        job = BackgroundJob(
            operation,
            on_discard=on_discard,
        )
        job.signals.finished.connect(
            lambda result, error, cid=conversation_id, active_job=job: self._finish_attachment_prepare(
                cid,
                active_job,
                activity_token,
                conversation,
                provider,
                user_message,
                attachments,
                composer_text,
                result,
                error,
            )
        )
        self._start_submission_job(conversation_id, job)

    def _is_current_conversation(self, conversation_id: str) -> bool:
        current = self._host.current_conversation
        return bool(
            current is not None
            and str(getattr(current, "id", "") or "") == str(conversation_id or "")
        )

    def _restore_failed_submission(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        if not self._is_current_conversation(conversation_id):
            return False
        restore = getattr(self._host.input_area, "restore_failed_message", None)
        return bool(callable(restore) and restore(content, attachments) is not False)

    def _confirm_current_submission(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        if not self._is_current_conversation(conversation_id):
            return False
        confirm = getattr(self._host.input_area, "confirm_message_sent", None)
        return bool(callable(confirm) and confirm(content, attachments) is not False)

    def _finish_attachment_prepare(
        self,
        conversation_id: str,
        job: BackgroundJob,
        activity_token: str,
        conversation: Conversation,
        provider: Provider,
        user_message: Message,
        attachments: list[dict[str, Any]],
        composer_text: str,
        result,
        error,
    ) -> None:
        host = self._host
        committed = False
        active = self._submission_jobs.get(conversation_id)
        if active is not job:
            if isinstance(result, InputPreparationResult):
                self._cleanup_prepared_inputs(conversation, result.created_refs)
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)
            return
        self._submission_jobs.pop(conversation_id, None)
        try:
            if error is not None:
                if isinstance(result, InputPreparationResult):
                    self._cleanup_prepared_inputs(conversation, result.created_refs)
                host.chat_view.show_notice(
                    f"附件准备失败：{error}",
                    tone="error",
                    timeout_ms=5000,
                    conversation_id=conversation_id,
                )
                self._restore_failed_submission(
                    conversation_id,
                    composer_text,
                    attachments,
                )
                return
            latest_conversation = host.services.conv_service.load(conversation_id)
            if latest_conversation is None:
                if isinstance(result, InputPreparationResult):
                    self._cleanup_prepared_inputs(conversation, result.created_refs)
                host.chat_view.show_notice(
                    "附件准备已取消：会话不存在",
                    tone="warning",
                    timeout_ms=5000,
                    conversation_id=conversation_id,
                )
                self._restore_failed_submission(
                    conversation_id,
                    composer_text,
                    attachments,
                )
                return
            preparation = result if isinstance(result, InputPreparationResult) else None
            if preparation is None:
                if isinstance(result, InputPreparationResult):
                    self._cleanup_prepared_inputs(conversation, result.created_refs)
                host.chat_view.show_notice(
                    "附件准备失败：返回结果无效",
                    tone="error",
                    timeout_ms=5000,
                    conversation_id=conversation_id,
                )
                self._restore_failed_submission(
                    conversation_id,
                    composer_text,
                    attachments,
                )
                return
            if preparation.failures:
                self._cleanup_prepared_inputs(
                    latest_conversation,
                    preparation.created_refs,
                )
                detail = "；".join(
                    f"{self._attachment_label(failure.source)}：{failure.error}"
                    for failure in preparation.failures[:3]
                )
                host.chat_view.show_notice(
                    f"附件准备失败：{detail}",
                    tone="error",
                    timeout_ms=8000,
                    conversation_id=conversation_id,
                )
                errors = {failure.source: failure.error for failure in preparation.failures}
                restored_attachments = [
                    {
                        **{key: value for key, value in attachment.items() if key != "error"},
                        **({"error": errors[str(attachment.get("path") or "")]} if str(attachment.get("path") or "") in errors else {}),
                    }
                    for attachment in attachments
                ]
                self._restore_failed_submission(
                    conversation_id,
                    composer_text,
                    restored_attachments,
                )
                return
            user_message.content_refs = list(preparation.refs)
            committed = self._commit_message(
                latest_conversation,
                provider,
                user_message,
                activity_token=activity_token,
                start_runtime=False,
            )
            if not committed:
                self._cleanup_prepared_inputs(
                    latest_conversation,
                    preparation.created_refs,
                )
                host.chat_view.show_notice(
                    "消息保存失败，请重试",
                    tone="error",
                    timeout_ms=5000,
                    conversation_id=conversation_id,
                )
                self._restore_failed_submission(
                    conversation_id,
                    composer_text,
                    [
                        {key: value for key, value in item.items() if key != "error"}
                        for item in attachments
                    ],
                )
                return
            self._confirm_current_submission(
                conversation_id,
                composer_text,
                attachments,
            )
            if committed:
                self.start_streaming(provider, conversation=latest_conversation, activity_token=activity_token)
        finally:
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)
            host.window_state_presenter.sync_input_enabled()

    @staticmethod
    def _attachment_label(source: str) -> str:
        value = str(source or "").strip()
        if value.startswith("data:"):
            return "粘贴图片"
        label = value.replace("\\", "/").rsplit("/", 1)[-1] or "附件"
        return label if len(label) <= 80 else label[:77] + "..."

    def _cleanup_prepared_inputs(self, conversation: Conversation, refs) -> None:
        try:
            self._host.services.content_service.cleanup_unreferenced(conversation, refs)
        except Exception as exc:
            logger.debug("Failed to clean unreferenced input snapshots: %s", exc)

    def _commit_message(
        self,
        conversation: Conversation,
        provider: Provider,
        user_message: Message,
        *,
        activity_token: str | None = None,
        start_runtime: bool = True,
        delegate_profile: str = '',
    ) -> bool:
        host = self._host
        owns_activity = activity_token is None
        if owns_activity:
            activity_token = host.services.conv_service.begin_lifecycle(conversation.id, "submit")
            if activity_token is None:
                return False
        try:
            try:
                host.services.conv_service.commit_message(conversation, user_message, activity_token=activity_token)
            except PersistenceError:
                return False

            is_current = bool(host.current_conversation and host.current_conversation.id == conversation.id)
            if is_current:
                host.current_conversation = conversation
                host.chat_view.bind_conversation(conversation)
                host.chat_view.add_message(user_message)

            conversations = host.services.conv_service.list_all()
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
            if is_current:
                host.sidebar.select_conversation(conversation.id)
            if is_current:
                host.services.app_coordinator.remember_current_conversation(
                    conversation,
                    providers=host.providers,
                    app_settings=host.app_settings,
                    is_streaming=False,
                )
                self._sync_header(conversation.id)

            if start_runtime:
                self.start_streaming(provider, conversation=conversation, activity_token=activity_token, delegate_profile=delegate_profile)
            return True
        finally:
            if owns_activity:
                host.services.conv_service.end_lifecycle(conversation.id, activity_token)


    def cancel_current_generation(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        host.message_runtime.cancel(host.current_conversation.id)

    def on_guidance_recovered(self, conversation_id: str, guidance: object) -> None:
        values = tuple(str(item).strip() for item in (guidance or ()) if str(item).strip())
        if not values:
            return
        existing = self._recovered_guidance.get(str(conversation_id or ""), ())
        self._recovered_guidance[str(conversation_id or "")] = existing + values
        self.restore_recovered_guidance(conversation_id)

    def restore_recovered_guidance(self, conversation_id: str) -> bool:
        key = str(conversation_id or "")
        values = self._recovered_guidance.get(key)
        current = self._host.current_conversation
        if not values or current is None or str(current.id) != key:
            return False
        restore = getattr(self._host.input_area, "restore_failed_message", None)
        if not callable(restore) or not restore("\n\n".join(values), []):
            return False
        self._recovered_guidance.pop(key, None)
        self._host.chat_view.show_notice(
            "未处理的补充要求已恢复到输入框",
            tone="warning",
            timeout_ms=5000,
            conversation_id=key,
        )
        return True

    # ------------------------------------------------------------------
    # Start streaming
    # ------------------------------------------------------------------

    def start_streaming(self, provider: Provider, *, conversation: Conversation | None = None, activity_token: str | None = None, delegate_profile: str = ''):
        return self._streaming_presenter.start_streaming(provider, conversation=conversation, activity_token=activity_token, delegate_profile=delegate_profile)

    def resume_interrupted(self, message_id: str) -> None:
        host = self._host
        conversation = host.current_conversation
        if conversation is None or host.message_runtime.is_streaming(conversation.id):
            return
        is_maintaining = getattr(
            getattr(host, "conversation_presenter", None),
            "is_maintaining",
            None,
        )
        if callable(is_maintaining) and is_maintaining(conversation.id):
            host.chat_view.show_notice(
                "该会话正在处理，请稍候",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation.id,
            )
            return
        message = next(
            (item for item in conversation.messages if str(getattr(item, "id", "") or "") == str(message_id or "")),
            None,
        )
        metadata = getattr(message, "metadata", {}) if message is not None else {}
        if message is None or not isinstance(metadata, dict) or not metadata.get("interrupted"):
            return

        provider_id = str(getattr(conversation, "provider_id", "") or host.input_area.get_selected_provider_id() or "")
        provider = self._find_provider(provider_id)
        if provider is None:
            QMessageBox.warning(host, "无法继续", "未找到该会话使用的服务商。")
            return

        metadata["resume_requested"] = True
        message.metadata = metadata
        host.chat_view.update_message(message)
        host.services.conv_service.save(conversation)
        state = self._streaming_presenter.start_streaming(
            provider,
            initial_runtime_messages=[Message(role="user", content=RESUME_INTERRUPTED_RUN)],
        )
        if state is None:
            metadata.pop("resume_requested", None)
            host.chat_view.update_message(message)
            host.services.conv_service.save(conversation)

    # ------------------------------------------------------------------
    # Streaming callbacks
    # ------------------------------------------------------------------

    def on_token(self, conversation_id: str, request_id: str, token: str) -> None:
        self._streaming_presenter.on_token(conversation_id, request_id, token)

    def on_thinking(self, conversation_id: str, request_id: str, thinking: str) -> None:
        self._streaming_presenter.on_thinking(conversation_id, request_id, thinking)

    def on_response_step(
        self, conversation_id: str, request_id: str, message: Message
    ) -> None:
        self._streaming_presenter.on_response_step(conversation_id, request_id, message)

    def on_response_complete(
        self, conversation_id: str, request_id: str, response
    ) -> None:
        self._streaming_presenter.on_response_complete(conversation_id, request_id, response)

    def on_response_error(
        self, conversation_id: str, request_id: str, error: str
    ) -> None:
        self._streaming_presenter.on_response_error(conversation_id, request_id, error)

    def on_run_finished(
        self,
        conversation_id: str,
        request_id: str,
        status: RunStatus,
    ) -> None:
        self._streaming_presenter.on_run_finished(conversation_id, request_id, status)

    def on_retry_attempt(
        self, conversation_id: str, request_id: str, detail: str
    ) -> None:
        self._streaming_presenter.on_retry_attempt(conversation_id, request_id, detail)

    def on_runtime_event(
        self,
        conversation_id: str,
        request_id: str,
        event: RunEvent,
    ) -> None:
        self._streaming_presenter.on_runtime_event(conversation_id, request_id, event)

    def on_conversation_patch(
        self,
        conversation_id: str,
        request_id: str,
        patch,
    ) -> None:
        self._streaming_presenter.on_conversation_patch(conversation_id, request_id, patch)

    def on_prompt_optimize_started(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        self._prompt_optimization_presenter.on_started(conversation_id, request_id)

    def on_prompt_optimize_complete(
        self,
        conversation_id: str,
        request_id: str,
        text: str,
    ) -> None:
        self._prompt_optimization_presenter.on_complete(conversation_id, request_id, text)

    def on_prompt_optimize_error(
        self,
        conversation_id: str,
        request_id: str,
        err: str,
    ) -> None:
        self._prompt_optimization_presenter.on_error(conversation_id, request_id, err)

    def on_prompt_optimize_cancelled(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        self._prompt_optimization_presenter.on_cancelled(conversation_id, request_id)

    def cancel_prompt_optimization(self) -> None:
        self._prompt_optimization_presenter.cancel()

    def request_prompt_optimization(self, raw_text: str) -> None:
        self._prompt_optimization_presenter.request(raw_text)

    # ------------------------------------------------------------------
    # Draft reuse and revision
    # ------------------------------------------------------------------

    def regenerate(self, assistant_message_id: str) -> None:
        """Restart the user turn that owns one persisted Assistant message."""
        host = self._host
        current = host.current_conversation
        if current is None:
            return
        conversation_id = str(current.id or "")
        if host.message_runtime.is_streaming(conversation_id):
            self._show_revision_error(conversation_id, "任务运行中不能重新生成")
            return
        if self.is_submitting(conversation_id):
            self._show_revision_error(conversation_id, "该会话正在提交消息，请稍候")
            return
        is_maintaining = getattr(
            getattr(host, "conversation_presenter", None),
            "is_maintaining",
            None,
        )
        if callable(is_maintaining) and is_maintaining(conversation_id):
            self._show_revision_error(conversation_id, "该会话正在处理，暂不能重新生成")
            return

        authoritative = host.services.conv_service.load(conversation_id)
        if authoritative is None:
            self._show_revision_error(conversation_id, "会话不存在")
            return
        target_user = self._user_for_assistant(
            authoritative.messages,
            assistant_message_id,
            allow_external=self._allows_channel_revisions(authoritative),
        )
        if target_user is None:
            self._show_revision_error(conversation_id, "找不到该回复对应的用户消息")
            return
        provider = host.services.conv_service.resolve_provider(
            host.providers,
            provider_id=authoritative.provider_id,
            provider_name=authoritative.provider_name,
        )
        if provider is None:
            self._show_revision_error(conversation_id, "未找到该会话使用的服务商")
            return
        if not self._confirm_turn_restart(
            authoritative,
            target_user.id,
            action="重新生成",
            channel_action=self._is_external_message(authoritative, target_user.id),
        ):
            return

        activity_token = host.services.conv_service.begin_lifecycle(
            conversation_id,
            "revision",
        )
        if activity_token is None:
            self._show_revision_error(conversation_id, "该会话正在运行或处理，暂不能重新生成")
            return
        fingerprint = host.services.conv_service.revision_fingerprint(authoritative)

        def operation():
            revision = host.services.conv_service.regenerate_user_turn(
                conversation_id,
                target_user.id,
                expected_fingerprint=fingerprint,
                activity_token=activity_token,
                allow_external=self._allows_channel_revisions(authoritative),
            )
            return InputPreparationResult(), revision

        def on_discard(_result, _error) -> None:
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)

        job = BackgroundJob(operation, on_discard=on_discard)
        job.signals.finished.connect(
            lambda result,
            error,
            active_job=job: self._finish_turn_restart(
                conversation_id=conversation_id,
                job=active_job,
                activity_token=activity_token,
                conversation_snapshot=authoritative,
                provider=provider,
                target_message_id=target_user.id,
                edited_content=None,
                attachments=[],
                result=result,
                error=error,
            )
        )
        self._start_submission_job(conversation_id, job)

    def delete_message(self, message_id: str) -> None:
        """Delete the selected message's complete turn in the background."""

        host = self._host
        current = host.current_conversation
        if current is None:
            return
        conversation_id = str(current.id or "")
        if host.message_runtime.is_streaming(conversation_id):
            self._show_revision_error(conversation_id, "任务运行中不能删除历史消息")
            return
        if self.is_submitting(conversation_id):
            self._show_revision_error(conversation_id, "该会话正在提交消息，请稍候")
            return
        is_maintaining = getattr(
            getattr(host, "conversation_presenter", None),
            "is_maintaining",
            None,
        )
        if callable(is_maintaining) and is_maintaining(conversation_id):
            self._show_revision_error(conversation_id, "该会话正在处理，暂不能删除")
            return

        authoritative = host.services.conv_service.load(conversation_id)
        if authoritative is None:
            self._show_revision_error(conversation_id, "会话不存在")
            return
        selected = next(
            (
                item
                for item in (authoritative.messages or [])
                if str(getattr(item, "id", "") or "") == str(message_id or "")
            ),
            None,
        )
        if selected is None or selected.role not in {"user", "assistant"}:
            self._show_revision_error(conversation_id, "该消息不能删除")
            return
        allow_external = self._allows_channel_revisions(authoritative)
        if selected.role == "assistant":
            target_user = self._user_for_assistant(
                authoritative.messages,
                selected.id,
                allow_external=allow_external,
            )
        else:
            target_user = restartable_user_by_id(
                authoritative.messages,
                selected.id,
                allow_external=allow_external,
            )
        if target_user is None:
            self._show_revision_error(
                conversation_id,
                "该消息不能删除，或会越过后续外部输入",
            )
            return
        if not self._confirm_turn_restart(
            authoritative,
            target_user.id,
            action="删除",
            destructive=True,
            channel_action=allow_external and bool(
                isinstance(getattr(target_user, "metadata", None), dict)
                and (
                    getattr(target_user, "metadata", {}).get("external_input")
                    or getattr(target_user, "metadata", {}).get("channel")
                )
            ),
        ):
            return

        activity_token = host.services.conv_service.begin_lifecycle(
            conversation_id,
            "revision",
        )
        if activity_token is None:
            self._show_revision_error(conversation_id, "该会话正在运行或处理，暂不能删除")
            return
        fingerprint = host.services.conv_service.revision_fingerprint(authoritative)

        def operation():
            revision = host.services.conv_service.remove_turn(
                conversation_id,
                selected.id,
                expected_fingerprint=fingerprint,
                activity_token=activity_token,
                allow_external=allow_external,
            )
            return InputPreparationResult(), revision

        def on_discard(_result, _error) -> None:
            host.services.conv_service.end_lifecycle(conversation_id, activity_token)

        job = BackgroundJob(operation, on_discard=on_discard)
        job.signals.finished.connect(
            lambda result,
            error,
            active_job=job: self._finish_turn_restart(
                conversation_id=conversation_id,
                job=active_job,
                activity_token=activity_token,
                conversation_snapshot=authoritative,
                provider=None,
                target_message_id=selected.id,
                edited_content=None,
                attachments=[],
                result=result,
                error=error,
                start_after=False,
            )
        )
        self._start_submission_job(conversation_id, job)

    @staticmethod
    def _allows_channel_revisions(conversation: Conversation | None) -> bool:
        """Allow external-message revision only for an explicitly bound Channel."""
        return is_bound_channel_conversation(conversation)

    @staticmethod
    def _is_external_message(conversation: Conversation | None, message_id: str) -> bool:
        if conversation is None:
            return False
        target = next(
            (
                message
                for message in (conversation.messages or [])
                if str(getattr(message, "id", "") or "") == str(message_id or "")
            ),
            None,
        )
        metadata = getattr(target, "metadata", {}) if target is not None else {}
        return isinstance(metadata, dict) and bool(
            metadata.get("external_input") or metadata.get("channel")
        )

    @staticmethod
    def _user_for_assistant(
        messages: list[Message],
        assistant_message_id: str,
        *,
        allow_external: bool = False,
    ) -> Message | None:
        return restartable_user_for_assistant(
            messages,
            assistant_message_id,
            allow_external=allow_external,
        )

    def edit(self, message_id: str) -> None:
        """Open one persisted real user turn in the composer for replacement."""
        host = self._host
        current = host.current_conversation
        if current is None:
            return
        conversation_id = str(current.id or "")
        if host.message_runtime.is_streaming(conversation_id):
            self._show_revision_error(conversation_id, "任务运行中不能编辑历史消息")
            return
        is_maintaining = getattr(
            getattr(host, "conversation_presenter", None),
            "is_maintaining",
            None,
        )
        if callable(is_maintaining) and is_maintaining(conversation_id):
            self._show_revision_error(conversation_id, "该会话正在处理，暂不能编辑")
            return

        authoritative = host.services.conv_service.load(conversation_id)
        if authoritative is None:
            self._show_revision_error(conversation_id, "会话不存在")
            return
        target_index = next(
            (
                index
                for index, item in enumerate(authoritative.messages or [])
                if str(item.id or "") == str(message_id or "")
            ),
            -1,
        )
        if target_index < 0:
            self._show_revision_error(conversation_id, "要编辑的消息不存在")
            return
        message = restartable_user_by_id(
            authoritative.messages,
            message_id,
            allow_external=self._allows_channel_revisions(authoritative),
        )
        if message is None:
            self._show_revision_error(
                conversation_id,
                "该消息不能编辑，或会越过后续外部输入",
            )
            return

        has_draft = getattr(host.input_area, "has_draft", None)
        existing_revision = getattr(host.input_area, "revision_state", None)
        if (
            (callable(has_draft) and has_draft())
            or (callable(existing_revision) and existing_revision())
        ):
            answer = QMessageBox.question(
                host,
                "替换当前草稿",
                "输入框已有内容。是否用所选消息替换当前草稿？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        text, attachments = self._draft_for_message(authoritative, message)
        real_users = [
            item
            for item in authoritative.messages
            if is_real_user_message(item)
        ]
        turn_number = next(
            (
                index + 1
                for index, item in enumerate(real_users)
                if str(item.id or "") == str(message.id or "")
            ),
            1,
        )
        following_turns = max(0, len(real_users) - turn_number)
        begin_revision = getattr(host.input_area, "begin_revision", None)
        if callable(begin_revision):
            begin_revision(
                conversation_id=conversation_id,
                message_id=message.id,
                fingerprint=host.services.conv_service.revision_fingerprint(authoritative),
                turn_number=turn_number,
                following_turns=following_turns,
                content=text,
                attachments=attachments,
            )

    def reuse(self, message_id: str) -> None:
        """Load one user message into the composer without changing history."""
        host = self._host
        conversation = host.current_conversation
        if conversation is None:
            return
        message = next(
            (
                item
                for item in (conversation.messages or [])
                if item.id == message_id and item.role == "user"
            ),
            None,
        )
        if message is None:
            return

        text, attachments = self._draft_for_message(conversation, message)
        loader = getattr(host.input_area, "load_draft", None)
        if callable(loader):
            loader(text, attachments)

    def _draft_for_message(
        self,
        conversation: Conversation,
        message: Message,
    ) -> tuple[str, list[dict[str, Any]]]:
        attachments: list[dict[str, Any]] = []
        host = self._host
        resolver = SessionContentResolver(host.services.content_service)
        for ref in message.content_refs or []:
            attachment = {
                "path": str(getattr(ref, "ref", "") or ""),
                "type": "image" if str(getattr(ref, "mime", "") or "").startswith("image/") else "file",
                "name": str(getattr(ref, "name", "") or ""),
                "mime": str(getattr(ref, "mime", "") or ""),
            }
            try:
                attachment["path"] = str(resolver.resolve(conversation, ref))
            except Exception as exc:
                attachment["error"] = f"附件不可用：{exc}"
            attachments.append(attachment)
        for source in message.images or []:
            value = str(source or "").strip()
            if not value:
                continue
            attachment = {"path": value, "type": "image"}
            if value.startswith("input:"):
                try:
                    loaded = host.services.content_service.load_ref(conversation, value)
                    attachment.update(
                        path=str(resolver.resolve(conversation, loaded)),
                        name=str(loaded.name or ""),
                        mime=str(loaded.mime or ""),
                    )
                except Exception as exc:
                    attachment["error"] = f"附件不可用：{exc}"
            attachments.append(attachment)

        text = extract_user_request(
            extract_composer_text(message.content, message.metadata)
        )
        return text, attachments

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_provider(self, provider_id: str):
        for p in self._host.providers:
            if p.id == provider_id:
                return p
        return None

    def _sync_header(self, conversation_id: str) -> None:
        presenter = getattr(self._host, "window_state_presenter", None)
        if presenter is None or not hasattr(presenter, "sync_chat_header_for_current_conversation"):
            return
        try:
            presenter.sync_chat_header_for_current_conversation(conversation_id)
        except Exception as exc:
            logger.debug("Failed to sync chat header after message update: %s", exc)
