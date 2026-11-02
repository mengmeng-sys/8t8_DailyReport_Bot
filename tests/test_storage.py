# Tests H: the Storage screen and storage-aware backup/restore.
from conftest import run
import storage
from test_import import DATA, make_report, send_file


async def send_bytes(h, raw, name, file_id="zip1", mime="application/zip"):
    h.fake.files[file_id] = raw
    await h.msg(document={"file_id": file_id, "file_unique_id": "u" + file_id,
                          "file_name": name, "file_size": len(raw), "mime_type": mime})


def test_h_storage_screen(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        await h.tap("imp:save")

        # the Storage button on Home opens the list
        await h.msg("/start")
        await h.tap("stor:0")
        text = h.last_text()
        assert "Storage" in text, text
        assert h.has_button("20/10/2026"), h.button_texts()

        # preview
        await h.tap("st:v:2026-10-20")
        assert "IMPORT" in h.last_text(), h.last_text()

        # send the file again
        await h.tap("st:f:2026-10-20")
        assert h.last_document() is not None

        # delete asks for confirmation
        await h.tap("st:d:2026-10-20")
        assert "Delete the stored file" in h.last_text(), h.last_text()
        await h.tap("st:dy:2026-10-20")
        assert not storage.stored_report_path("2026-10-20").exists()
        assert h.has_button("↩️ Undo"), h.button_texts()
        await h.stop()

    run(scenario(harness))


def test_h_backup_restore_storage(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        await h.tap("imp:save")

        # /backup includes the stored file
        await h.msg("/backup")
        doc = h.last_document()
        assert doc is not None
        assert b"storage/reports/2026-10-20.xlsx" in doc

        # remove the stored file, then restore from the backup
        storage.delete_stored_report("2026-10-20")
        await send_bytes(h, doc, "hostel-backup.zip")
        text = h.last_text()
        assert "1 file(s) added" in text, text
        assert storage.stored_report_path("2026-10-20").exists()

        # restoring again never overwrites
        before = storage.stored_report_path("2026-10-20").read_bytes()
        await send_bytes(h, doc, "hostel-backup.zip")
        assert "already there" in h.last_text(), h.last_text()
        assert storage.stored_report_path("2026-10-20").read_bytes() == before
        await h.stop()

    run(scenario(harness))
