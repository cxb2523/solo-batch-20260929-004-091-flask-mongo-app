import mongomock
import pymongo
import pytest

from repositories import RepositoryManager, MEMORY, MONGO
from run import create_app


MONGO_TEST_URI = "mongodb://localhost:27017/flask_mongo_app_test"


def mongo_available():
    try:
        client = pymongo.MongoClient(
            MONGO_TEST_URI, serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        return True
    except Exception:
        return False


@pytest.fixture(params=[MEMORY, MONGO])
def manager(request):
    backend = request.param
    if backend == MONGO:
        if not mongo_available():
            pytest.skip("MongoDB is not available on localhost:27017")
        client = pymongo.MongoClient(
            MONGO_TEST_URI, serverSelectionTimeoutMS=2000)
        db = client.get_default_database()
        for collection_name in ("notes", "messages", "users"):
            db[collection_name].delete_many({})
        return RepositoryManager(mongo_db=db)

    return RepositoryManager(
        memory_db=mongomock.MongoClient().db,
        mongo_uri="",
    )


@pytest.fixture
def client(manager):
    app = create_app(
        repository_manager=manager,
        config_overrides={"TESTING": True, "WTF_CSRF_ENABLED": False},
    )
    return app.test_client()


def make_note(note_id="note-1", user_id="user-1"):
    from note import Note
    note = Note("Title", "Body", user_id, "Demo User", id=note_id)
    return note.dict()


def make_message(message_id="message-1", from_id="user-1", to_id="user-2"):
    from message import Message
    message = Message(
        "Title", "Body", from_id, "Sender", to_id, "Recipient",
        id=message_id)
    return message.dict()


def make_user(user_id="user-1", email="user@example.com"):
    from user import User
    user = User("Mr", "Test", "User", email, id=user_id)
    return user.dict()
