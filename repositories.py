"""Repository layer centralising all persistence access for the app.

Each model (User, Note, Message) gets an abstract repository interface with
two behaviourally aligned concrete implementations:

- ``Mongo*Repository``  - backed by a real pymongo client.
- ``Memory*Repository`` - backed by mongomock (in-memory, pymongo-compatible).

Both share the same collection-operation code, so their behaviour (including
idempotent delete semantics under concurrency) is identical by construction.

``build_repositories`` selects the backend from ``MONGO_URI``: if the URI is
missing, the connection times out, or the connection drops mid-operation, the
app falls back to the in-memory backend. While falling back, reads and writes
never raise to the caller - they degrade to safe default values instead.
"""

from abc import ABC, abstractmethod
import functools
import threading

import mongomock
from pymongo import MongoClient
from pymongo.errors import PyMongoError


DEFAULT_DB_NAME = "app"
CONNECT_TIMEOUT_MS = 2000


# ABSTRACT REPOSITORY INTERFACES


class UserRepository(ABC):
    """Persistence interface for user records."""

    @abstractmethod
    def find_by_email(self, email):
        """Return the user dict for an email address, or None."""

    @abstractmethod
    def find_by_id(self, user_id):
        """Return the user dict for an id, or None."""

    @abstractmethod
    def find_all_except(self, user_id):
        """Return all user dicts except the given id."""

    @abstractmethod
    def insert(self, user_dict):
        """Insert a user dict. Return True on success."""

    @abstractmethod
    def verify(self, email):
        """Flag the user with this email as verified. Return True if matched."""

    @abstractmethod
    def update_name(self, email, title, first_name, last_name):
        """Update name fields for a user. Return True if matched."""

    @abstractmethod
    def delete(self, user_id):
        """Delete the user with this id. Return True if matched."""

    @abstractmethod
    def count(self):
        """Return the number of user records."""


class NoteRepository(ABC):
    """Persistence interface for note records."""

    @abstractmethod
    def insert(self, note_dict):
        """Insert a note dict. Return True on success."""

    @abstractmethod
    def find_active_for_user(self, user_id):
        """Return non-deleted notes for a user, newest first."""

    @abstractmethod
    def soft_delete(self, note_id):
        """Flag a note as deleted. Return True if the note exists.

        Deletes are idempotent: deleting the same id twice (even
        concurrently) reports success for a note that exists, on both
        backends.
        """

    @abstractmethod
    def delete_for_user(self, user_id):
        """Permanently delete all notes for a user. Return True on success."""

    @abstractmethod
    def count(self):
        """Return the number of non-deleted notes."""


class MessageRepository(ABC):
    """Persistence interface for message records."""

    @abstractmethod
    def insert(self, message_dict):
        """Insert a message dict. Return True on success."""

    @abstractmethod
    def find_inbox(self, user_id):
        """Return non-deleted received messages for a user, newest first."""

    @abstractmethod
    def find_sent(self, user_id):
        """Return visible sent messages for a user, newest first."""

    @abstractmethod
    def soft_delete(self, message_id):
        """Flag a message as deleted. Return True if the message exists.

        Idempotent under concurrent deletes, matching the note semantics.
        """

    @abstractmethod
    def hide_for_sender(self, message_id):
        """Hide a sent message for its sender. Return True if matched."""

    @abstractmethod
    def delete_for_user(self, user_id):
        """Permanently delete all messages sent by or to a user."""

    @abstractmethod
    def count(self):
        """Return the number of non-deleted messages."""


# SHARED COLLECTION-BASED IMPLEMENTATIONS
#
# pymongo and mongomock expose the same collection API, so a single
# implementation per model serves both backends. The concrete Mongo*/Memory*
# classes below only differ in which client they bind to, which is what keeps
# the two backends behaviourally aligned.


class _CollectionUserRepository(UserRepository):

    def __init__(self, db):
        self._users = db.users

    def find_by_email(self, email):
        return self._users.find_one({"email": email}, {"_id": 0})

    def find_by_id(self, user_id):
        return self._users.find_one({"id": user_id}, {"_id": 0})

    def find_all_except(self, user_id):
        return list(self._users.find({"id": {"$ne": user_id}}, {"_id": 0}))

    def insert(self, user_dict):
        return self._users.insert_one(user_dict).acknowledged

    def verify(self, email):
        result = self._users.update_one(
            {"email": email}, {"$set": {"verified": True}})
        return result.matched_count > 0

    def update_name(self, email, title, first_name, last_name):
        result = self._users.update_one(
            {"email": email},
            {"$set": {"title": title, "first_name": first_name,
                      "last_name": last_name}})
        return result.matched_count > 0

    def delete(self, user_id):
        return self._users.delete_one({"id": user_id}).deleted_count > 0

    def count(self):
        return self._users.count_documents({})


class _CollectionNoteRepository(NoteRepository):

    def __init__(self, db):
        self._notes = db.notes

    def insert(self, note_dict):
        return self._notes.insert_one(note_dict).acknowledged

    def find_active_for_user(self, user_id):
        cursor = self._notes.find(
            {"user_id": user_id, "deleted": False}).sort("timestamp", -1)
        return list(cursor)

    def soft_delete(self, note_id):
        # matched_count (not modified_count) keeps deletes idempotent:
        # a concurrent second delete of the same id still matches the
        # document and reports the same result as the first.
        result = self._notes.update_one(
            {"id": note_id}, {"$set": {"deleted": True}})
        return result.matched_count > 0

    def delete_for_user(self, user_id):
        self._notes.delete_many({"user_id": user_id})
        return True

    def count(self):
        return self._notes.count_documents({"deleted": False})


class _CollectionMessageRepository(MessageRepository):

    def __init__(self, db):
        self._messages = db.messages

    def insert(self, message_dict):
        return self._messages.insert_one(message_dict).acknowledged

    def find_inbox(self, user_id):
        cursor = self._messages.find(
            {"to_id": user_id, "deleted": False}).sort("timestamp", -1)
        return list(cursor)

    def find_sent(self, user_id):
        cursor = self._messages.find(
            {"from_id": user_id, "deleted": False,
             "hidden_for_sender": False}).sort("timestamp", -1)
        return list(cursor)

    def soft_delete(self, message_id):
        result = self._messages.update_one(
            {"id": message_id}, {"$set": {"deleted": True}})
        return result.matched_count > 0

    def hide_for_sender(self, message_id):
        result = self._messages.update_one(
            {"id": message_id}, {"$set": {"hidden_for_sender": True}})
        return result.matched_count > 0

    def delete_for_user(self, user_id):
        self._messages.delete_many(
            {"$or": [{"from_id": user_id}, {"to_id": user_id}]})
        return True

    def count(self):
        return self._messages.count_documents({"deleted": False})


# CONCRETE BACKENDS


class MongoUserRepository(_CollectionUserRepository):
    """User repository backed by a real MongoDB client."""

    def __init__(self, client, db_name=DEFAULT_DB_NAME):
        super().__init__(client[db_name])


class MongoNoteRepository(_CollectionNoteRepository):
    """Note repository backed by a real MongoDB client."""

    def __init__(self, client, db_name=DEFAULT_DB_NAME):
        super().__init__(client[db_name])


class MongoMessageRepository(_CollectionMessageRepository):
    """Message repository backed by a real MongoDB client."""

    def __init__(self, client, db_name=DEFAULT_DB_NAME):
        super().__init__(client[db_name])


class MemoryUserRepository(_CollectionUserRepository):
    """User repository backed by mongomock (in-memory)."""

    def __init__(self, client=None, db_name=DEFAULT_DB_NAME):
        super().__init__((client or mongomock.MongoClient())[db_name])


class MemoryNoteRepository(_CollectionNoteRepository):
    """Note repository backed by mongomock (in-memory)."""

    def __init__(self, client=None, db_name=DEFAULT_DB_NAME):
        super().__init__((client or mongomock.MongoClient())[db_name])


class MemoryMessageRepository(_CollectionMessageRepository):
    """Message repository backed by mongomock (in-memory)."""

    def __init__(self, client=None, db_name=DEFAULT_DB_NAME):
        super().__init__((client or mongomock.MongoClient())[db_name])


# RESILIENT FALLBACK WRAPPER


class _FallbackState:
    """Shared flag so every repository reports the same active backend."""

    def __init__(self, active=False):
        self.active = active
        self.lock = threading.Lock()


class ResilientRepository:
    """Thread-safe proxy around a primary repository with memory fallback.

    On a pymongo error the primary is abandoned and all further calls go to
    the in-memory fallback. No exception ever escapes to the caller: failures
    degrade to the per-method default values in ``degraded_defaults``.
    """

    def __init__(self, primary, fallback, degraded_defaults, state):
        self._primary = primary
        self._fallback = fallback
        self._degraded_defaults = degraded_defaults
        self._state = state

    @property
    def backend_name(self):
        return "memory" if self._state.active else "mongo"

    def _degraded(self, name):
        default = self._degraded_defaults.get(name, False)
        return default() if callable(default) else default

    def _invoke(self, name, *args, **kwargs):
        if not self._state.active and self._primary is not None:
            try:
                return getattr(self._primary, name)(*args, **kwargs)
            except PyMongoError:
                # Connection dropped mid-operation: fall back for good.
                with self._state.lock:
                    self._state.active = True
            except Exception:
                return self._degraded(name)
        try:
            return getattr(self._fallback, name)(*args, **kwargs)
        except Exception:
            return self._degraded(name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return functools.partial(self._invoke, name)


_USER_DEFAULTS = {
    "find_by_email": None,
    "find_by_id": None,
    "find_all_except": list,
    "insert": False,
    "verify": False,
    "update_name": False,
    "delete": False,
    "count": 0,
}

_NOTE_DEFAULTS = {
    "insert": False,
    "find_active_for_user": list,
    "soft_delete": False,
    "delete_for_user": False,
    "count": 0,
}

_MESSAGE_DEFAULTS = {
    "insert": False,
    "find_inbox": list,
    "find_sent": list,
    "soft_delete": False,
    "hide_for_sender": False,
    "delete_for_user": False,
    "count": 0,
}


# REPOSITORY BUNDLE + FACTORY


class RepositoryBundle:
    """The three model repositories plus the active backend name."""

    def __init__(self, users, notes, messages, state):
        self.users = users
        self.notes = notes
        self.messages = messages
        self._state = state

    @property
    def backend_name(self):
        return "memory" if self._state.active else "mongo"


def build_repositories(mongo_uri=None, db_name=DEFAULT_DB_NAME,
                       connect_timeout_ms=CONNECT_TIMEOUT_MS):
    """Build the repository bundle, choosing the backend from ``mongo_uri``.

    A missing URI, a connection timeout, or a refused connection selects the
    in-memory (mongomock) backend; a healthy URI selects MongoDB. Later
    mid-operation failures flip the bundle to memory automatically.
    """
    state = _FallbackState()
    memory_client = mongomock.MongoClient()

    mongo_client = None
    if mongo_uri:
        try:
            candidate = MongoClient(
                mongo_uri, serverSelectionTimeoutMS=connect_timeout_ms)
            candidate.admin.command("ping")
            mongo_client = candidate
        except PyMongoError:
            mongo_client = None

    if mongo_client is None:
        state.active = True

    users = ResilientRepository(
        MongoUserRepository(mongo_client, db_name) if mongo_client else None,
        MemoryUserRepository(memory_client, db_name),
        _USER_DEFAULTS, state)
    notes = ResilientRepository(
        MongoNoteRepository(mongo_client, db_name) if mongo_client else None,
        MemoryNoteRepository(memory_client, db_name),
        _NOTE_DEFAULTS, state)
    messages = ResilientRepository(
        MongoMessageRepository(mongo_client, db_name) if mongo_client else None,
        MemoryMessageRepository(memory_client, db_name),
        _MESSAGE_DEFAULTS, state)

    return RepositoryBundle(users, notes, messages, state)
