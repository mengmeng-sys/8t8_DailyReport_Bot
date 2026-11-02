# Tests F and G: importing an uploaded xlsx, and rejecting bad files.
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from conftest import run
import fill
import history
import importer
import storage


def make_report(data, path, iso="2026-10-20"):
    fill.fill_template(data, str(path), report_date=__import__("datetime").date.fromisoformat(iso))


async def send_file(h, path, name=None, file_size=None, file_id="up1"):
    p = Path(path)
    raw = p.read_bytes() if p.exists() else b""
    h.fake.files[file_id] = raw
    await h.msg(document={"file_id": file_id, "file_unique_id": "u" + file_id,
                          "file_name": name or p.name,
                          "file_size": file_size if file_size is not None else len(raw),
                          "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})


def patch_cached(path, ref, value):
    """Change a cached formula result inside the xlsx (the input cells stay the same)."""
    tmp = str(path) + ".tmp"
    with zipfile.ZipFile(str(path)) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                s = data.decode("utf-8")
                s = re.sub(rf'(<c r="{ref}"[^>]*>.*?<v>)[^<]*(</v>)', rf"\g<1>{value}\g<2>", s, flags=re.S)
                data = s.encode("utf-8")
            zout.writestr(info, data)
    shutil.move(tmp, str(path))


def simple_file(path, d2=None, b23=None):
    wb = Workbook()
    ws = wb.active
    if d2 is not None:
        ws["D2"] = d2
    if b23 is not None:
        ws["B23"] = b23
    wb.save(str(path))


DATA = {"rooms": {"pool": [40, 0, 20]}, "tours": {"bus": [5, 0, 0]}, "bar": [50, 10, 5],
        "discount": {"amount": 7, "reason": "long stay"},
        "expenses": [{"name": "water", "amount": 12}, {"name": "rice", "amount": 8.5}],
        "cash_check": {}}


def test_f_round_trip(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        text = h.last_text()
        assert "IMPORT" in text, text
        assert "Pool view" in text and "40" in text, text
        assert "Bar" in text and "50" in text, text
        assert "water" in text and "long stay" in text, text
        await h.tap("imp:save")
        assert history.load_report("2026-10-20") is not None
        assert storage.stored_report_path("2026-10-20").exists()
        saved = history.load_report("2026-10-20")
        assert saved["rooms"]["pool"] == [40.0, 0.0, 20.0]
        assert saved["bar"] == [50.0, 10.0, 5.0]
        assert saved["discount"] == {"amount": 7.0, "reason": "long stay"}
        assert saved["expenses"] == [{"name": "water", "amount": 12.0}, {"name": "rice", "amount": 8.5}]
        await h.stop()

    run(scenario(harness))


def test_f_existing_day_asks_and_trashes(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        await h.tap("imp:save")
        # send it again: it must ask before replacing
        await send_file(h, path)
        text = h.last_text()
        assert "already saved" in text, text
        assert h.has_button("Replace"), h.button_texts()
        await h.tap("imp:save")
        assert history.load_report("2026-10-20")["bar"] == [50.0, 10.0, 5.0]
        # the old version was kept in trash
        trash = list(storage._dir("trash").glob("*.json"))
        assert trash, "the replaced day must be in trash"
        await h.stop()

    run(scenario(harness))


def test_f_wrong_total_warns(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        patch_cached(path, "C27", 9999)          # cached revenue is wrong
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        text = h.last_text()
        assert "revenue" in text and "9999" in text, text
        await h.stop()

    run(scenario(harness))


def test_f_missing_date_asks(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "report.xlsx"          # no date in the name, no B23
        simple_file(path, d2=10)
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        text = h.last_text()
        assert "no date" in text, text
        await h.msg("20/10/2026")
        text = h.last_text()
        assert "IMPORT" in text and "20/10/2026" in text, text
        assert h.has_button("Save all"), h.button_texts()
        await h.tap("imp:save")
        assert history.load_report("2026-10-20") is not None
        await h.stop()

    run(scenario(harness))


def test_f_wrong_date_uses_cell(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "01-11-2026_Report.xlsx"   # name says 01/11/2026
        simple_file(path, d2=10, b23=__import__("datetime").date(2026, 10, 20))
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        text = h.last_text()
        assert "20/10/2026" in text, text
        await h.stop()

    run(scenario(harness))


def test_f_multiple_files_at_once(harness, tmp_path):
    async def scenario(h):
        days = [("2026-10-18", [10, 0, 0]), ("2026-10-19", [20, 0, 0]), ("2026-10-20", [30, 0, 0])]
        paths = []
        for iso, pool in days:
            p = tmp_path / f"{iso}_Report.xlsx"
            make_report({"rooms": {"pool": pool}, "tours": {}, "bar": [], "discount": {},
                         "expenses": [], "cash_check": {}}, p, iso)
            paths.append(p)
        await h.start()
        await h.msg("/start")
        for p in paths:                       # send them one after another, like a burst
            await send_file(h, p)
        text = h.last_text()
        assert "3 files" in text, text
        for d in ("18/10/2026", "19/10/2026", "20/10/2026"):
            assert d in text, text
        await h.tap("imp:save")
        for iso, _ in days:
            assert history.load_report(iso) is not None
            assert storage.stored_report_path(iso).exists()
        await h.stop()

    run(scenario(harness))


def test_g_bad_extensions(harness, tmp_path):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        for name in ["report.xlsm", "report.xls", "report.txt", "report.csv"]:
            await send_file(h, tmp_path / "x.xlsx", name=name)
            assert "can only read" in h.last_text(), (name, h.last_text())
        await h.stop()

    run(scenario(harness))


def test_g_too_big(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path, file_size=6_000_000)
        assert "too big" in h.last_text(), h.last_text()
        assert history.load_report("2026-10-20") is None
        await h.stop()

    run(scenario(harness))


def test_g_zip_bomb(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/worksheets/sheet1.xml", b"\0" * 30_000_000)
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        assert "zip bomb" in h.last_text(), h.last_text()
        assert history.load_report("2026-10-20") is None
        await h.stop()

    run(scenario(harness))


def test_g_negative_number(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        simple_file(path, d2=-5)
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        assert "negative" in h.last_text(), h.last_text()
        assert history.load_report("2026-10-20") is None
        await h.stop()

    run(scenario(harness))


def test_g_text_in_number_cell(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        simple_file(path, d2="abc")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        assert "not a number" in h.last_text(), h.last_text()
        assert history.load_report("2026-10-20") is None
        await h.stop()

    run(scenario(harness))


def test_g_hostile_name(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        make_report(DATA, path, "2026-10-20")
        await h.start()
        await h.msg("/start")
        await send_file(h, path, name="../../evil.xlsx")
        assert "not allowed" in h.last_text(), h.last_text()
        assert history.load_report("2026-10-20") is None
        await h.stop()

    run(scenario(harness))


def test_g_broken_file(harness, tmp_path):
    async def scenario(h):
        path = tmp_path / "20-10-2026_Report.xlsx"
        path.write_bytes(b"this is not an excel file at all")
        await h.start()
        await h.msg("/start")
        await send_file(h, path)
        assert "not a readable" in h.last_text(), h.last_text()
        assert history.load_report("2026-10-20") is None
        await h.stop()

    run(scenario(harness))
