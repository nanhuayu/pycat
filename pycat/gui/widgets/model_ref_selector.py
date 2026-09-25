from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QComboBox, QCompleter, QListView

from pycat.core.llm.model_selection import provider_model_ids
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.models.contracts.model_target import ModelTarget
from pycat.models.model_ref import build_model_ref
from pycat.models.provider import Provider


@dataclass(frozen=True)
class ModelRefOption:
    label: str
    value: str
    provider_name: str
    model_name: str


def build_model_ref_options(providers: Iterable[Provider], *, model_type: str = "chat", require_image_input: bool = False) -> list[ModelRefOption]:
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
            profile = provider.effective_model_profile(model_name)
            if profile.model_type != model_type or (require_image_input and not profile.supports_input("image")):
                continue
            if model_type == "image" and not provider.supports_image_api:
                continue
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
        empty_label: str | None = None,
        allow_unlisted_current: bool = False,
        model_type: str = "chat",
        require_image_input: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._allow_empty = bool(allow_empty)
        self._empty_label = str(empty_label or self.tr("跟随当前对话模型"))
        self._allow_unlisted_current = bool(allow_unlisted_current)
        self._last_valid_ref = ""
        self._model_type = model_type
        self._require_image_input = require_image_input
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(20)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(18)
        configure_combo_popup(self, popup_minimum_width=320)
        self.lineEdit().setPlaceholderText(self.tr("搜索模型 / provider|model"))
        self.setToolTip(self.tr("从已添加的常用模型中搜索选择。"))
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
                self.setItemData(0, self.tr("留空表示跟随当前对话正在使用的模型。"), Qt.ItemDataRole.ToolTipRole)

            for option in build_model_ref_options(providers, model_type=self._model_type, require_image_input=self._require_image_input):
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
            # This method is inherited; QObject.tr() would use the subclass's
            # context, while pylupdate extracts the message under ModelRefCombo.
            self.setItemData(index, QCoreApplication.translate("ModelRefCombo", "当前绑定模型不在可用目录中；请检查服务、模型用途和输入能力。"), Qt.ItemDataRole.ToolTipRole)
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
        model_type: str = "chat",
        allow_inherit: bool = True,
        parent=None,
    ) -> None:
        QComboBox.__init__(self, parent)
        self._allow_empty = True
        self._empty_label = self.tr("使用辅助模型默认值")
        self._allow_unlisted_current = False
        self._last_valid_ref = ""
        self._model_type = model_type
        self._require_image_input = False
        self._allow_inherit = allow_inherit
        self._allow_unlisted_current = model_type == "image"
        self._providers = list(providers or [])
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(20)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(18)
        configure_combo_popup(self, popup_minimum_width=320)
        self.lineEdit().setPlaceholderText(self.tr("搜索模型 / provider|model"))
        self.setToolTip(self.tr("可跟随辅助默认、跟随会话主模型，或指定 provider|model。"))
        self.currentIndexChanged.connect(self._remember_current_index)
        self.set_providers(providers or [], current_model_ref="")
        self.set_model_target(current_target or ModelTarget())

    def set_providers(
        self,
        providers: Iterable[Provider],
        *,
        current_model_ref: str | None = None,
    ) -> None:
        self._providers = list(providers)
        current = self.model_target() if self.count() else ModelTarget()
        if current_model_ref:
            current = ModelTarget.explicit(current_model_ref)

        self.blockSignals(True)
        try:
            self.clear()
            if self._allow_inherit:
                self.addItem(self.tr("使用辅助模型默认值"), self._AUXILIARY_VALUE)
                self.addItem(self.tr("跟随会话主模型"), self._PRIMARY_VALUE)
            else:
                self.addItem(self.tr("请选择模型"), "")
            for option in build_model_ref_options(self._providers, model_type=self._model_type):
                self.addItem(option.label, option.value)
                idx = self.count() - 1
                self.setItemData(idx, f"{option.provider_name}\n{option.model_name}", Qt.ItemDataRole.ToolTipRole)
        finally:
            self.blockSignals(False)
        self._configure_completer()
        self.set_model_target(current)
        self._refresh_popup_width()

    def set_model_type(self, model_type: str) -> None:
        if self._model_type == model_type:
            return
        self._model_type = model_type
        self._allow_inherit = model_type == "chat"
        self._allow_unlisted_current = model_type == "image"
        self.set_providers(self._providers)

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
        # Resolve typed/completed text through the same catalog as the base combo.
        # currentData() alone still points at the previous item while editing.
        value = self.model_ref()
        if value in {self._PRIMARY_VALUE, self._AUXILIARY_VALUE}:
            return ModelTarget(source="primary" if value == self._PRIMARY_VALUE else "auxiliary")
        return ModelTarget.explicit(value)
