"""One shell selector with advanced settings shared by all desktop platforms."""
import json

from PyQt6.QtCore import QCoreApplication
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
            layout.addWidget(build_page_header(QCoreApplication.translate('TerminalPage', '终端'), QCoreApplication.translate('TerminalPage', '选择本机 Shell。SSH 命令由远端环境执行。')))
        section = FormSection(QCoreApplication.translate('TerminalPage', '默认 Shell'))
        self.backend_combo = QComboBox()
        configure_combo_popup(self.backend_combo)
        self.backend_combo.addItem(QCoreApplication.translate('TerminalPage', '自动选择'), ("auto", ""))
        for kind, executable in shell_choices:
            self.backend_combo.addItem(f"{kind} · {executable}", (kind, executable))
        self.backend_combo.addItem(QCoreApplication.translate('TerminalPage', '自定义'), ("custom", ""))
        current = (config.backend, config.executable)
        # Qt treats Python tuple user data as opaque objects; compare values
        # in Python so reopening settings selects the existing shell row.
        index = next((i for i in range(self.backend_combo.count())
                      if self.backend_combo.itemData(i) == current), -1)
        if index < 0:
            self.backend_combo.addItem(QCoreApplication.translate('TerminalPage', '已配置：{backend}').format(backend=config.backend), current)
            index = self.backend_combo.count() - 1
        self.backend_combo.setCurrentIndex(index)
        section.form.addRow("Shell", self.backend_combo)
        self.bang_behavior_combo = QComboBox()
        self.bang_behavior_combo.addItem(QCoreApplication.translate('TerminalPage', '执行 Shell 命令'), "shell")
        self.bang_behavior_combo.addItem(QCoreApplication.translate('TerminalPage', '交给 Agent'), "agent")
        self.bang_behavior_combo.setCurrentIndex(max(0, self.bang_behavior_combo.findData(config.bang_command_behavior)))
        section.form.addRow(QCoreApplication.translate('TerminalPage', '! 命令'), self.bang_behavior_combo)
        self.wait_seconds_spin = QSpinBox()
        self.wait_seconds_spin.setRange(5, 600)
        self.wait_seconds_spin.setSuffix(QCoreApplication.translate('TerminalPage', ' 秒'))
        self.wait_seconds_spin.setValue(config.wait_seconds)
        section.form.addRow(QCoreApplication.translate('TerminalPage', '命令等待上限'), self.wait_seconds_spin)
        layout.addWidget(section.group)

        advanced = CollapsibleSection(QCoreApplication.translate('TerminalPage', '高级设置'), collapsed=True)
        fields = FormSection("")
        fields.group.findChild(QLabel, "settings_section_title").hide()
        self.executable_edit = ThemedLineEdit(config.executable)
        self.executable_edit.setPlaceholderText(QCoreApplication.translate('TerminalPage', '留空使用所选 Shell 的默认程序'))
        fields.form.addRow(QCoreApplication.translate('TerminalPage', '可执行文件'), self.executable_edit)
        self.arguments_edit = ThemedLineEdit(json.dumps(list(config.arguments), ensure_ascii=False))
        self.arguments_edit.setPlaceholderText(QCoreApplication.translate('TerminalPage', '参数数组，例如 ["--login"]'))
        fields.form.addRow(QCoreApplication.translate('TerminalPage', '启动参数'), self.arguments_edit)
        self.wsl_distro_edit = ThemedLineEdit(config.wsl_distro)
        self.wsl_distro_edit.setPlaceholderText(QCoreApplication.translate('TerminalPage', '留空使用默认发行版'))
        self.wsl_label = QLabel(QCoreApplication.translate('TerminalPage', 'WSL 发行版'))
        fields.form.addRow(self.wsl_label, self.wsl_distro_edit)
        self.encoding_combo = QComboBox()
        for label, value in [(QCoreApplication.translate('TerminalPage', '自动检测'), "auto"), ("UTF-8", "utf-8"), ("UTF-16", "utf-16"),
                             (QCoreApplication.translate('TerminalPage', '系统默认'), "system"), ("GB18030", "gb18030")]:
            self.encoding_combo.addItem(label, value)
        self.encoding_combo.setCurrentIndex(max(0, self.encoding_combo.findData(config.output_encoding)))
        fields.form.addRow(QCoreApplication.translate('TerminalPage', '管道日志编码'), self.encoding_combo)
        self.inherit_env_check = fields.add_checkbox(QCoreApplication.translate('TerminalPage', '继承当前进程环境变量'), checked=config.inherit_env)
        advanced.body_layout.addWidget(fields.group)
        layout.addWidget(advanced)
        self.preview_label = QLabel(QCoreApplication.translate('TerminalPage', '等待到期后命令继续在后台运行，可从输入区的 Shell 图标查看输出或结束进程。'))
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
            raise ValueError(QCoreApplication.translate('TerminalPage', 'Shell 启动参数必须是字符串数组'))
        config = ShellConfig(
            backend=self.backend_combo.currentData()[0], executable=self.executable_edit.text().strip(),
            arguments=tuple(args), wsl_distro=self.wsl_distro_edit.text().strip(),
            output_encoding=self.encoding_combo.currentData(), inherit_env=self.inherit_env_check.isChecked(),
            bang_command_behavior=self.bang_behavior_combo.currentData(), wait_seconds=self.wait_seconds_spin.value(),
        )
        return {"shell": config.to_dict()}
