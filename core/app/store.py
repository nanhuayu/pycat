from __future__ import annotations

from dataclasses import replace
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
Listener = Callable[[], None]
OnChange = Callable[[T, T], None]


class Store(Generic[T]):
    def __init__(self, initial_state: T, on_change: OnChange[T] | None = None) -> None:
        self._state = initial_state
        self._on_change = on_change
        self._listeners: set[Listener] = set()

    def get_state(self) -> T:
        return self._state

    def set_state(self, updater: Callable[[T], T]) -> T:
        previous = self._state
        next_state = updater(previous)
        if next_state == previous:
            return self._state
        self._state = next_state
        if self._on_change is not None:
            self._on_change(next_state, previous)
        for listener in list(self._listeners):
            listener()
        return self._state

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe


def patch_dataclass_state(state: T, **updates) -> T:
    return replace(state, **updates)


def create_store(initial_state: T, on_change: OnChange[T] | None = None) -> Store[T]:
    return Store(initial_state=initial_state, on_change=on_change)
