# MongoDB backend: reports, drafts, trash, stored files and backup all live in MongoDB,
# so nothing is lost when Render wipes the disk on a redeploy.
import datetime

from conftest import run
import db
import history
import storage
from test_import import DATA, make_report, send_file


TODAY = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")


def test_reports(mongo):
    assert db.use_mongo()
    assert history.load_report("2026-10-20") is None
    history.save_report("2026-10-20", {"rooms": {"pool": [40, 0, 20]}})
    history.save_report("2026-10-21", {"rooms": {}})
    assert history.load_report("2026-10-20") == {"rooms": {"pool": [40, 0, 20]}}
    assert history.list_reports() == ["2026-10-21", "2026-10-20"]
    assert history.delete_report("2026-10-20") is True
    assert history.delete_report("2026-10-20") is False
    assert history.list_reports() == ["2026-10-21"]


def test_drafts(mongo):
    session = {"date": TODAY, "explicit": True, "saved": False,
               "data": {"rooms": {"pool": [1, 2, 3]}}}
    assert storage.load_draft(TODAY) is None
    storage.save_draft(session)
    d = storage.load_draft(TODAY)
    assert d["data"]["rooms"]["pool"] == [1, 2, 3]
    assert d["explicit"] is True
    assert d["saved"] is False
    assert [r["date"] for r in storage.list_drafts()] == [TODAY]
    assert [r["date"] for r in storage.list_drafts(exclude=TODAY)] == []
    assert storage.delete_draft(TODAY) is True
    assert storage.load_draft(TODAY) is None


def test_trash(mongo):
    name = storage.trash("history", "2026-10-20", {"data": {"rooms": {}}})
    assert storage.restore_trash(name) == {"data": {"rooms": {}}}
    storage.delete_trash(name)
    assert storage.restore_trash(name) is None
    # a bad id is ignored
    assert storage.restore_trash("not-an-id") is None
    # prune removes old entries
    mongo["trash"].insert_one({"kind": "history", "iso": "2026-01-01", "payload": {},
                               "at": datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)})
    storage.prune_trash(days=7)
    assert mongo["trash"].count_documents({}) == 0


def test_stored_files(mongo):
    raw = b"fake xlsx bytes"
    assert storage.get_stored_report("2026-10-20") is None
    storage.store_report("2026-10-20", raw)
    assert storage.get_stored_report("2026-10-20") == raw
    assert storage.stored_report_exists("2026-10-20") is True
    assert storage.list_reports() == ["2026-10-20"]
    assert storage.stored_file_b64("2026-10-20") == "ZmFrZSB4bHN4IGJ5dGVz"
    # storing again replaces the file instead of adding a second copy
    storage.store_report("2026-10-20", b"new")
    assert storage.get_stored_report("2026-10-20") == b"new"
    assert mongo["fs.files"].count_documents({"filename": "2026-10-20.xlsx"}) == 1
    # delete
    assert storage.delete_stored_report("2026-10-20") is True
    assert storage.get_stored_report("2026-10-20") is None
    assert storage.stored_report_exists("2026-10-20") is False
    # restore from base64
    storage.restore_stored("2026-10-20", "ZmFrZSB4bHN4IGJ5dGVz")
    assert storage.get_stored_report("2026-10-20") == raw


def test_backup_restore(mongo):
    history.save_report("2026-10-20", {"rooms": {"pool": [40, 0, 20]}})
    history.save_report("2026-10-21", {"rooms": {}})
    storage.store_report("2026-10-20", b"report-bytes")
    raw, count = storage.backup_zip()
    assert count == 2
    assert b"storage/reports/2026-10-20.xlsx" in raw

    # wipe everything, then restore from the backup
    history.delete_report("2026-10-20")
    history.delete_report("2026-10-21")
    storage.delete_stored_report("2026-10-20")
    assert history.list_reports() == []
    assert storage.list_reports() == []

    result = storage.restore_zip(raw)
    assert result == (2, 0, 0, 1, 0), result
    assert history.load_report("2026-10-20") == {"rooms": {"pool": [40, 0, 20]}}
    assert storage.get_stored_report("2026-10-20") == b"report-bytes"

    # restoring again never overwrites what is already there
    history.save_report("2026-10-20", {"rooms": {"changed": True}})
    result = storage.restore_zip(raw)
    assert result == (0, 2, 0, 0, 1), result
    assert history.load_report("2026-10-20") == {"rooms": {"changed": True}}


def test_bot_storage_flow(harness, mongo, tmp_path):
    """The Storage screen works with MongoDB: preview, send, delete, undo."""
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        await h.tap("imp:save")

        await h.msg("/start")
        await h.tap("stor:0")
        assert "Storage" in h.last_text(), h.last_text()

        await h.tap("st:v:2026-10-20")
        assert "IMPORT" in h.last_text(), h.last_text()

        await h.tap("st:f:2026-10-20")
        assert h.last_document() is not None

        await h.tap("st:d:2026-10-20")
        await h.tap("st:dy:2026-10-20")
        assert storage.get_stored_report("2026-10-20") is None
        assert h.has_button("↩️ Undo"), h.button_texts()

        await h.tap("undo")
        assert storage.get_stored_report("2026-10-20") is not None
        await h.stop()

    run(scenario(harness))


def test_ping_shows_storage(harness, mongo):
    async def scenario(h):
        await h.start()
        await h.msg("/ping")
        assert "MongoDB" in h.last_text(), h.last_text()
        await h.stop()

    run(scenario(harness))
