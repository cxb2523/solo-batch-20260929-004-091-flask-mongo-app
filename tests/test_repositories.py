import threading
from datetime import datetime, timedelta

from conftest import make_note, make_message, make_user


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------

def test_note_add_and_count(manager):
    assert manager.notes.add(make_note("n1"))
    assert manager.notes.count_active() == 1


def test_note_find_only_active_for_user_sorted(manager):
    manager.notes.add(make_note("n1", user_id="a"))
    manager.notes.add(make_note("n2", user_id="b"))
    manager.notes.add(make_note("n3", user_id="a"))
    manager.notes.soft_delete("n3")

    notes = manager.notes.find_active_by_user("a")
    assert [note["id"] for note in notes] == ["n1"]


def test_note_soft_delete_semantics(manager):
    manager.notes.add(make_note("n1"))
    # First delete transitions active -> deleted: True
    assert manager.notes.soft_delete("n1") is True
    # Deleting again is a no-op: False
    assert manager.notes.soft_delete("n1") is False
    # Missing id: False
    assert manager.notes.soft_delete("does-not-exist") is False
    assert manager.notes.count_active() == 0


def test_note_concurrent_delete_same_id_has_one_winner(manager):
    manager.notes.add(make_note("race"))
    results = []

    def delete():
        results.append(manager.notes.soft_delete("race"))

    threads = [threading.Thread(target=delete) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 9
    assert manager.notes.soft_delete("race") is False


def test_note_delete_all_for_user(manager):
    manager.notes.add(make_note("n1", user_id="a"))
    manager.notes.add(make_note("n2", user_id="a"))
    manager.notes.add(make_note("n3", user_id="b"))
    assert manager.notes.delete_all_for_user("a") == 2
    assert manager.notes.find_active_by_user("a") == []
    assert manager.notes.count_active() == 1


def test_latest_active_note_id(manager):
    assert manager.notes.latest_active_id() is None
    first = make_note("n1")
    second = make_note("n2")
    first["timestamp"] = datetime.utcnow()
    second["timestamp"] = first["timestamp"] + timedelta(seconds=1)
    manager.notes.add(first)
    manager.notes.add(second)
    assert manager.notes.latest_active_id() == "n2"
    manager.notes.soft_delete("n2")
    assert manager.notes.latest_active_id() == "n1"


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def test_message_add_and_count(manager):
    assert manager.messages.add(make_message("m1"))
    assert manager.messages.count_active() == 1


def test_message_inbox_and_sent(manager):
    manager.messages.add(make_message("m1", from_id="a", to_id="b"))
    manager.messages.add(make_message("m2", from_id="b", to_id="a"))

    inbox = manager.messages.find_inbox("a")
    sent = manager.messages.find_sent("a")
    assert [message["id"] for message in inbox] == ["m2"]
    assert [message["id"] for message in sent] == ["m1"]


def test_message_hide_from_sender_excludes_from_sent(manager):
    manager.messages.add(make_message("m1", from_id="a", to_id="b"))
    assert manager.messages.hide_from_sender("m1") is True
    assert manager.messages.hide_from_sender("m1") is False
    assert manager.messages.find_sent("a") == []
    # Still in the recipient's inbox
    assert [m["id"] for m in manager.messages.find_inbox("b")] == ["m1"]


def test_message_soft_delete_semantics(manager):
    manager.messages.add(make_message("m1"))
    assert manager.messages.soft_delete("m1") is True
    assert manager.messages.soft_delete("m1") is False
    assert manager.messages.soft_delete("missing") is False
    assert manager.messages.count_active() == 0


def test_message_concurrent_delete_same_id_has_one_winner(manager):
    manager.messages.add(make_message("race"))
    results = []

    def delete():
        results.append(manager.messages.soft_delete("race"))

    threads = [threading.Thread(target=delete) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 9


def test_message_delete_all_for_user(manager):
    manager.messages.add(make_message("m1", from_id="a", to_id="b"))
    manager.messages.add(make_message("m2", from_id="b", to_id="a"))
    manager.messages.add(make_message("m3", from_id="c", to_id="d"))
    assert manager.messages.delete_all_for_user("a") == 2
    assert manager.messages.count_active() == 1


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def test_user_add_find_and_others(manager):
    assert manager.users.add(make_user("u1", "a@example.com"))
    assert manager.users.add(make_user("u2", "b@example.com"))

    by_email = manager.users.find_by_email("a@example.com")
    by_id = manager.users.find_by_id("u1")
    assert by_email["id"] == "u1"
    assert by_id["email"] == "a@example.com"
    assert "_id" not in by_email

    others = manager.users.find_others("u1")
    assert [user["id"] for user in others] == ["u2"]


def test_user_mark_verified(manager):
    manager.users.add(make_user("u1", "a@example.com"))
    assert manager.users.mark_verified("a@example.com") is True
    assert manager.users.find_by_id("u1")["verified"] is True
    assert manager.users.mark_verified("nobody@example.com") is False


def test_user_update_name(manager):
    manager.users.add(make_user("u1", "a@example.com"))
    assert manager.users.update_name("a@example.com", "Dr", "New", "Name")
    user = manager.users.find_by_id("u1")
    assert (user["title"], user["first_name"], user["last_name"]) == (
        "Dr", "New", "Name")


def test_user_delete(manager):
    manager.users.add(make_user("u1"))
    assert manager.users.delete("u1") is True
    assert manager.users.delete("u1") is False
    assert manager.users.find_by_id("u1") is None
