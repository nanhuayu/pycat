"""Application lifetime and explicit public service access."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Self

from pycat.core.app.services.run import RunHandle
from pycat.models.contracts.agent import ApplicationError, RunRequest, RunResult

if TYPE_CHECKING:
    from pycat.core.app.container import AppContainer, AppServices


class PyCat:
    def __init__(
        self, *, data_dir: str | Path | None = None, background_curation: bool = False, transport_factory=None
    ):
        self._options = dict(
            data_dir=data_dir, background_curation=background_curation, transport_factory=transport_factory
        )
        self._container: AppContainer | None = None
        self._closed = False

    async def __aenter__(self) -> Self:
        if self._closed:
            raise ApplicationError("A closed application cannot be reopened.")
        if self._container is None:
            from pycat.core.app.container import AppContainer

            self._container = AppContainer(**self._options)
            self._container.services.run_service.bind_loop()
        return self

    @property
    def _services(self) -> AppServices:
        if self._container is None or self._closed:
            raise ApplicationError("Open the application with 'async with PyCat(...)'.")
        return self._container.services

    @property
    def conversations(self):
        return self._services.conv_service

    @property
    def workspaces(self):
        return self._services.workspace_service

    @property
    def data_dir(self) -> Path:
        return self._services.data_dir

    @property
    def models(self):
        return self._services.provider_catalog_service

    @property
    def settings(self):
        return self._services.settings_update_service

    @property
    def modes(self):
        return self._services.mode_catalog_service

    @property
    def content(self):
        return self._services.knowledge_service.resolver

    @property
    def knowledge(self):
        return self._services.knowledge_service

    @property
    def skills(self):
        return self._services.skill_service

    @property
    def channels(self):
        return self._services.channel_service

    @property
    def commands(self):
        return self._services.command_service

    @property
    def tools(self):
        return self._services.tools

    @property
    def mcp(self):
        return self._services.mcp

    async def run(self, request: RunRequest, *, approval_callback=None, questions_callback=None) -> RunResult:
        return await self._services.run_service.run(
            request, approval_callback=approval_callback, questions_callback=questions_callback
        )

    def start(self, request: RunRequest, *, approval_callback=None, questions_callback=None) -> RunHandle:
        return self._services.run_service.start(
            request, approval_callback=approval_callback, questions_callback=questions_callback
        )

    async def compact(self, conversation_id: str, *, expected_revision: str | None = None):
        return await self._services.run_service.compact(conversation_id, expected_revision=expected_revision)

    async def aclose(self) -> None:
        self._closed = True
        if self._container is not None:
            await self._container.aclose()

    async def __aexit__(self, *_):
        await self.aclose()


def run_sync(request: RunRequest, *, data_dir: str | Path | None = None, transport_factory=None) -> RunResult:
    """Open, run and close once. Persistent/streaming callers use the async API."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ApplicationError("run_sync cannot run inside an event loop; await app.run(request).")

    async def once():
        async with PyCat(data_dir=data_dir, transport_factory=transport_factory) as app:
            return await app.run(request)

    return asyncio.run(once())
