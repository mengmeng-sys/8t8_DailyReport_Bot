# db.py - MongoDB connection. Used when MONGODB_URI is set (Render); otherwise local files.
# On Render the disk is wiped on every restart/redeploy, so the reports, drafts, trash and
# uploaded files live in MongoDB and survive redeploys.
import os


def use_mongo():
    """True when a MongoDB URI is set. Read at call time, so .env changes and tests apply."""
    return bool(os.getenv("MONGODB_URI"))


def db_name():
    return os.getenv("MONGODB_DB", "hostel_bot")


_client = None
_db = None


def mongo():
    """The MongoDB database (creates the connection the first time it is needed)."""
    global _client, _db
    if _client is None:
        from pymongo import MongoClient
        _client = MongoClient(os.getenv("MONGODB_URI"), serverSelectionTimeoutMS=8000)
        _db = _client[db_name()]
    return _db


def ping():
    """True if MongoDB answers."""
    if not use_mongo():
        return False
    try:
        mongo().client.admin.command("ping")
        return True
    except Exception:
        return False


def reset():
    """Forget the connection (used by tests)."""
    global _client, _db
    _client = None
    _db = None
