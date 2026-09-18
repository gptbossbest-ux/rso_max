"""Synchronized operations for process-local in-memory session state.

Only operations performed through :class:`SessionManager` use its lock.  The
mutable mappings and session dictionaries returned to callers are not guarded
against direct external mutation, so this module does not make a complete bot
conversation atomic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, MutableMapping
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from rso_bot.states import FLOW_KEYS, METER_INPUT_KEYS, S

Session = dict[str, Any]
SessionMap = MutableMapping[int, Any]
Clock = Callable[[], datetime]


class SessionManager:
    """Synchronize individual session operations without owning business logic.

    The registry and mutations made by these methods are protected by this
    instance's lock.  Callers still receive mutable dictionaries; direct access
    to them, or access through another manager, is outside that protection.
    """

    def __init__(
        self,
        states: SessionMap,
        *,
        clock: Clock,
        logger: logging.Logger | None = None,
    ) -> None:
        self.states = states
        self._clock = clock
        self._logger = logger
        self._lock = RLock()

    def now(self) -> datetime:
        return self._clock()

    def touch(self, state: Session) -> Session:
        with self._lock:
            state["last_active"] = self.now()
            return state

    def get_state(self, chat_id: int) -> Session:
        with self._lock:
            if chat_id not in self.states:
                self.states[chat_id] = self.touch({"state": S.MENU})
            return self.states[chat_id]

    def cleanup(
        self,
        states: SessionMap | None = None,
        ttl_minutes: int = 30,
    ) -> int:
        target = self.states if states is None else states
        with self._lock:
            cutoff = self.now() - timedelta(minutes=ttl_minutes)
            to_remove = [
                chat_id
                for chat_id, state in target.items()
                if isinstance(state, dict)
                and state.get("last_active", self.now()) < cutoff
            ]
            for chat_id in to_remove:
                del target[chat_id]

        if to_remove and self._logger is not None:
            self._logger.info(
                "cleanup_user_states: удалено %d устаревших сессий",
                len(to_remove),
            )
        return len(to_remove)

    def clear_flow(self, state: Session) -> None:
        with self._lock:
            for key in FLOW_KEYS:
                state.pop(key, None)
            state["state"] = S.MENU

    def reset_meter_input(self, state: Session) -> None:
        with self._lock:
            for key in METER_INPUT_KEYS:
                state.pop(key, None)
            state["state"] = S.WAITING_VALUE1
