"""Dual-backend tests for the repository layer.

Every behavioural test runs against both the Mongo and the memory
(mongomock) implementations. Where no live MongoDB server is available, the
Mongo* classes are exercised with a mongomock client standing in for the
server, since both expose the identical pymongo collection API.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import mongomock
import pytest
from pymongo.errors import PyMongoError

import repositories


def _mongo_client():
    """Real MongoDB if reachable, otherwise a mongomock stand-in."""
    try:
        client = repositories.MongoClient(
            "mongodb://127.0.0.1:27017/", serverSelectionTimeoutMS=200)
        client.admin.command("ping")
        return client
    except PyMongoError:
        return mongomock.MongoClient()


def _make_repos(backend):
    db_name = "test_db"
    if backend == "mongo":
        client = _mongo_client()
        client.drop_database(db_name)
        return {
            "users": repositories.MongoUserRepository(client, db_name),
            "notes": repositories.MongoNoteRepository(client, db_name),
            "messages": repositories.MongoMessageRepository(client, db_name),
        }
    client = mongomock.MongoClient()
    return {
        "users": repositories.MemoryUserRepository(client, db_name),
        "notes": repositories.MemoryNoteRepository(client, db_name),
        "messages": repositories.MemoryMessageRepository(client, db_name),
    }


@pytest.fixture(params=["mongo", "memory"])
def repos(request):
    return _make_repos(request.param)


def _note(note_id, user_id="user-1", deleted=False, minutes_ago=0):
    return {
        "title": "Note " + note_id,
        "body": "Body " + note_id,
        "user_id": user_id,
        "user_name": "Test User",
        "timestamp": datetime.utcnow() - timedelta(minutes=minutes_ago),
        "date_string": "1 Jan 2026",
        "id": note_id,
        "deleted": deleted,
    }


def _message(message_id, from_id="user-1", to_id="user-2", deleted=False,
             hidden_for_sender=False, minutes_ago=0):
    return {
        "title": "Message " + message_id,
        "body": "Body " + message_id,
        "from_id": from_id,
        "from_name": "Sender",
        "to_id": to_id,
        "to_name": "Recipient",
        "timestamp": datetime.utcnow() - timedelta(minutes=minutes_ago),
        "date_string": "1 Jan 2026 00:00",
        "id": message_id,
        "deleted": deleted,
        "hidden_for_sender": hidden_for_sender,
    }


def _user(user_id, email=None, verified=False):
    return {
        "id": user_id,
        "title": "Dr",
        "first_name": "Test",
        "last_name": "User",
        "email": email or user_id + "@example.com",
        "verified": verified,
    }


# USER REPOSITORY


def test_user_insert_and_find(repos):
    assert repos["users"].insert(_user("u1")) is True
    by_id = repos["users"].find_by_id("u1")
    by_email = repos["users"].find_by_email("u1@example.com")
    assert by_id["first_name"] == "Test"
    assert by_email["id"] == "u1"
    assert "_id" not in by_id
    assert repos["users"].find_by_id("missing") is None
    assert repos["users"].count() == 1


def test_user_find_all_except(repos):
    repos["users"].insert(_user("u1"))
    repos["users"].insert(_user("u2"))
    others = repos["users"].find_all_except("u1")
    assert [user["id"] for user in others] == ["u2"]


def test_user_verify_update_and_delete(repos):
    repos["users"].insert(_user("u1"))
    assert repos["users"].verify("u1@example.com") is True
    assert repos["users"].find_by_id("u1")["verified"] is True
    assert repos["users"].update_name("u1@example.com", "Ms", "New", "Name") is True
    updated = repos["users"].find_by_id("u1")
    assert (updated["title"], updated["first_name"], updated["last_name"]) == ("Ms", "New", "Name")
    assert repos["users"].delete("u1") is True
    assert repos["users"].find_by_id("u1") is None


# NOTE REPOSITORY


def test_note_insert_and_find_active(repos):
    repos["notes"].insert(_note("n1", minutes_ago=5))
    repos["notes"].insert(_note("n2", minutes_ago=1))
    repos["notes"].insert(_note("n3", user_id="other"))
    repos["notes"].insert(_note("n4", deleted=True))
    notes = repos["notes"].find_active_for_user("user-1")
    assert [note["id"] for note in notes] == ["n2", "n1"]  # newest first
    assert repos["notes"].count() == 3  # deleted note excluded


def test_note_soft_delete_is_idempotent(repos):
    repos["notes"].insert(_note("n1"))
    assert repos["notes"].soft_delete("n1") is True
    assert repos["notes"].soft_delete("n1") is True  # second delete: same result
    assert repos["notes"].soft_delete("missing") is False
    assert repos["notes"].count() == 0


def test_note_concurrent_delete_same_id(repos):
    repos["notes"].insert(_note("n1"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: repos["notes"].soft_delete("n1"), range(16)))
    # Idempotent semantics: every concurrent delete reports the note matched.
    assert all(result is True for result in results)
    assert repos["notes"].count() == 0


def test_note_delete_for_user(repos):
    repos["notes"].insert(_note("n1"))
    repos["notes"].insert(_note("n2", user_id="other"))
    assert repos["notes"].delete_for_user("user-1") is True
    remaining = repos["notes"].find_active_for_user("other")
    assert [note["id"] for note in remaining] == ["n2"]


# MESSAGE REPOSITORY


def test_message_inbox_and_sent(repos):
    repos["messages"].insert(_message("m1", minutes_ago=5))
    repos["messages"].insert(_message("m2", minutes_ago=1))
    repos["messages"].insert(_message("m3", deleted=True))
    repos["messages"].insert(_message("m4", hidden_for_sender=True))
    inbox = repos["messages"].find_inbox("user-2")
    sent = repos["messages"].find_sent("user-1")
    assert [message["id"] for message in inbox] == ["m4", "m2", "m1"]
    assert [message["id"] for message in sent] == ["m2", "m1"]
    assert repos["messages"].count() == 3


def test_message_soft_delete_and_hide(repos):
    repos["messages"].insert(_message("m1"))
    repos["messages"].insert(_message("m2"))
    assert repos["messages"].hide_for_sender("m1") is True
    assert [message["id"] for message in repos["messages"].find_sent("user-1")] == ["m2"]
    assert repos["messages"].soft_delete("m2") is True
    assert repos["messages"].soft_delete("m2") is True  # idempotent
    assert repos["messages"].soft_delete("missing") is False
    assert repos["messages"].count() == 1


def test_message_concurrent_delete_same_id(repos):
    repos["messages"].insert(_message("m1"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: repos["messages"].soft_delete("m1"), range(16)))
    assert all(result is True for result in results)
    assert repos["messages"].count() == 0


def test_message_delete_for_user(repos):
    repos["messages"].insert(_message("m1", from_id="user-1", to_id="user-2"))
    repos["messages"].insert(_message("m2", from_id="user-3", to_id="user-1"))
    repos["messages"].insert(_message("m3", from_id="user-3", to_id="user-4"))
    assert repos["messages"].delete_for_user("user-1") is True
    assert repos["messages"].count() == 1


# CONCURRENT DELETE SEMANTICS MATCH ACROSS BACKENDS


def test_concurrent_delete_semantics_match_across_backends():
    outcomes = {}
    for backend in ("mongo", "memory"):
        backend_repos = _make_repos(backend)
        backend_repos["notes"].insert(_note("shared-id"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: backend_repos["notes"].soft_delete("shared-id"),
                range(16)))
        outcomes[backend] = (results, backend_repos["notes"].count())
    assert outcomes["mongo"] == outcomes["memory"]


# FACTORY + FALLBACK


def test_factory_missing_uri_uses_memory():
    bundle = repositories.build_repositories(None)
    assert bundle.backend_name == "memory"
    assert bundle.notes.insert(_note("n1")) is True
    assert bundle.notes.count() == 1


def test_factory_unreachable_uri_falls_back_to_memory():
    bundle = repositories.build_repositories(
        "mongodb://127.0.0.1:1/", connect_timeout_ms=200)
    assert bundle.backend_name == "memory"
    assert bundle.notes.insert(_note("n1")) is True
    assert bundle.notes.count() == 1


def test_factory_healthy_uri_uses_mongo(monkeypatch):
    monkeypatch.setattr(repositories, "MongoClient", mongomock.MongoClient)
    bundle = repositories.build_repositories("mongodb://example/")
    assert bundle.backend_name == "mongo"
    assert bundle.notes.insert(_note("n1")) is True
    assert bundle.notes.count() == 1


class _BrokenPrimary:
    """Primary repository whose connection drops mid-operation."""

    def __getattr__(self, name):
        raise PyMongoError("connection lost")


def test_mid_operation_disconnect_falls_back_without_raising(monkeypatch):
    monkeypatch.setattr(repositories, "MongoClient", mongomock.MongoClient)
    bundle = repositories.build_repositories("mongodb://example/")
    assert bundle.backend_name == "mongo"

    # Simulate the MongoDB connection dropping after startup.
    bundle.notes._primary = _BrokenPrimary()

    # The write degrades to the memory backend instead of raising.
    assert bundle.notes.insert(_note("n1")) is True
    assert bundle.backend_name == "memory"
    assert bundle.notes.count() == 1
    assert [note["id"] for note in bundle.notes.find_active_for_user("user-1")] == ["n1"]


def test_fallback_never_raises_even_if_memory_backend_fails():
    bundle = repositories.build_repositories(None)
    bundle.notes._fallback = _BrokenPrimary()
    bundle.users._fallback = _BrokenPrimary()
    # Every read/write degrades to a safe default instead of raising.
    assert bundle.notes.insert(_note("n1")) is False
    assert bundle.notes.count() == 0
    assert bundle.notes.find_active_for_user("user-1") == []
    assert bundle.notes.soft_delete("n1") is False
    assert bundle.users.find_by_id("u1") is None
    assert bundle.users.find_all_except("u1") == []
