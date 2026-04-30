from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from core.channel.runtime import ChannelConnectionSnapshot, ChannelRuntimeService
from core.config.schema import ChannelConfig
from ui.utils.icon_manager import Icons
from ui.utils.qr_code import build_qr_code_pixmap


class WeChatQRConnectDialog(QDialog):
    """Popup flow for WeChat QR bridge pairing."""

    def __init__(
        self,
        *,
        channel: ChannelConfig,
        channel_runtime: ChannelRuntimeService,
        force_new: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._channel = channel
        self._channel_runtime = channel_runtime
        self._force_new = bool(force_new)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_snapshot)
        self._snapshot: ChannelConnectionSnapshot | None = None

        self._setup_ui()
        QTimer.singleShot(0, self._start_flow)

    @property
    def snapshot(self) -> ChannelConnectionSnapshot | None:
        return self._snapshot

    def _setup_ui(self) -> None:
        self.setWindowTitle("微信二维码连接")
        self.setObjectName("wechat_qr_connect_dialog")
        self.setModal(True)
        self.setMinimumSize(420, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel("像 Cherry Studio 一样，扫码完成后会自动接入当前微信实例。")
        header.setWordWrap(True)
        header.setProperty("muted", True)
        layout.addWidget(header)

        self.status_label = QLabel("连接状态：准备中")
        self.status_label.setProperty("heading", True)
        layout.addWidget(self.status_label)

        self.detail_label = QLabel("正在准备二维码连接向导…")
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("muted", True)
        layout.addWidget(self.detail_label)

        self.qr_label = QLabel("二维码将在这里显示")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumSize(280, 280)
        layout.addWidget(self.qr_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.meta_label = QLabel("连接成功后会显示账号与会话信息。")
        self.meta_label.setWordWrap(True)
        self.meta_label.setProperty("muted", True)
        layout.addWidget(self.meta_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.regenerate_btn = QPushButton("重新生成二维码")
        self.regenerate_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        self.regenerate_btn.clicked.connect(self._regenerate_snapshot)
        actions.addWidget(self.regenerate_btn)

        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        actions.addWidget(self.close_btn)

        layout.addLayout(actions)

    def _start_flow(self) -> None:
        self._request_snapshot(force_new=self._force_new)

    def _poll_snapshot(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        if str(snapshot.status or "").strip().lower() in {"connected", "ready", "expired", "error"}:
            self._poll_timer.stop()
            return
        self._request_snapshot(force_new=False)

    def _regenerate_snapshot(self) -> None:
        self._request_snapshot(force_new=True)

    def _request_snapshot(self, *, force_new: bool) -> None:
        try:
            snapshot = self._channel_runtime.refresh_wechat_connection(self._channel, force_new=force_new)
        except Exception as exc:
            self._poll_timer.stop()
            self._snapshot = None
            self.status_label.setText("连接状态：异常")
            self.detail_label.setText(f"二维码连接失败：{exc}")
            self.qr_label.clear()
            self.qr_label.setText("无法生成二维码")
            self.meta_label.setText("请检查扫码桥接地址、网络连通性或 token 配置。")
            return

        self._snapshot = snapshot
        self._channel = self._apply_snapshot_to_channel(snapshot)
        self._render_snapshot(snapshot)

        status = str(snapshot.status or "").strip().lower()
        if status in {"connected", "ready"}:
            self._poll_timer.stop()
            self.close_btn.setText("已连接，正在继续…")
            self.close_btn.setEnabled(False)
            QTimer.singleShot(450, self.accept)
            return

        if status in {"expired", "error"}:
            self._poll_timer.stop()
            return

        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def _render_snapshot(self, snapshot: ChannelConnectionSnapshot) -> None:
        status_text = {
            "draft": "草稿",
            "disconnected": "未连接",
            "pending": "待扫码",
            "waiting": "待扫码",
            "scanned": "已扫码待确认",
            "connected": "已连接",
            "ready": "已连接",
            "expired": "已失效",
            "error": "异常",
        }.get(str(snapshot.status or "").strip().lower(), str(snapshot.status or "未连接"))
        self.status_label.setText(f"连接状态：{status_text}")
        self.detail_label.setText(str(snapshot.detail or "请使用微信扫描二维码完成连接。"))

        if snapshot.qr_text:
            pixmap = build_qr_code_pixmap(snapshot.qr_text, size=280)
            if not pixmap.isNull():
                self.qr_label.setPixmap(pixmap)
                self.qr_label.setText("")
            else:
                self.qr_label.clear()
                self.qr_label.setText("二维码渲染失败")
        else:
            self.qr_label.clear()
            self.qr_label.setText("当前没有可显示的二维码")

        meta_parts: list[str] = []
        if snapshot.account_name:
            meta_parts.append(f"账号：{snapshot.account_name}")
        if snapshot.session_id:
            meta_parts.append(f"二维码会话：{snapshot.session_id}")
        if snapshot.expires_at:
            meta_parts.append(f"过期：{snapshot.expires_at}")
        self.meta_label.setText(" · ".join(meta_parts) if meta_parts else "连接成功后会显示账号与会话信息。")

        status = str(snapshot.status or "").strip().lower()
        self.regenerate_btn.setEnabled(status not in {"connected", "ready"})

    def done(self, result: int) -> None:
        self._poll_timer.stop()
        super().done(result)

    def _apply_snapshot_to_channel(self, snapshot: ChannelConnectionSnapshot) -> ChannelConfig:
        payload = self._channel.to_dict()
        config = dict(payload.get("config", {}) or {})
        config.update(snapshot.to_config_patch())
        payload["config"] = config
        return ChannelConfig.from_dict(payload)
