"""Thread-safe mutable controls for one active agent run."""
from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from pycat.models.contracts.agent import RunPolicy
from pycat.models.contracts.tooling import FilesystemScope, ToolPermissionConfig


_COMPLETION_TOOL = "agent__complete"


def effective_run_policy(
    policy: RunPolicy,
    control: "RunControl | None" = None,
) -> tuple[RunPolicy, int]:
    """Return one immutable permission view without changing the run ceiling."""
    permissions = policy.tool_permissions
    filesystem_scope = policy.filesystem_scope
    revision = 0
    if control is not None:
        permissions, filesystem_scope, revision = control.access_snapshot()
    if policy.completion_policy == "explicit":
        permissions = ToolPermissionConfig(
            category_defaults=dict(permissions.category_defaults or {}),
            tools={
                **dict(permissions.tools or {}),
                _COMPLETION_TOOL: {"action": "allow"},
            },
        )
        return replace(
            policy,
            tool_permissions=permissions,
            filesystem_scope=filesystem_scope,
        ), revision
    return replace(
        policy,
        tool_permissions=permissions,
        filesystem_scope=filesystem_scope,
    ), revision


class RunControl:
    """Own pending user guidance and the latest permission snapshot for one run."""

    def __init__(
        self,
        tool_permissions: ToolPermissionConfig | None = None,
        *,
        filesystem_scope: FilesystemScope | None = None,
        max_items: int = 8,
        max_chars: int = 16_384,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self._lock = threading.Lock()
        self._pending: deque[str] = deque()
        self._pending_chars = 0
        self._tool_permissions = self._copy_permissions(tool_permissions or ToolPermissionConfig())
        initial_scope = filesystem_scope or FilesystemScope()
        self._filesystem_mode = initial_scope.mode
        self._allow_home_read = initial_scope.allow_home_read
        self._read_grants = list(initial_scope.granted_read_roots)
        self._access_revision = 0
        self._accepting = True
        self._max_items = int(max_items)
        self._max_chars = int(max_chars)
        self._skill_snapshots: dict[str, dict] = {}

    def skill_snapshot(self, name: str, value: dict | None = None) -> dict | None:
        """The first successful load pins one method for this run only."""
        with self._lock:
            if value is not None:
                self._skill_snapshots.setdefault(name, deepcopy(value))
            return deepcopy(self._skill_snapshots.get(name))

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def pending_chars(self) -> int:
        with self._lock:
            return self._pending_chars

    def submit(self, text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        with self._lock:
            if not self._accepting:
                return False
            if len(self._pending) >= self._max_items:
                return False
            if self._pending_chars + len(value) > self._max_chars:
                return False
            self._pending.append(value)
            self._pending_chars += len(value)
            return True

    def take_pending(self) -> tuple[str, ...]:
        with self._lock:
            return self._take_pending_locked()

    def take_or_close(self) -> tuple[str, ...]:
        """Take a pending batch, or atomically stop accepting when empty."""
        with self._lock:
            pending = self._take_pending_locked()
            if not pending:
                self._accepting = False
            return pending

    def access_snapshot(self) -> tuple[ToolPermissionConfig, FilesystemScope, int]:
        with self._lock:
            return (
                self._copy_permissions(self._tool_permissions),
                FilesystemScope(
                    mode=self._filesystem_mode,
                    allow_home_read=self._allow_home_read,
                    granted_read_roots=tuple(self._read_grants),
                ),
                self._access_revision,
            )

    def update_access(
        self,
        tool_permissions: ToolPermissionConfig,
        filesystem_scope: FilesystemScope,
    ) -> int | None:
        if not isinstance(tool_permissions, ToolPermissionConfig):
            raise TypeError("tool_permissions must be a ToolPermissionConfig")
        if not isinstance(filesystem_scope, FilesystemScope):
            raise TypeError("filesystem_scope must be a FilesystemScope")
        copied = self._copy_permissions(tool_permissions)
        with self._lock:
            if not self._accepting:
                return None
            self._tool_permissions = copied
            self._filesystem_mode = filesystem_scope.mode
            self._allow_home_read = filesystem_scope.allow_home_read
            for root in filesystem_scope.granted_read_roots:
                if root not in self._read_grants:
                    self._read_grants.append(root)
            self._access_revision += 1
            return self._access_revision

    def add_read_grant(self, root: str) -> int | None:
        value = str(root or "").strip()
        if not value:
            raise ValueError("read grant root is required")
        canonical = str(Path(value).expanduser().resolve(strict=False))
        with self._lock:
            if not self._accepting:
                return None
            if canonical in self._read_grants:
                return self._access_revision
            self._read_grants.append(canonical)
            self._access_revision += 1
            return self._access_revision

    def close_and_drain(self) -> tuple[str, ...]:
        with self._lock:
            self._accepting = False
            return self._take_pending_locked()

    def _take_pending_locked(self) -> tuple[str, ...]:
        pending = tuple(self._pending)
        self._pending.clear()
        self._pending_chars = 0
        return pending

    @staticmethod
    def _copy_permissions(tool_permissions: ToolPermissionConfig) -> ToolPermissionConfig:
        return ToolPermissionConfig.from_dict(tool_permissions.to_dict())


__all__ = ["RunControl", "effective_run_policy"]
