from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QGroupBox,
    QFormLayout,
    QComboBox,
    QLineEdit,
    QCheckBox,
    QLabel,
)

from core.config.schema import ShellConfig
from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup


class TerminalPage(QWidget):
    page_title = "终端"

    def __init__(self, shell_config: ShellConfig, parent=None):
        super().__init__(parent)
        self._setup_ui(shell_config or ShellConfig())

    def _setup_ui(self, shell_config: ShellConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("终端", "统一选择命令执行宿主、编码与默认可执行文件。"))

        backend_group = QGroupBox("宿主 Shell")
        backend_layout = QFormLayout(backend_group)

        self.backend_combo = QComboBox()
        configure_combo_popup(self.backend_combo)
        self.backend_combo.addItem("CMD（推荐）", "cmd")
        self.backend_combo.addItem("PowerShell", "powershell")
        self.backend_combo.addItem("WSL", "wsl")
        current_backend = str(getattr(shell_config, "backend", "cmd") or "cmd").strip().lower()
        idx = self.backend_combo.findData(current_backend)
        self.backend_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.backend_combo.currentIndexChanged.connect(self._update_preview)
        backend_layout.addRow("默认后端:", self.backend_combo)

        self.encoding_combo = QComboBox()
        configure_combo_popup(self.encoding_combo)
        self.encoding_combo.addItem("自动检测", "auto")
        self.encoding_combo.addItem("UTF-8", "utf-8")
        self.encoding_combo.addItem("系统默认", "system")
        self.encoding_combo.addItem("GB18030", "gb18030")
        encoding_value = str(getattr(shell_config, "output_encoding", "auto") or "auto").strip().lower()
        encoding_idx = self.encoding_combo.findData(encoding_value)
        self.encoding_combo.setCurrentIndex(encoding_idx if encoding_idx >= 0 else 0)
        backend_layout.addRow("输出编码:", self.encoding_combo)

        self.inherit_env_check = QCheckBox("继承当前进程环境变量")
        self.inherit_env_check.setChecked(bool(getattr(shell_config, "inherit_env", True)))
        backend_layout.addRow("环境:", self.inherit_env_check)
        layout.addWidget(backend_group)

        exec_group = QGroupBox("后端可执行文件")
        exec_layout = QFormLayout(exec_group)

        self.cmd_edit = QLineEdit()
        self.cmd_edit.setText(str(getattr(shell_config, "cmd_executable", "") or ""))
        self.cmd_edit.setPlaceholderText("留空则使用系统 COMSPEC / cmd.exe")
        exec_layout.addRow("CMD:", self.cmd_edit)

        self.powershell_edit = QLineEdit()
        self.powershell_edit.setText(str(getattr(shell_config, "powershell_executable", "powershell.exe") or "powershell.exe"))
        self.powershell_edit.setPlaceholderText("powershell.exe")
        exec_layout.addRow("PowerShell:", self.powershell_edit)

        self.wsl_edit = QLineEdit()
        self.wsl_edit.setText(str(getattr(shell_config, "wsl_executable", "wsl.exe") or "wsl.exe"))
        self.wsl_edit.setPlaceholderText("wsl.exe")
        exec_layout.addRow("WSL:", self.wsl_edit)

        self.wsl_distro_edit = QLineEdit()
        self.wsl_distro_edit.setText(str(getattr(shell_config, "wsl_distro", "") or ""))
        self.wsl_distro_edit.setPlaceholderText("可选，例如 Ubuntu")
        exec_layout.addRow("WSL 发行版:", self.wsl_distro_edit)
        layout.addWidget(exec_group)

        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setProperty("muted", True)
        layout.addWidget(self.preview_label)

        hint = QLabel(
            "这里控制 execute_command / shell_start / shell_status / shell_logs / shell_wait / shell_kill 的默认宿主后端。"
            "本轮先提供统一入口与管理能力，不引入完整 PTY/终端页签系统。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()
        self._update_preview()

    def _update_preview(self) -> None:
        backend = str(self.backend_combo.currentData() or "cmd")
        if backend == "powershell":
            text = "当前会使用 PowerShell 作为命令执行宿主，适合对象管道和 PowerShell 脚本。"
        elif backend == "wsl":
            distro = (self.wsl_distro_edit.text() or "").strip()
            text = f"当前会使用 WSL 作为命令执行宿主{'（发行版: ' + distro + '）' if distro else ''}，适合 Linux 工具链。"
        else:
            text = "当前会使用 CMD 作为命令执行宿主，兼容性最好，适合作为默认 Windows 后端。"
        self.preview_label.setText(text)

    def collect(self) -> dict:
        config = ShellConfig(
            backend=str(self.backend_combo.currentData() or "cmd").strip().lower() or "cmd",
            cmd_executable=(self.cmd_edit.text() or "").strip(),
            powershell_executable=(self.powershell_edit.text() or "powershell.exe").strip() or "powershell.exe",
            wsl_executable=(self.wsl_edit.text() or "wsl.exe").strip() or "wsl.exe",
            wsl_distro=(self.wsl_distro_edit.text() or "").strip(),
            output_encoding=str(self.encoding_combo.currentData() or "auto").strip().lower() or "auto",
            inherit_env=bool(self.inherit_env_check.isChecked()),
        )
        return {"shell": config.to_dict(), "shell_backend": config.backend}
