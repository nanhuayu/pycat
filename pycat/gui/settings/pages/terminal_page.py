"""One shell selector with advanced settings shared by all desktop platforms."""
import json

from PyQt6.QtWidgets import QComboBox, QLabel, QSpinBox, QVBoxLayout, QWidget

from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.widgets.collapsible_section import CollapsibleSection
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.models.contracts.config import ShellConfig


class TerminalPage(QWidget):
    page_title = "终端"

    def __init__(self, shell_config: ShellConfig, parent=None, *, embedded=False, shell_choices=()):
        super().__init__(parent)
        config = shell_config or ShellConfig()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        if not embedded:
            layout.addWidget(build_page_header("终端", "选择本机 Shell。SSH 命令由远端环境执行。"))
        section = FormSection("默认 Shell")
        self.backend_combo = QComboBox()
        configure_combo_popup(self.backend_combo)
        self.backend_combo.addItem("自动选择", ("auto", ""))
        for kind, executable in shell_choices:
            self.backend_combo.addItem(f"{kind} · {executable}", (kind, executable))
        self.backend_combo.addItem("自定义", ("custom", ""))
        current = (config.backend, config.executable)
        index = self.backend_combo.findData(current)
        if index < 0:
            self.backend_combo.addItem(f"已配置：{config.backend}", current)
            index = self.backend_combo.count() - 1
        self.backend_combo.setCurrentIndex(index)
        section.form.addRow("Shell", self.backend_combo)
        self.bang_behavior_combo = QComboBox()
        self.bang_behavior_combo.addItem("执行 Shell 命令", "shell")
        self.bang_behavior_combo.addItem("交给 Agent", "agent")
        self.bang_behavior_combo.setCurrentIndex(max(0, self.bang_behavior_combo.findData(config.bang_command_behavior)))
        section.form.addRow("! 命令", self.bang_behavior_combo)
        self.wait_seconds_spin = QSpinBox()
        self.wait_seconds_spin.setRange(5, 600)
        self.wait_seconds_spin.setSuffix(" 秒")
        self.wait_seconds_spin.setValue(config.wait_seconds)
        section.form.addRow("命令等待上限", self.wait_seconds_spin)
        layout.addWidget(section.group)

        advanced = CollapsibleSection("高级设置", collapsed=True)
        fields = FormSection("")
        fields.group.findChild(QLabel, "settings_section_title").hide()
        self.executable_edit = ThemedLineEdit(config.executable)
        self.executable_edit.setPlaceholderText("留空使用所选 Shell 的默认程序")
        fields.form.addRow("可执行文件", self.executable_edit)
        self.arguments_edit = ThemedLineEdit(json.dumps(list(config.arguments), ensure_ascii=False))
        self.arguments_edit.setPlaceholderText('参数数组，例如 ["--login"]')
        fields.form.addRow("启动参数", self.arguments_edit)
        self.wsl_distro_edit = ThemedLineEdit(config.wsl_distro)
        self.wsl_distro_edit.setPlaceholderText("留空使用默认发行版")
        self.wsl_label = QLabel("WSL 发行版")
        fields.form.addRow(self.wsl_label, self.wsl_distro_edit)
        self.encoding_combo = QComboBox()
        for label, value in [("自动检测", "auto"), ("UTF-8", "utf-8"), ("UTF-16", "utf-16"),
                             ("系统默认", "system"), ("GB18030", "gb18030")]:
            self.encoding_combo.addItem(label, value)
        self.encoding_combo.setCurrentIndex(max(0, self.encoding_combo.findData(config.output_encoding)))
        fields.form.addRow("管道日志编码", self.encoding_combo)
        self.inherit_env_check = fields.add_checkbox("继承当前进程环境变量", checked=config.inherit_env)
        advanced.body_layout.addWidget(fields.group)
        layout.addWidget(advanced)
        self.preview_label = QLabel("等待到期后命令继续在后台运行，可从输入区的 Shell 图标查看输出或结束进程。")
        self.preview_label.setWordWrap(True)
        self.preview_label.setProperty("muted", True)
        layout.addWidget(self.preview_label)
        layout.addStretch()
        self.backend_combo.currentIndexChanged.connect(self._selection_changed)
        self.wsl_label.setVisible(config.backend == "wsl")
        self.wsl_distro_edit.setVisible(config.backend == "wsl")

    def _selection_changed(self):
        kind, executable = self.backend_combo.currentData()
        self.executable_edit.setText(executable)
        self.wsl_distro_edit.setEnabled(kind == "wsl")
        self.wsl_label.setVisible(kind == "wsl")
        self.wsl_distro_edit.setVisible(kind == "wsl")

    def collect(self):
        args = json.loads(self.arguments_edit.text() or "[]")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("Shell 启动参数必须是字符串数组")
        config = ShellConfig(
            backend=self.backend_combo.currentData()[0], executable=self.executable_edit.text().strip(),
            arguments=tuple(args), wsl_distro=self.wsl_distro_edit.text().strip(),
            output_encoding=self.encoding_combo.currentData(), inherit_env=self.inherit_env_check.isChecked(),
            bang_command_behavior=self.bang_behavior_combo.currentData(), wait_seconds=self.wait_seconds_spin.value(),
        )
        return {"shell": config.to_dict()}
