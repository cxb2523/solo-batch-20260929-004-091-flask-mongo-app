"""Repository layer.

All persistence calls live here.  Every model (note / message / user) is
accessed through a repository interface with two interchangeable backends:

* ``Mongo*Repository``  - a real MongoDB database via ``pymongo``
* ``Memory*Repository`` - an in-memory database via ``mongomock``

``RepositoryManager`` decides which backend is active based on ``MONGO_URI``
and transparently falls back to the in-memory backend when MongoDB is not
configured, cannot be reached or loses the connection mid-session.  During a
fallback every read/write is degraded gracefully: repository methods never
propagate backend exceptions to the caller.
"""

import os
import threading

import mongomock
import pymongo


# Backend identifiers
MONGO = "mongo"
MEMORY = "memory"

# Friendly labels shown on the index status bar
BACKEND_LABELS = {
    MONGO: "MongoDB",
    MEMORY: "In-memory (mongomock)",
}

# Placeholder value found in the example configuration file; treat it as
# "not configured" rather than attempting to connect to it.
URI_PLACEHOLDERS = {"", "<MongoDB URI>", None}


# ---------------------------------------------------------------------------
# Repository interfaces
# ---------------------------------------------------------------------------

class NoteRepository:
    """Persistence interface for the ``notes`` collection."""

    def add(self, note_data):
        raise NotImplementedError

    def find_active_by_user(self, user_id):
        """Active notes belonging to a user, newest first."""
        raise NotImplementedError

    def soft_delete(self, note_id):
        """Mark a single still-active note deleted.

        Returns ``True`` only when this call transitions a note from active
        to deleted.  Deleting a missing or already-deleted id returns
        ``False`` - identically on both backends, including under
        concurrent calls.
        """
        raise NotImplementedError

    def latest_active_id(self):
        raise NotImplementedError

    def delete_all_for_user(self, user_id):
        raise NotImplementedError

    def count_active(self):
        raise NotImplementedError


class MessageRepository:
    """Persistence interface for the ``messages`` collection."""

    def add(self, message_data):
        raise NotImplementedError

    def find_inbox(self, user_id):
        raise NotImplementedError

    def find_sent(self, user_id):
        raise NotImplementedError

    def soft_delete(self, message_id):
        """See :meth:`NoteRepository.soft_delete` for shared semantics."""
        raise NotImplementedError

    def hide_from_sender(self, message_id):
        raise NotImplementedError

    def latest_active_id(self):
        raise NotImplementedError

    def delete_all_for_user(self, user_id):
        raise NotImplementedError

    def count_active(self):
        raise NotImplementedError


class UserRepository:
    """Persistence interface for the ``users`` collection."""

    def add(self, user_data):
        raise NotImplementedError

    def find_by_email(self, email):
        raise NotImplementedError

    def find_by_id(self, user_id):
        raise NotImplementedError

    def find_others(self, user_id):
        raise NotImplementedError

    def mark_verified(self, email):
        raise NotImplementedError

    def update_name(self, email, title, first_name, last_name):
        raise NotImplementedError

    def delete(self, user_id):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared query logic (keeps the two backends behaviourally identical)
# ---------------------------------------------------------------------------

class _NoteRepositoryBase(NoteRepository):
    def find_active_by_user(self, user_id):
        return list(
            self._collection.find(
                {"user_id": user_id, "deleted": False}
            ).sort("timestamp", pymongo.DESCENDING)
        )

    def soft_delete(self, note_id):
        result = self._collection.update_one(
            {"id": note_id, "deleted": False},
            {"$set": {"deleted": True}},
        )
        return result.modified_count == 1

    def latest_active_id(self):
        note = self._collection.find_one(
            {"deleted": False}, sort=[("timestamp", pymongo.DESCENDING)]
        )
        return note["id"] if note else None

    def delete_all_for_user(self, user_id):
        return self._collection.delete_many({"user_id": user_id}).deleted_count

    def count_active(self):
        return self._collection.count_documents({"deleted": False})


class _MessageRepositoryBase(MessageRepository):
    def find_inbox(self, user_id):
        return list(
            self._collection.find(
                {"to_id": user_id, "deleted": False}
            ).sort("timestamp", pymongo.DESCENDING)
        )

    def find_sent(self, user_id):
        return list(
            self._collection.find(
                {"from_id": user_id,
                 "deleted": False,
                 "hidden_for_sender": False}
            ).sort("timestamp", pymongo.DESCENDING)
        )

    def soft_delete(self, message_id):
        result = self._collection.update_one(
            {"id": message_id, "deleted": False},
            {"$set": {"deleted": True}},
        )
        return result.modified_count == 1

    def hide_from_sender(self, message_id):
        result = self._collection.update_one(
            {"id": message_id, "hidden_for_sender": False},
            {"$set": {"hidden_for_sender": True}},
        )
        return result.modified_count == 1

    def latest_active_id(self):
        message = self._collection.find_one(
            {"deleted": False}, sort=[("timestamp", pymongo.DESCENDING)]
        )
        return message["id"] if message else None

    def delete_all_for_user(self, user_id):
        return self._collection.delete_many(
            {"$or": [{"from_id": user_id}, {"to_id": user_id}]}
        ).deleted_count

    def count_active(self):
        return self._collection.count_documents({"deleted": False})


class _UserRepositoryBase(UserRepository):
    def find_by_email(self, email):
        return self._collection.find_one({"email": email}, {"_id": 0})

    def find_by_id(self, user_id):
        return self._collection.find_one({"id": user_id}, {"_id": 0})

    def find_others(self, user_id):
        return list(
            self._collection.find({"id": {"$ne": user_id}}, {"_id": 0})
        )

    def mark_verified(self, email):
        result = self._collection.update_one(
            {"email": email}, {"$set": {"verified": True}}
        )
        return result.matched_count == 1

    def update_name(self, email, title, first_name, last_name):
        result = self._collection.update_one(
            {"email": email},
            {"$set": {"title": title,
                      "first_name": first_name,
                      "last_name": last_name}},
        )
        return result.matched_count == 1

    def delete(self, user_id):
        return self._collection.delete_one({"id": user_id}).deleted_count == 1


# ---------------------------------------------------------------------------
# MongoDB backend (pymongo)
# ---------------------------------------------------------------------------

class MongoNoteRepository(_NoteRepositoryBase):
    def __init__(self, db):
        self._collection = db.notes

    def add(self, note_data):
        return self._collection.insert_one(note_data).inserted_id is not None


class MongoMessageRepository(_MessageRepositoryBase):
    def __init__(self, db):
        self._collection = db.messages

    def add(self, message_data):
        return self._collection.insert_one(message_data).inserted_id is not None


class MongoUserRepository(_UserRepositoryBase):
    def __init__(self, db):
        self._collection = db.users

    def add(self, user_data):
        return self._collection.insert_one(user_data).inserted_id is not None


# ---------------------------------------------------------------------------
# In-memory backend (mongomock)
# ---------------------------------------------------------------------------

class MemoryNoteRepository(_NoteRepositoryBase):
    def __init__(self, db, lock):
        self._collection = db.notes
        self._lock = lock

    def add(self, note_data):
        with self._lock:
            return self._collection.insert_one(note_data).inserted_id is not None

    def soft_delete(self, note_id):
        with self._lock:
            return super().soft_delete(note_id)

    def delete_all_for_user(self, user_id):
        with self._lock:
            return super().delete_all_for_user(user_id)


class MemoryMessageRepository(_MessageRepositoryBase):
    def __init__(self, db, lock):
        self._collection = db.messages
        self._lock = lock

    def add(self, message_data):
        with self._lock:
            return self._collection.insert_one(message_data).inserted_id is not None

    def soft_delete(self, message_id):
        with self._lock:
            return super().soft_delete(message_id)

    def hide_from_sender(self, message_id):
        with self._lock:
            return super().hide_from_sender(message_id)

    def delete_all_for_user(self, user_id):
        with self._lock:
            return super().delete_all_for_user(user_id)


class MemoryUserRepository(_UserRepositoryBase):
    def __init__(self, db, lock):
        self._collection = db.users
        self._lock = lock

    def add(self, user_data):
        with self._lock:
            return self._collection.insert_one(user_data).inserted_id is not None

    def delete(self, user_id):
        with self._lock:
            return super().delete(user_id)


# ---------------------------------------------------------------------------
# Fault-tolerant proxy and manager
# ---------------------------------------------------------------------------

# Degraded return value for each method name, used when even the in-memory
# backend raises (it never should, but callers must never see an exception).
_DEFAULTS = {
    "add": False,
    "soft_delete": False,
    "hide_from_sender": False,
    "mark_verified": False,
    "update_name": False,
    "delete": False,
    "delete_all_for_user": 0,
    "count_active": 0,
    "latest_active_id": None,
    "find_active_by_user": list,
    "find_inbox": list,
    "find_sent": list,
    "find_others": list,
    "find_by_email": None,
    "find_by_id": None,
}


class _RepositoryProxy:
    """Delegates to the active backend and triggers fallback on failure."""

    def __init__(self, manager, mongo_repo_cls, memory_repo_cls):
        self._manager = manager
        self._mongo_cls = mongo_repo_cls
        self._memory_cls = memory_repo_cls

    def _target(self):
        if self._manager.backend == MONGO:
            return self._mongo_cls(self._manager.mongo_db)
        return self._memory_cls(self._manager.memory_db, self._manager.lock)

    def __getattr__(self, name):
        default = _DEFAULTS[name]

        def call(*args, **kwargs):
            try:
                return getattr(self._target(), name)(*args, **kwargs)
            except Exception as error:
                # Missing config is handled at startup; this covers
                # connect timeouts and mid-session disconnects.
                if self._manager.backend == MONGO:
                    self._manager._fallback(error)
                try:
                    return getattr(self._target(), name)(*args, **kwargs)
                except Exception as memory_error:
                    self._manager._log_memory_error(name, memory_error)
                    return default() if callable(default) else default

        return call


class RepositoryManager:
    """Builds both backends and exposes proxies for every model."""

    def __init__(self, mongo_uri=None, dbname=None,
                 server_selection_timeout_ms=1000,
                 mongo_db=None, memory_db=None):
        self.lock = threading.RLock()
        self.memory_db = memory_db or mongomock.MongoClient().db
        self.mongo_db = mongo_db
        self.backend = MEMORY
        self.degraded = False
        self.fallback_reason = None
        self.connect_error = None

        if mongo_db is not None:
            # Pre-connected database (tests / explicit injection).
            self.backend = MONGO
        else:
            self._connect(
                self._resolve_uri(mongo_uri),
                dbname,
                server_selection_timeout_ms,
            )

        self.notes = _RepositoryProxy(
            self, MongoNoteRepository, MemoryNoteRepository)
        self.messages = _RepositoryProxy(
            self, MongoMessageRepository, MemoryMessageRepository)
        self.users = _RepositoryProxy(
            self, MongoUserRepository, MemoryUserRepository)

    @staticmethod
    def _resolve_uri(mongo_uri):
        if mongo_uri is not None:
            uri = mongo_uri
        else:
            # MONGO_URI is the primary switch; MONGODB_URI kept as a
            # backwards-compatible alias.
            uri = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI")
        if uri is not None:
            uri = uri.strip()
        return None if uri in URI_PLACEHOLDERS else uri

    def _connect(self, uri, dbname, timeout_ms):
        if not uri:
            self.fallback_reason = "MONGO_URI not configured"
            return
        try:
            client = pymongo.MongoClient(
                uri,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
                socketTimeoutMS=timeout_ms,
            )
            parsed_dbname = None
            try:
                parsed_dbname = pymongo.uri_parser.parse_uri(uri).get("database")
            except Exception:
                parsed_dbname = None
            resolved_dbname = (
                dbname
                or parsed_dbname
                or os.environ.get("MONGO_DBNAME")
                or "flask_mongo_app"
            )
            client.admin.command("ping")
            self.mongo_db = client[resolved_dbname]
            self.backend = MONGO
        except Exception as error:
            self.connect_error = error
            self.degraded = True
            self.fallback_reason = "MongoDB unavailable at startup"
            self.backend = MEMORY

    def _fallback(self, error):
        # Called from proxy call sites; keep the transition atomic.
        with self.lock:
            if self.backend == MONGO:
                self.backend = MEMORY
                self.degraded = True
                self.fallback_reason = "MongoDB connection lost"
                print("repository: MongoDB connection lost: {}".format(error))

    def _log_memory_error(self, method_name, error):
        # Memory backend failures are swallowed by contract.
        print("repository: in-memory backend failed in {}: {}".format(
            method_name, error))

    @property
    def backend_label(self):
        return BACKEND_LABELS[self.backend]

    def status(self):
        """Status-bar payload.  Never raises, even on a broken backend."""
        try:
            note_count = self.notes.count_active()
        except Exception:
            note_count = 0
        try:
            message_count = self.messages.count_active()
        except Exception:
            message_count = 0
        return {
            "backend": self.backend,
            "backend_label": self.backend_label,
            "degraded": self.degraded,
            "fallback_reason": self.fallback_reason,
            "note_count": note_count,
            "message_count": message_count,
        }

    def _switch_to_mongo_for_tests(self, mongo_db):
        """Test seam: simulate recovery / swap of the Mongo database."""
        self.mongo_db = mongo_db
        self.backend = MONGO
