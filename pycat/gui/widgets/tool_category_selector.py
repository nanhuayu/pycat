"""Shared tool-category ceiling editor for settings and dialogs."""
from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QCheckBox, QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from pycat.gui.view_models.tooling_labels import tool_category_label
from pycat.models.contracts.tooling import TOOL_CATEGORIES, ToolSelectionPolicy


class ToolCategorySelector(QWidget):
    def __init__(
        self,
        parent=None,
        *,
        columns: int = 4,
        object_prefix: str = "tool_category",
        allow_inherit: bool = True,
        show_ids: bool = False,
    ) -> None:
        super().__init__(parent)
        self._allow_inherit = bool(allow_inherit)
        self._inheriting = bool(allow_inherit)
        self._loading = False
        self._ceiling = set(TOOL_CATEGORIES)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.inheritance_label = QLabel("")
        self.inheritance_label.setProperty("muted", True)
        self.inheritance_label.setWordWrap(True)
        self.inheritance_label.setVisible(False)
        layout.addWidget(self.inheritance_label)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.checks: dict[str, QCheckBox] = {}
        column_count = max(1, int(columns or 1))
        for index, category in enumerate(TOOL_CATEGORIES):
            label = tool_category_label(category)
            checkbox = QCheckBox(f"{label} ({category})" if show_ids else label)
            checkbox.setObjectName(f"{object_prefix}_{category}")
            checkbox.toggled.connect(self._mark_explicit)
            grid.addWidget(checkbox, index // column_count, index % column_count)
            self.checks[category] = checkbox
        layout.addLayout(grid)

        self.reset_button = QPushButton(QCoreApplication.translate('ToolCategorySelector', '重置为继承'))
        self.reset_button.setObjectName("settings_action_btn")
        self.reset_button.clicked.connect(self.reset_to_inherit)
        self.reset_button.setVisible(self._allow_inherit)
        layout.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignLeft)

    @property
    def is_inheriting(self) -> bool:
        return self._allow_inherit and self._inheriting

    def set_policy(
        self,
        *,
        ceiling: set[str] | tuple[str, ...] | list[str],
        policy: ToolSelectionPolicy | None,
        inheritance_path: str = "",
    ) -> None:
        self._ceiling = {str(item) for item in ceiling if str(item) in TOOL_CATEGORIES}
        inherited = policy is None or policy.allowed_categories is None
        selected = self._ceiling if inherited else self._ceiling & set(policy.allowed_categories or ())
        self._loading = True
        try:
            self._inheriting = bool(self._allow_inherit and inherited)
            for category, checkbox in self.checks.items():
                allowed = category in self._ceiling
                checkbox.setEnabled(allowed)
                checkbox.setChecked(allowed and category in selected)
                checkbox.setToolTip(QCoreApplication.translate('ToolCategorySelector', '可在当前层取消该类别。') if allowed else QCoreApplication.translate('ToolCategorySelector', '上层未允许，当前层不能开启。'))
            self.inheritance_label.setText(str(inheritance_path or ""))
            self.inheritance_label.setVisible(bool(inheritance_path))
            self._sync_reset_button()
        finally:
            self._loading = False

    def set_categories(self, categories: set[str] | tuple[str, ...] | list[str]) -> None:
        self.set_policy(
            ceiling=set(TOOL_CATEGORIES),
            policy=ToolSelectionPolicy.from_categories(categories),
        )

    def reset_to_inherit(self) -> None:
        if self._allow_inherit:
            self.set_policy(
                ceiling=self._ceiling,
                policy=None,
                inheritance_path=self.inheritance_label.text(),
            )

    def selection_policy(self) -> ToolSelectionPolicy | None:
        if self.is_inheriting:
            return None
        return ToolSelectionPolicy.from_categories(
            category for category, checkbox in self.checks.items() if checkbox.isChecked()
        )

    def selected_categories(self) -> set[str]:
        return {category for category, checkbox in self.checks.items() if checkbox.isChecked()}

    def _mark_explicit(self, _checked: bool) -> None:
        if self._loading or not self._allow_inherit:
            return
        self._inheriting = False
        self._sync_reset_button()

    def _sync_reset_button(self) -> None:
        self.reset_button.setEnabled(self._allow_inherit and not self._inheriting)


__all__ = ["ToolCategorySelector"]
