"""Small example-based old/new comparison, backed by the shared runtime."""
import threading
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QComboBox, QLabel, QPushButton, QHeaderView
from pycat.gui.runtime.background_job import BackgroundJob


class SkillEvaluationDialog(QDialog):
    def __init__(self, candidate, *, evaluator, parent=None):
        super().__init__(parent)
        self._candidate = candidate
        self._evaluator = evaluator
        self._cancel = threading.Event()
        self._job = None
        self.setWindowTitle("技能对照试验")
        self.resize(720, 480)
        self.setMinimumSize(500, 380)
        layout = QVBoxLayout(self)
        self.status = QLabel("填写正常、失败、不适用和保留样例，分别比较原方法与候选方法。所有样例都通过且优于原方法后，才可发布。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(4, 4)
        self.table.setHorizontalHeaderLabels(["类型", "输入", "应包含", "不应包含"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(65)
        self.table.verticalHeader().hide()
        kinds = [("正常", "normal"), ("失败", "failure"), ("不适用", "inapplicable"), ("保留样例", "heldout")]
        for row in range(4):
            combo = QComboBox()
            for label, value in kinds:
                combo.addItem(label, value)
            combo.setCurrentIndex(row)
            self.table.setCellWidget(row, 0, combo)
            for column in range(1, 4):
                self.table.setItem(row, column, QTableWidgetItem(""))
        layout.addWidget(self.table, 1)
        self.run = QPushButton("开始对照试验")
        self.run.clicked.connect(self._start)
        layout.addWidget(self.run)

    def _start(self):
        suite = []
        for row in range(self.table.rowCount()):
            kind = self.table.cellWidget(row, 0).currentData()
            prompt = self.table.item(row, 1).text().strip()
            contains = self.table.item(row, 2).text().strip()
            excludes = self.table.item(row, 3).text().strip()
            if not prompt or not contains:
                self.status.setText("每个样例都需要输入和明确的预期结果。")
                return
            suite.append({"kind": "normal" if kind == "heldout" else kind,
                          "split": "heldout" if kind == "heldout" else "train", "prompt": prompt,
                          "contains": [contains], "excludes": [excludes] if excludes else []})
        self.run.setEnabled(False)
        self.table.setEnabled(False)
        self.status.setText("正在比较原方法与候选方法…")
        job = BackgroundJob(lambda: self._evaluator(self._candidate, suite, self._cancel))
        self._job = job
        def finished(report, error):
            self._job = None
            self.run.setEnabled(True)
            self.table.setEnabled(True)
            self.status.setText(str(error) if error else
                f"原方法 {report['before_passed']}/{report['case_count']}，候选 {report['after_passed']}/{report['case_count']}。" +
                ("试验通过，可以发布。" if report["passed"] else "未达到发布条件，现有方法保持不变。"))
            if not error and report.get("cost"):
                costs = report["cost"]
                self.status.setText(self.status.text() + "\n" + "；".join(
                    f"{label} {costs[key]['elapsed_ms'] / 1000:.1f} 秒，" +
                    (f"{costs[key]['tokens']} tokens" if costs[key].get("tokens") is not None else "用量未返回")
                    for key, label in (("before", "原方法"), ("after", "候选方法"))))
        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def closeEvent(self, event):
        self._cancel.set()
        if self._job:
            self._job.abandon()
        super().closeEvent(event)
