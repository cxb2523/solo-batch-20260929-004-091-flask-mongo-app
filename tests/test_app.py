import json

from repositories import MEMORY, MONGO


def test_index_returns_200_with_status_bar(client, manager):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="backend-status-bar"' in body
    if manager.backend == MONGO:
        assert "MongoDB" in body
    else:
        assert "mongomock" in body
    assert 'id="note-count"' in body
    assert 'id="message-count"' in body
    assert 'Add note' in body
    assert 'Delete note' in body


def test_status_api(client, manager):
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["backend"] == manager.backend
    assert payload["note_count"] == 0
    assert payload["message_count"] == 0


def test_add_note_api_refreshes_counts(client, manager):
    response = client.post("/api/add_note")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["action"] == "add_note"
    assert payload["note_count"] == 1
    assert payload["message_count"] == 0
    assert payload["id"]


def test_delete_note_api_success_then_no_note(client, manager):
    added = client.post("/api/add_note").get_json()
    first = client.post("/api/delete_note").get_json()
    assert first["success"] is True
    assert first["id"] == added["id"]
    assert first["note_count"] == 0

    second = client.post("/api/delete_note").get_json()
    assert second["success"] is False
    assert second["note_count"] == 0


def test_add_and_delete_message_api(client, manager):
    added = client.post("/api/add_message").get_json()
    assert added["success"] is True
    assert added["message_count"] == 1

    deleted = client.post("/api/delete_message").get_json()
    assert deleted["success"] is True
    assert deleted["id"] == added["id"]
    assert deleted["message_count"] == 0


def test_concurrent_delete_note_requests_single_success(client, manager):
    added = client.post("/api/add_note").get_json()
    note_id = added["id"]

    import threading
    results = []

    def delete():
        response = client.post(
            "/api/delete_note", data={"note_id": note_id})
        results.append(response.get_json())

    threads = [threading.Thread(target=delete) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [result for result in results if result["success"]]
    assert len(successes) == 1
    assert client.get("/api/status").get_json()["note_count"] == 0


def test_add_note_form_route_requires_login(client):
    response = client.post(
        "/add_note", data={"title": "t", "body": "b"})
    # flask-login redirects unauthenticated users to the login page
    assert response.status_code in (301, 302)
    assert "/login" in response.headers["Location"]


def test_delete_note_form_route_requires_login(client):
    response = client.post("/delete_note", data={"note_id": "x"})
    assert response.status_code in (301, 302)
    assert "/login" in response.headers["Location"]


def test_status_payload_is_json_serialisable(client, manager):
    response = client.get("/api/status")
    # Must not raise
    json.loads(response.get_data(as_text=True))
