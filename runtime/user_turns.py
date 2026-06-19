"""Per-user turn lock registry and interrupt detection.

Extracted from main.py — batch B of main.py responsibility splitting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from agent.state_machine import InterruptChecker

logger = logging.getLogger("goal")


class UserTurnRegistry:
    """Per-user asyncio.Lock pool with LRU eviction and interrupt tracking.

    Ensures at most one turn per user runs at a time.  Locks are created
    on first access and evicted (LRU, idle-first) when the pool exceeds
    *max_locks*.
    """

    def __init__(self, max_locks: int = 512) -> None:
        self._max_locks = max_locks
        self._locks: dict[str, asyncio.Lock] = {}
        self._access_times: dict[str, float] = {}
        # user_id → latest inbound seq seen globally (for interrupt detection)
        self._latest_inbound_seq: dict[str, int] = {}

    # ------------------------------------------------------------------
    # interrupt tracking
    # ------------------------------------------------------------------

    def record_arrival(self, user_id: str, seq: int = 0) -> None:
        """Record the latest inbound sequence for a user."""
        if seq > 0:
            current = self._latest_inbound_seq.get(user_id, 0)
            if seq > current:
                self._latest_inbound_seq[user_id] = seq

    def make_interrupt_checker(
        self, user_id: str, input_latest_seq: int,
    ) -> InterruptChecker:
        """Return a checker that detects a newer inbound message.

        The returned callable matches :data:`agent.state_machine.InterruptChecker`
        (``Callable[[str], bool]``).
        """

        def _check(_interrupt_token: str) -> bool:
            latest_seq = self._latest_inbound_seq.get(user_id, 0)
            interrupted = latest_seq > input_latest_seq
            if interrupted:
                logger.info(
                    "interrupt_check: latest_seq=%d > input_latest_seq=%d "
                    "-> interrupted=True",
                    latest_seq,
                    input_latest_seq,
                )
            return interrupted

        return _check

    # ------------------------------------------------------------------
    # lock pool
    # ------------------------------------------------------------------

    def get_lock(self, user_id: str) -> asyncio.Lock:
        """Get or create a per-user async lock for turn ordering."""
        now = time.time()
        self._cleanup(now)
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        self._access_times[user_id] = now
        return self._locks[user_id]

    def _cleanup(self, now: float | None = None) -> None:
        """Evict oldest idle locks when the pool exceeds *max_locks*."""
        if len(self._locks) <= self._max_locks:
            return
        now = now or time.time()
        stale_ids = sorted(
            self._access_times,
            key=lambda uid: self._access_times.get(uid, 0),
        )
        target = self._max_locks // 2
        for uid in stale_ids:
            lock = self._locks.get(uid)
            if lock is not None and lock.locked():
                continue
            self._locks.pop(uid, None)
            self._access_times.pop(uid, None)
            if len(self._locks) <= target:
                break

    # ------------------------------------------------------------------
    # diagnostics (for tests and admin)
    # ------------------------------------------------------------------

    @property
    def lock_count(self) -> int:
        """Current number of tracked locks."""
        return len(self._locks)

    @property
    def active_locks(self) -> int:
        """Number of locks currently held (locked)."""
        return sum(1 for lk in self._locks.values() if lk.locked())
