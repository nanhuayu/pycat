"""Display-only runtime projection; event keys, never labels, select behavior."""
from dataclasses import dataclass

from PyQt6.QtCore import QCoreApplication

from pycat.models.streaming import ConversationStreamState


@dataclass(frozen=True)
class RuntimeStatusView:
    title: str
    detail: str
    active: bool
    hint: str = ""


def project_runtime_status(
    stream_state: ConversationStreamState | None,
    *,
    operation: str = "",
    pending_guidance: int = 0,
    pending_interactions: int = 0,
) -> RuntimeStatusView:
    """Format one snapshot without changing run state, evidence or user text."""
    if pending_interactions:
        title = QCoreApplication.translate("RuntimeStatus", "等待回复")
        detail = QCoreApplication.translate("RuntimeStatus", "此会话有 {count} 项问题或工具确认等待你处理。")
        return RuntimeStatusView(title, detail.format(count=pending_interactions), True, title + "…")

    operation_key = str(operation or "").strip().lower()
    if operation_key and operation_key != "turn":
        operations = {
            "prepare-input": (QCoreApplication.translate("RuntimeStatus", "准备附件"),
                              QCoreApplication.translate("RuntimeStatus", "正在创建会话附件快照")),
            "revision": (QCoreApplication.translate("RuntimeStatus", "修订中"),
                         QCoreApplication.translate("RuntimeStatus", "正在替换消息并重建活动会话历史")),
            "compact": (QCoreApplication.translate("RuntimeStatus", "压缩中"),
                        QCoreApplication.translate("RuntimeStatus", "正在压缩当前会话上下文")),
            "workspace": (QCoreApplication.translate("RuntimeStatus", "迁移中"),
                          QCoreApplication.translate("RuntimeStatus", "正在迁移当前会话文件")),
            "delete": (QCoreApplication.translate("RuntimeStatus", "删除中"),
                       QCoreApplication.translate("RuntimeStatus", "正在删除当前会话")),
        }
        title, detail = operations.get(operation_key, (
            QCoreApplication.translate("RuntimeStatus", "处理中"),
            QCoreApplication.translate("RuntimeStatus", "正在处理当前会话"),
        ))
        return RuntimeStatusView(title, detail, True, title + "…")
    if stream_state is None:
        return RuntimeStatusView(QCoreApplication.translate("RuntimeStatus", "空闲"),
                                 QCoreApplication.translate("RuntimeStatus", "等待下一次请求"), False)

    active_tool = str(getattr(stream_state, "active_tool", "") or "").strip()
    kind = str(getattr(stream_state, "last_event_kind", "") or "").strip()
    last_detail = str(getattr(stream_state, "last_event_detail", "") or "").strip()
    events = getattr(stream_state, 'recent_events', ()) or ()
    last_event = events[-1] if events else {}
    model = str(getattr(stream_state, "model", "") or "").strip()
    generating_hint = QCoreApplication.translate("RuntimeStatus", "正在生成…")
    if active_tool:
        title = QCoreApplication.translate("RuntimeStatus", "工具 · {name}").format(name=active_tool)
        detail = last_detail or QCoreApplication.translate("RuntimeStatus", "正在等待工具返回")
        waiting = title + "…"
    elif kind == 'condense' and last_event.get('phase') == 'end':
        title = QCoreApplication.translate("RuntimeStatus", "生成中")
        detail = QCoreApplication.translate("RuntimeStatus", "正在等待模型响应")
        waiting = generating_hint
    elif kind == 'complete':
        title = {
            'cancelled': QCoreApplication.translate("RuntimeStatus", "已停止"),
            'interrupted': QCoreApplication.translate("RuntimeStatus", "未完成"),
        }.get(last_event.get('status'), QCoreApplication.translate("RuntimeStatus", "已完成"))
        detail = last_detail or title
        waiting = title
    elif kind:
        labels = {
            "turn_start": QCoreApplication.translate("RuntimeStatus", "开始执行"),
            "tool_start": QCoreApplication.translate("RuntimeStatus", "工具中"),
            "tool_end": QCoreApplication.translate("RuntimeStatus", "工具完成"),
            "retry": QCoreApplication.translate("RuntimeStatus", "重试中"),
            "error": QCoreApplication.translate("RuntimeStatus", "出错"),
            "step": QCoreApplication.translate("RuntimeStatus", "处理中"),
            "condense": QCoreApplication.translate("RuntimeStatus", "压缩中"),
            "text_delta": QCoreApplication.translate("RuntimeStatus", "生成中"),
            "thinking_delta": QCoreApplication.translate("RuntimeStatus", "思考中"),
        }
        title = labels.get(kind, QCoreApplication.translate("RuntimeStatus", "运行中"))
        detail = last_detail or "-"
        if kind in {"turn_start", "tool_end", "text_delta"}:
            waiting = generating_hint
        elif kind == "error":
            waiting = title
        else:
            waiting = title + "…"
    else:
        title = QCoreApplication.translate("RuntimeStatus", "生成中")
        detail = model or QCoreApplication.translate("RuntimeStatus", "正在等待模型响应")
        waiting = generating_hint

    pending = max(0, int(pending_guidance or 0))
    if pending:
        title = QCoreApplication.translate("RuntimeStatus", "待处理 {count}").format(count=pending)
        detail = QCoreApplication.translate("RuntimeStatus", "{detail}；当前步骤完成后处理补充要求").format(detail=detail)
        waiting = title + "…"
    return RuntimeStatusView(title, detail, True, waiting)
