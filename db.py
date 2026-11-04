# db.py - MongoDB connection. Used when MONGODB_URI is set and reachable (Render);
# otherwise the bot falls back to local files and says so in /ping and the log.
import os

_client = None
_db = None
_backend = None   # None = not decided yet, "mongo" or "files"


def configured():
    """True when MONGODB_URI is set."""
    return bool(os.getenv("MONGODB_URI"))


def db_name():
    return os.getenv("MONGODB_DB", "hostel_bot")


def mongo():
    """The MongoDB database (connects the first time it is needed)."""
    global _client, _db
    if _client is None:
        from pymongo import MongoClient
        _client = MongoClient(os.getenv("MONGODB_URI"), serverSelectionTimeoutMS=8000)
        _db = _client[db_name()]
    return _db


def ping():
    """True if MongoDB is configured and answers."""
    if not configured():
        return False
    try:
        mongo().client.admin.command("ping")
        return True
    except Exception:
        return False


def choose_backend():
    """Decide the storage backend: MongoDB if configured and reachable, else local files."""
    global _backend
    _backend = "mongo" if (configured() and ping()) else "files"
    return _backend


def use_mongo():
    """True when the bot is storing data in MongoDB."""
    if _backend is None:
        choose_backend()
    # also check the env: a stale backend choice must not override a missing MONGODB_URI
    return _backend == "mongo" and configured()


def status():
    """Fresh status for /ping: 'mongo', 'unreachable' or 'files'."""
    if not configured():
        return "files"
    return "mongo" if ping() else "unreachable"


def reset():
    """Forget the connection and the backend choice (used by tests)."""
    global _client, _db, _backend
    _client = None
    _db = None
    _backend = None
