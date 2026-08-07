from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QCompleter, QListView

from core.llm.model_selection import provider_model_ids
from gui.utils.combo_box import configure_combo_popup
from models.contracts.model_target import ModelTarget
from models.model_ref import build_model_ref
from models.provider import Provider


@dataclass(frozen=True)
class ModelRefOption:
    label: str
    value: str
    provider_name: str
    model_name: str


def build_model_ref_options(providers: Iterable[Provider]) -> list[ModelRefOption]:
    """Return de-duplicated provider|model options for global model pickers."""

    options: list[ModelRefOption] = []
    seen: set[str] = set()
    for provider in providers or []:
        if not bool(getattr(provider, "enabled", True)):
            continue
        provider_name = str(getattr(provider, "name", "") or "").strip()
        if not provider_name:
            continue

        for model_name in provider_model_ids(provider):
            value = build_model_ref(provider_name, model_name)
            if not value or value in seen:
                continue
            seen.add(value)

            options.append(
                ModelRefOption(
                    label=value,
                    value=value,
                    provider_name=provider_name,
                    model_name=model_name,
                )
            )
    return options


class ModelRefCombo(QComboBox):
    """Searchable combo for selecting a normalized ``provider|model`` reference."""

    def __init__(
        self,
        providers: Iterable[Provider] | None = None,
        *,
        current_model_ref: str = "",
        allow_empty: bool = True,
        empty_label: str = "跟随当前对话模型",
        allow_unlisted_current: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._allow_empty = bool(allow_empty)
        self._empty_label = str(empty_label or "跟随当前对话模型")
        self._allow_unlisted_current = bool(allow_unlisted_current)
        self._last_valid_ref = ""
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(20)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(18)
        configure_combo_popup(self, popup_minimum_width=320)
        self.lineEdit().setPlaceholderText("搜索模型 / provider|model")
        self.setToolTip("从已添加的常用模型中搜索选择。")
        self.currentIndexChanged.connect(self._remember_current_index)
        self.set_providers(providers or [], current_model_ref=current_model_ref)

    def set_providers(
        self,
        providers: Iterable[Provider],
        *,
        current_model_ref: str | None = None,
    ) -> None:
        current = self.model_ref() if current_model_ref is None else str(current_model_ref or "").strip()

        self.blockSignals(True)
        try:
            self.clear()
            if self._allow_empty:
                self.addItem(self._empty_label, "")
                self.setItemData(0, "留空表示跟随当前对话正在使用的模型。", Qt.ItemDataRole.ToolTipRole)

            for option in build_model_ref_options(providers):
                self.addItem(option.label, option.value)
                idx = self.count() - 1
                tip_lines = [option.provider_name, option.model_name, option.value]
                self.setItemData(
                    idx,
                    "\n".join(tip_lines),
                    Qt.ItemDataRole.ToolTipRole,
                )
        finally:
            self.blockSignals(False)

        self._configure_completer()
        self.set_model_ref(current)
        self._refresh_popup_width()

    def set_model_ref(self, model_ref: str) -> None:
        value = str(model_ref or "").strip()
        if not value:
            self.setCurrentIndex(0 if self.count() else -1)
            self._last_valid_ref = str(self.itemData(self.currentIndex()) or "").strip() if self.currentIndex() >= 0 else ""
            return

        idx = self.findData(value)
        if idx >= 0:
            self.setCurrentIndex(idx)
            self._last_valid_ref = value
            return

        for i in range(self.count()):
            if self.itemText(i).strip() == value:
                self.setCurrentIndex(i)
                self._last_valid_ref = str(self.itemData(i) or "").strip()
                return

        if self._allow_unlisted_current:
            self.addItem(value, value)
            index = self.count() - 1
            self.setItemData(index, "当前会话模型（未加入常用模型目录）", Qt.ItemDataRole.ToolTipRole)
            self.setCurrentIndex(index)
            self._last_valid_ref = value
            return
        self.setCurrentIndex(0 if self.count() else -1)
        self._last_valid_ref = str(self.itemData(self.currentIndex()) or "").strip() if self.currentIndex() >= 0 else ""

    def model_ref(self) -> str:
        text = (self.currentText() or "").strip()
        idx = self.currentIndex()
        if idx >= 0 and self.itemText(idx).strip() == text:
            value = str(self.itemData(idx) or "").strip()
            self._last_valid_ref = value
            return value

        for i in range(self.count()):
            item_text = self.itemText(i).strip()
            item_value = str(self.itemData(i) or "").strip()
            if text == item_text or (item_value and text == item_value):
                self._last_valid_ref = item_value
                return item_value
        fallback_index = self.findData(self._last_valid_ref) if self._last_valid_ref else -1
        return str(self.itemData(fallback_index) or "").strip() if fallback_index >= 0 else ""

    def _remember_current_index(self, index: int) -> None:
        if index < 0:
            return
        self._last_valid_ref = str(self.itemData(index) or "").strip()

    def _configure_completer(self) -> None:
        completer = self.completer()
        if completer is None:
            completer = QCompleter(self.model(), self)
            self.setCompleter(completer)
        popup = completer.popup()
        if popup is None or popup.objectName() != "combo_popup_view":
            popup = QListView(self)
            popup.setObjectName("combo_popup_view")
            completer.setPopup(popup)
        try:
            popup.setTextElideMode(Qt.TextElideMode.ElideMiddle)
            popup.setUniformItemSizes(True)
        except Exception:
            pass
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def _refresh_popup_width(self) -> None:
        widest = 0
        metrics = self.fontMetrics()
        for index in range(self.count()):
            widest = max(widest, metrics.horizontalAdvance(self.itemText(index) or ""))
        popup_width = min(max(280, widest + 64), 560)
        self.view().setMinimumWidth(popup_width)
        completer = self.completer()
        if completer is not None and completer.popup() is not None:
            completer.popup().setMinimumWidth(popup_width)


class ModelTargetCombo(ModelRefCombo):
    """Model picker with explicit primary and auxiliary inheritance choices."""

    _PRIMARY_VALUE = "__model_target_primary__"
    _AUXILIARY_VALUE = "__model_target_auxiliary__"

    def __init__(
        self,
        providers: Iterable[Provider] | None = None,
        *,
        current_target: ModelTarget | None = None,
        parent=None,
    ) -> None:
        QComboBox.__init__(self, parent)
        self._allow_empty = True
        self._empty_label = "使用辅助模型默认值"
        self._allow_unlisted_current = False
        self._last_valid_ref = ""
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(20)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(18)
        configure_combo_popup(self, popup_minimum_width=320)
        self.lineEdit().setPlaceholderText("搜索模型 / provider|model")
        self.setToolTip("可跟随辅助默认、跟随会话主模型，或指定 provider|model。")
        self.currentIndexChanged.connect(self._remember_current_index)
        self.set_providers(providers or [], current_model_ref="")
        self.set_model_target(current_target or ModelTarget())

    def set_providers(
        self,
        providers: Iterable[Provider],
        *,
        current_model_ref: str | None = None,
    ) -> None:
        current = self.model_target() if self.count() else ModelTarget()
        if current_model_ref:
            current = ModelTarget.explicit(current_model_ref)

        self.blockSignals(True)
        try:
            self.clear()
            self.addItem("使用辅助模型默认值", self._AUXILIARY_VALUE)
            self.addItem("跟随会话主模型", self._PRIMARY_VALUE)
            for option in build_model_ref_options(providers):
                self.addItem(option.label, option.value)
                idx = self.count() - 1
                self.setItemData(idx, f"{option.provider_name}\n{option.model_name}", Qt.ItemDataRole.ToolTipRole)
        finally:
            self.blockSignals(False)
        self._configure_completer()
        self.set_model_target(current)
        self._refresh_popup_width()

    def set_model_target(self, target: ModelTarget) -> None:
        value = target if isinstance(target, ModelTarget) else ModelTarget()
        if value.source == "primary":
            self.setCurrentIndex(max(0, self.findData(self._PRIMARY_VALUE)))
            return
        if value.source == "explicit" and value.model_ref:
            self.set_model_ref(value.model_ref)
            return
        self.setCurrentIndex(max(0, self.findData(self._AUXILIARY_VALUE)))

    def model_target(self) -> ModelTarget:
        idx = self.currentIndex()
        data = str(self.itemData(idx) or "") if idx >= 0 else ""
        text = str(self.currentText() or "").strip()
        if data == self._PRIMARY_VALUE:
            return ModelTarget(source="primary")
        if data == self._AUXILIARY_VALUE or text == "使用辅助模型默认值":
            return ModelTarget(source="auxiliary")
        value = self.model_ref()
        if value in {self._PRIMARY_VALUE, self._AUXILIARY_VALUE}:
            return ModelTarget(source="primary" if value == self._PRIMARY_VALUE else "auxiliary")
        return ModelTarget.explicit(value)
