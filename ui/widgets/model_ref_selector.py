from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QCompleter

from models.provider import Provider, build_model_ref
from ui.utils.combo_box import configure_combo_popup


@dataclass(frozen=True)
class ModelRefOption:
    label: str
    value: str
    provider_name: str
    model_name: str
    is_default: bool = False


def build_model_ref_options(providers: Iterable[Provider]) -> list[ModelRefOption]:
    """Return de-duplicated provider|model options for global model pickers."""

    options: list[ModelRefOption] = []
    seen: set[str] = set()
    for provider in providers or []:
        provider_name = str(getattr(provider, "name", "") or "").strip()
        if not provider_name:
            continue

        default_model = str(getattr(provider, "default_model", "") or "").strip()
        model_names: list[str] = []
        if default_model:
            model_names.append(default_model)
        try:
            profiles = provider.get_model_profiles()
        except Exception:
            profiles = getattr(provider, "model_profiles", []) or []
        for profile in profiles or []:
            model_name = str(getattr(profile, "model_id", "") or "").strip()
            if model_name:
                model_names.append(model_name)
        for model in getattr(provider, "models", []) or []:
            model_name = str(model or "").strip()
            if model_name:
                model_names.append(model_name)

        local_seen: set[str] = set()
        for model_name in model_names:
            if model_name in local_seen:
                continue
            local_seen.add(model_name)

            value = build_model_ref(provider_name, model_name)
            if not value or value in seen:
                continue
            seen.add(value)

            is_default = bool(default_model and model_name == default_model)
            options.append(
                ModelRefOption(
                    label=value,
                    value=value,
                    provider_name=provider_name,
                    model_name=model_name,
                    is_default=is_default,
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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._allow_empty = bool(allow_empty)
        self._empty_label = str(empty_label or "跟随当前对话模型")
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(20)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(18)
        configure_combo_popup(self, popup_minimum_width=320)
        self.lineEdit().setPlaceholderText("搜索模型 / provider|model")
        self.setToolTip("从所有服务商模型中搜索选择；也可手动输入 provider|model。")
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
                if option.is_default:
                    tip_lines.insert(2, "服务商默认模型")
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
            return

        idx = self.findData(value)
        if idx >= 0:
            self.setCurrentIndex(idx)
            return

        for i in range(self.count()):
            if self.itemText(i).strip() == value:
                self.setCurrentIndex(i)
                return

        self.addItem(value, value)
        self.setCurrentIndex(self.count() - 1)

    def model_ref(self) -> str:
        text = (self.currentText() or "").strip()
        idx = self.currentIndex()
        if idx >= 0 and self.itemText(idx).strip() == text:
            return str(self.itemData(idx) or "").strip()

        for i in range(self.count()):
            item_text = self.itemText(i).strip()
            item_value = str(self.itemData(i) or "").strip()
            if text == item_text or (item_value and text == item_value):
                return item_value
        return text

    def _configure_completer(self) -> None:
        completer = self.completer()
        if completer is None:
            completer = QCompleter(self.model(), self)
            self.setCompleter(completer)
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