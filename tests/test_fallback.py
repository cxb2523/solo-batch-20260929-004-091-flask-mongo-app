"""Configuration / connection failures and mid-session disconnect fallback."""

import mongomock
import pymongo
import pytest

from repositories import (RepositoryManager, MEMORY, MONGO)
from conftest import make_note


def test_missing_uri_starts_on_memory_backend(monkeypatch):
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    manager = RepositoryManager()
    assert manager.backend == MEMORY
    assert manager.degraded is False
    assert "not configured" in manager.fallback_reason


def test_placeholder_uri_starts_on_memory_backend():
    manager = RepositoryManager(mongo_uri="<MongoDB URI>")
    assert manager.backend == MEMORY


def test_connection_timeout_falls_back_to_memory():
    # Port 1 is virtually guaranteed to refuse connections; a short timeout
    # keeps the test fast.
    manager = RepositoryManager(
        mongo_uri="mongodb://127.0.0.1:1/test",
        server_selection_timeout_ms=500,
    )
    assert manager.backend == MEMORY
    assert manager.degraded is True
    assert manager.connect_error is not None
    # Reads and writes still work and never raise
    assert manager.notes.add(make_note("n1")) is True
    assert manager.notes.count_active() == 1


class _ExplodingCursor:
    def __init__(self, error):
        self._error = error

    def sort(self, *args, **kwargs):
        raise self._error


class _ExplodingCollection:
    def __init__(self, error):
        self._error = error

    def find(self, *args, **kwargs):
        raise self._error

    def find_one(self, *args, **kwargs):
        raise self._error

    def update_one(self, *args, **kwargs):
        raise self._error

    def insert_one(self, *args, **kwargs):
        raise self._error

    def delete_one(self, *args, **kwargs):
        raise self._error

    def delete_many(self, *args, **kwargs):
        raise self._error

    def count_documents(self, *args, **kwargs):
        raise self._error


class _ExplodingDB:
    def __init__(self, error):
        self._error = error

    def __getattr__(self, name):
        return _ExplodingCollection(self._error)


class _BrokenCursorCollection(_ExplodingCollection):
    def __init__(self, error, fail_after):
        super().__init__(error)
        self._fail_after = fail_after
        self._calls = 0

    def find(self, *args, **kwargs):
        self._calls += 1
        if self._calls > self._fail_after:
            raise self._error
        return _WorkingCursor()


class _WorkingCursor:
    def sort(self, *args, **kwargs):
        return iter([])


class _PartlyBrokenDB:
    def __init__(self):
        self.notes = _BrokenCursorCollection(
            pymongo.errors.AutoReconnect("connection lost"),
            fail_after=1)


def test_mid_session_disconnect_falls_back_and_never_raises():
    memory_db = mongomock.MongoClient().db
    manager = RepositoryManager(
        mongo_db=_ExplodingDB(
            pymongo.errors.AutoReconnect("connection lost")),
        memory_db=memory_db,
    )
    assert manager.backend == MONGO

    # First failing call transparently flips to the memory backend and
    # retries there - no exception escapes.
    assert manager.notes.find_active_by_user("u") == []
    assert manager.backend == MEMORY
    assert manager.degraded is True
    assert "lost" in manager.fallback_reason

    # Subsequent writes/reads continue on the memory backend.
    assert manager.notes.add(make_note("n1")) is True
    assert manager.notes.count_active() == 1
    assert manager.notes.soft_delete("n1") is True


def test_fallback_returns_degraded_values_without_raising(monkeypatch):
    manager = RepositoryManager(mongo_uri="")

    class BrokenMemoryRepo:
        def count_active(self):
            raise RuntimeError("boom")

    # Force even the memory backend to fail: callers still receive the
    # degraded value rather than an exception.
    monkeypatch.setattr(
        manager, "memory_db",
        _ExplodingDB(RuntimeError("boom")),
        raising=False)
    assert manager.notes.count_active() == 0
    assert manager.messages.find_inbox("u") == []
    assert manager.users.find_by_id("u") is None
