from __future__ import annotations

from typing import Any


__all__ = [
	"AppBootstrap",
	"AppBootstrapState",
	"AppContainer",
	"AppCoordinator",
	"AppServices",
	"AppSettingsUpdate",
	"AppState",
	"ConversationSelection",
	"ConversationSettingsUpdate",
	"EMPTY_APP_STATE",
	"Store",
]


def __getattr__(name: str) -> Any:
	if name in {"AppBootstrap", "AppBootstrapState"}:
		from pycat.core.app.bootstrap import AppBootstrap, AppBootstrapState

		return {"AppBootstrap": AppBootstrap, "AppBootstrapState": AppBootstrapState}[name]
	if name in {"AppContainer", "AppServices"}:
		from pycat.core.app.container import AppContainer, AppServices

		return {"AppContainer": AppContainer, "AppServices": AppServices}[name]
	if name == "AppCoordinator":
		from pycat.core.app.coordinator import AppCoordinator

		return AppCoordinator
	if name in {"AppSettingsUpdate", "AppState", "ConversationSelection", "ConversationSettingsUpdate", "EMPTY_APP_STATE"}:
		from pycat.core.app.state import (
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
		from pycat.core.app.store import Store

		return Store
	raise AttributeError(name)
