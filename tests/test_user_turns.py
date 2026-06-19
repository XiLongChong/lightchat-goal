"""Tests for runtime.user_turns — UserTurnRegistry."""

import asyncio

import pytest

from runtime.user_turns import UserTurnRegistry


class TestUserTurnRegistry:
    """Unit tests for the per-user lock registry and interrupt detection."""

    def test_same_user_returns_same_lock(self):
        registry = UserTurnRegistry(max_locks=512)
        lock1 = registry.get_lock("alice")
        lock2 = registry.get_lock("alice")
        assert lock1 is lock2
        assert registry.lock_count == 1

    def test_different_users_return_different_locks(self):
        registry = UserTurnRegistry(max_locks=512)
        lock_a = registry.get_lock("alice")
        lock_b = registry.get_lock("bob")
        assert lock_a is not lock_b
        assert registry.lock_count == 2

    def test_lock_count_tracks_unique_users(self):
        registry = UserTurnRegistry(max_locks=512)
        for i in range(10):
            registry.get_lock(f"user_{i}")
        assert registry.lock_count == 10

    def test_active_locks_count(self):
        registry = UserTurnRegistry(max_locks=512)

        async def hold_lock():
            lock = registry.get_lock("alice")
            async with lock:
                assert registry.active_locks == 1
            assert registry.active_locks == 0

        asyncio.run(hold_lock())

    def test_eviction_when_exceeds_max_locks(self):
        registry = UserTurnRegistry(max_locks=4)

        # Create 6 locks — should trigger eviction down to max_locks//2 = 2
        for i in range(6):
            registry.get_lock(f"user_{i}")

        # After eviction, should have at most max_locks
        assert registry.lock_count <= 4
        # The oldest users (not locked) should be gone
        assert registry.lock_count >= 2  # evicts down to target=2

    def test_eviction_skips_locked_users(self):
        registry = UserTurnRegistry(max_locks=3)

        async def hold_and_fill():
            # Hold a lock on alice, then fill up with other users
            alice_lock = registry.get_lock("alice")
            async with alice_lock:
                for i in range(10):
                    registry.get_lock(f"user_{i}")
                # alice should still be present because her lock is held
                lock_alice_after = registry.get_lock("alice")
                assert lock_alice_after is alice_lock

        asyncio.run(hold_and_fill())

    def test_interrupt_checker_no_new_message(self):
        registry = UserTurnRegistry()
        checker = registry.make_interrupt_checker("alice", input_latest_seq=5)
        # No arrival recorded → latest_seq=0 → 0 > 5 is False
        assert checker("") is False

    def test_interrupt_checker_newer_message_arrived(self):
        registry = UserTurnRegistry()
        # Record a message with seq=10
        registry.record_arrival("alice", seq=10)
        checker = registry.make_interrupt_checker("alice", input_latest_seq=5)
        # latest_seq=10 > input_latest_seq=5 → True
        assert checker("") is True

    def test_interrupt_checker_old_message_no_interrupt(self):
        registry = UserTurnRegistry()
        registry.record_arrival("alice", seq=5)
        checker = registry.make_interrupt_checker("alice", input_latest_seq=10)
        # latest_seq=5 > 10 is False
        assert checker("") is False

    def test_interrupt_checker_same_seq_no_interrupt(self):
        registry = UserTurnRegistry()
        registry.record_arrival("alice", seq=5)
        checker = registry.make_interrupt_checker("alice", input_latest_seq=5)
        # 5 > 5 is False
        assert checker("") is False

    def test_interrupt_checker_different_users_independent(self):
        registry = UserTurnRegistry()
        registry.record_arrival("alice", seq=10)
        checker_bob = registry.make_interrupt_checker("bob", input_latest_seq=5)
        # bob has no arrivals → latest_seq=0 → False
        assert checker_bob("") is False
        checker_alice = registry.make_interrupt_checker("alice", input_latest_seq=5)
        assert checker_alice("") is True

    def test_record_arrival_ignores_zero_seq(self):
        registry = UserTurnRegistry()
        registry.record_arrival("alice", seq=0)
        checker = registry.make_interrupt_checker("alice", input_latest_seq=0)
        # seq=0 is ignored → latest_seq stays 0
        assert checker("") is False

    def test_record_arrival_only_increases(self):
        registry = UserTurnRegistry()
        registry.record_arrival("alice", seq=10)
        registry.record_arrival("alice", seq=5)  # should be ignored (not greater)
        checker = registry.make_interrupt_checker("alice", input_latest_seq=7)
        # latest_seq is still 10
        assert checker("") is True

    def test_get_lock_is_async_lock(self):
        registry = UserTurnRegistry()
        lock = registry.get_lock("test_user")
        assert isinstance(lock, asyncio.Lock)
