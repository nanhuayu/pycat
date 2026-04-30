from __future__ import annotations

from typing import Any


__all__ = [
	"AppBootstrap",
	"AppBootstrapState",
	"AppCoordinator",
	"AppSettingsUpdate",
	"AppState",
	"ConversationSelection",
	"ConversationSettingsUpdate",
	"EMPTY_APP_STATE",
	"Store",
]


def __getattr__(name: str) -> Any:
	if name in {"AppBootstrap", "AppBootstrapState"}:
		from core.app.bootstrap import AppBootstrap, AppBootstrapState

		return {"AppBootstrap": AppBootstrap, "AppBootstrapState": AppBootstrapState}[name]
	if name == "AppCoordinator":
		from core.app.coordinator import AppCoordinator

		return AppCoordinator
	if name in {"AppSettingsUpdate", "AppState", "ConversationSelection", "ConversationSettingsUpdate", "EMPTY_APP_STATE"}:
		from core.app.state import (
			AppSettingsUpdate,
			AppState,
			ConversationSelection,
			ConversationSettingsUpdate,
			EMPTY_APP_STATE,
		)

		return {
			"AppSettingsUpdate": AppSettingsUpdate,
			"AppState": AppState,
			"ConversationSelection": ConversationSelection,
			"ConversationSettingsUpdate": ConversationSettingsUpdate,
			"EMPTY_APP_STATE": EMPTY_APP_STATE,
		}[name]
	if name == "Store":
		from core.app.store import Store

		return Store
	raise AttributeError(name)
