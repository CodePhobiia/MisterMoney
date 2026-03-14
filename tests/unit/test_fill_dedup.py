"""Tests for LRU fill dedup — eviction, duplicate detection (T0-12)."""

import os
import sys

# The _LRUDedup class is defined inside pmm1/main.py.
# Import it by adding path and importing module internals.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from collections import OrderedDict


class _LRUDedup:
    """Copy of the class from pmm1/main.py for isolated testing."""

    def __init__(self, maxsize: int = 2000):
        self._seen: OrderedDict[str, bool] = OrderedDict()
        self._maxsize = maxsize

    def check_and_add(self, key: str) -> bool:
        """Returns True if duplicate (already seen)."""
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = True
        while len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


class TestLRUDedup:
    def test_new_key_not_duplicate(self):
        dedup = _LRUDedup(maxsize=100)
        assert dedup.check_and_add("key1") is False

    def test_same_key_is_duplicate(self):
        dedup = _LRUDedup(maxsize=100)
        dedup.check_and_add("key1")
        assert dedup.check_and_add("key1") is True

    def test_different_keys_not_duplicate(self):
        dedup = _LRUDedup(maxsize=100)
        dedup.check_and_add("key1")
        assert dedup.check_and_add("key2") is False

    def test_eviction_oldest(self):
        dedup = _LRUDedup(maxsize=3)
        dedup.check_and_add("a")
        dedup.check_and_add("b")
        dedup.check_and_add("c")
        dedup.check_and_add("d")  # Evicts "a"
        # "a" should no longer be seen (was evicted)
        assert dedup.check_and_add("a") is False  # Not duplicate anymore
        # "a" is now re-added, which evicts "b"
        # "d" and "c" should still be seen
        assert dedup.check_and_add("d") is True
        assert dedup.check_and_add("c") is True

    def test_lru_reordering(self):
        """Accessing a key moves it to the end, preventing eviction."""
        dedup = _LRUDedup(maxsize=3)
        dedup.check_and_add("a")
        dedup.check_and_add("b")
        dedup.check_and_add("c")
        # Access "a" to move it to end (order is now: b, c, a)
        dedup.check_and_add("a")  # Duplicate, but moves to end
        # Now insert "d" — "b" should be evicted (it's now oldest)
        dedup.check_and_add("d")  # order: c, a, d
        assert dedup.check_and_add("b") is False  # Evicted
        assert dedup.check_and_add("a") is True  # Still there

    def test_large_insert(self):
        """Insert 2001 entries with maxsize=2000. Oldest should be evicted."""
        dedup = _LRUDedup(maxsize=2000)
        for i in range(2001):
            dedup.check_and_add(f"key-{i}")
        # key-0 should have been evicted (only last 2000 remain: key-1 through key-2000)
        assert dedup.check_and_add("key-0") is False
        # key-2000 should still be there
        assert dedup.check_and_add("key-2000") is True

    def test_maxsize_respected(self):
        dedup = _LRUDedup(maxsize=5)
        for i in range(10):
            dedup.check_and_add(f"k{i}")
        assert len(dedup._seen) == 5
