"""App-level tests: index status bar and JSON add/delete API.

The app is imported with no MONGO_URI configured, so it must serve
everything from the in-memory fallback backend without raising.
"""

import os

import pytest

# Force the in-memory backend before the app module is imported.
os.environ.pop("MONGO_URI", None)
os.environ.pop("MONGODB_URI", None)

import run


@pytest.fixture()
def client():
    run.talisman.force_https = False
    run.app.config["TESTING"] = True
    with run.app.test_client() as test_client:
        yield test_client


def test_index_returns_200_with_status_bar(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="status-bar"' in html
    assert 'id="status-backend"' in html
    assert "memory" in html  # MongoDB disconnected -> memory backend shown
    assert 'id="status-notes-count"' in html
    assert 'id="status-messages-count"' in html
    assert 'id="add-note-btn"' in html
    assert 'id="delete-note-btn"' in html
    assert 'id="add-message-btn"' in html
    assert 'id="delete-message-btn"' in html


def test_status_endpoint_reports_memory_backend(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["backend"] == "memory"
    assert data["notes"] == 0
    assert data["messages"] == 0


def test_note_add_and_delete_updates_counts(client):
    response = client.post("/api/notes", data={"title": "First"})
    data = response.get_json()
    assert response.status_code == 200
    assert data["success"] is True
    assert data["notes"] == 1
    note_id = data["note_id"]

    response = client.post("/api/notes", data={"title": "Second"})
    assert response.get_json()["notes"] == 2

    response = client.post(f"/api/notes/{note_id}/delete")
    data = response.get_json()
    assert data["success"] is True
    assert data["notes"] == 1

    # Deleting the same id again: idempotent success, count unchanged.
    response = client.post(f"/api/notes/{note_id}/delete")
    data = response.get_json()
    assert data["success"] is True
    assert data["notes"] == 1

    # Deleting an unknown id reports failure but stays a 200 JSON response.
    response = client.post("/api/notes/does-not-exist/delete")
    data = response.get_json()
    assert data["success"] is False
    assert data["notes"] == 1


def test_message_add_and_delete_updates_counts(client):
    response = client.post("/api/messages", data={"title": "Hello"})
    data = response.get_json()
    assert response.status_code == 200
    assert data["success"] is True
    assert data["messages"] == 1
    message_id = data["message_id"]

    response = client.post(f"/api/messages/{message_id}/delete")
    data = response.get_json()
    assert data["success"] is True
    assert data["messages"] == 0

    response = client.post(f"/api/messages/{message_id}/delete")
    data = response.get_json()
    assert data["success"] is True
    assert data["messages"] == 0


def test_index_reflects_updated_counts(client):
    client.post("/api/notes", data={"title": "Counted"})
    client.post("/api/messages", data={"title": "Counted"})
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert 'id="status-notes-count"' in html
    status = client.get("/api/status").get_json()
    assert status["notes"] >= 1
    assert status["messages"] >= 1
