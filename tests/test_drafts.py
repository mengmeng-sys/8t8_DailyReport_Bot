# Tests A-E and I: drafts, autosave, resume, undo, and the keypad.
import asyncio
import datetime

from conftest import run
import history
import storage


TODAY = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
YESTERDAY = (datetime.datetime.now().astimezone() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def dmy(iso):
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


async def fill_pool_bar(h):
    """Fill pool 40/0/20 and bar 50/10/5 through the UI."""
    await h.tap("new")
    await h.tap("m:rooms")
    await h.tap("in:rooms:pool")
    await h.msg("40 0 20")
    await h.tap("m:main")
    await h.tap("in:bar")
    await h.msg("50 10 5")


def check_report(h, pool="40", bar="50"):
    text = h.last_text()
    assert "Pool view" in text, text
    assert pool in text, text
    assert "Bar" in text, text
    assert bar in text, text


def test_a_draft_resume(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)
        await h.tap("review")
        check_report(h)

        # Home then Start: the numbers must still be there, with the draft banner
        await h.tap("home")
        assert "Draft in progress" in h.last_text(), h.last_text()
        await h.tap("new")
        await h.tap("review")
        check_report(h)

        # /today
        await h.msg("/today")
        await h.tap("review")
        check_report(h)

        # the "📝 Today" shortcut
        await h.msg("📝 Today")
        await h.tap("review")
        check_report(h)

        await h.stop()

    run(scenario(harness))


def test_a_draft_file_written(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)
        d = storage.load_draft(TODAY)
        assert d is not None
        assert d["data"]["rooms"]["pool"] == [40.0, 0.0, 20.0]
        assert d["data"]["bar"] == [50.0, 10.0, 5.0]
        await h.stop()

    run(scenario(harness))


def test_b_restart_offers_draft(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)
        # simulated restart: a brand new Application on the same DATA_DIR
        await h.start()
        await h.msg("/start")
        assert "Draft in progress" in h.last_text(), h.last_text()
        await h.tap("d:cont")
        await h.tap("review")
        check_report(h)
        await h.stop()

    run(scenario(harness))


def test_c_yesterday_draft_offered(harness):
    async def scenario(h):
        session = {"date": YESTERDAY, "explicit": True, "saved": False,
                   "data": {"rooms": {"city": [12, 0, 0]}, "tours": {}, "bar": [], "discount": {},
                            "expenses": [], "cash_check": {}}}
        storage.save_draft(session)
        await h.start()
        await h.msg("/start")
        text = h.last_text()
        assert "Unfinished draft from" in text, text
        await h.tap(f"d:cont:{YESTERDAY}")
        assert dmy(YESTERDAY) in h.last_text(), h.last_text()
        d = storage.load_draft(YESTERDAY)
        assert d["data"]["rooms"]["city"] == [12.0, 0.0, 0.0]
        await h.stop()

    run(scenario(harness))


def test_d_confirm_deletes_draft(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)
        await h.tap("review")
        await h.tap("act:generate")

        assert history.load_report(TODAY) is not None
        assert storage.load_draft(TODAY) is None

        await h.msg("/start")
        assert "already saved" in h.last_text(), h.last_text()
        await h.tap("new")
        assert "Updating saved report" in h.last_text(), h.last_text()
        await h.tap("review")
        check_report(h)
        await h.stop()

    run(scenario(harness))


def test_e_clear_undo(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)

        # Clear all asks for confirmation
        await h.tap("act:reset")
        assert "Clear all" in h.last_text(), h.last_text()
        await h.tap("act:resety")
        assert "Cleared" in h.last_text(), h.last_text()
        assert h.has_button("↩️ Undo"), h.button_texts()
        # Undo restores the numbers
        await h.tap("undo")
        await h.tap("review")
        check_report(h)
        await h.stop()

    run(scenario(harness))


def test_e_start_new_undo(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)
        await h.tap("home")
        await h.tap("d:new")
        assert "Start a new report" in h.last_text(), h.last_text()
        await h.tap("d:newy")
        assert "Started a new report" in h.last_text(), h.last_text()
        assert h.has_button("↩️ Undo"), h.button_texts()
        await h.tap("undo")
        await h.tap("review")
        check_report(h)
        await h.stop()

    run(scenario(harness))


def test_e_discard_undo(harness):
    async def scenario(h):
        session = {"date": YESTERDAY, "explicit": True, "saved": False,
                   "data": {"rooms": {"city": [12, 0, 0]}, "tours": {}, "bar": [], "discount": {},
                            "expenses": [], "cash_check": {}}}
        storage.save_draft(session)
        await h.start()
        await h.msg("/start")
        await h.tap(f"d:disc:{YESTERDAY}")
        assert "Discard the draft" in h.last_text(), h.last_text()
        await h.tap(f"d:discy:{YESTERDAY}")
        assert storage.load_draft(YESTERDAY) is None
        assert h.has_button("↩️ Undo"), h.button_texts()
        await h.tap("undo")
        assert storage.load_draft(YESTERDAY) is not None
        await h.stop()

    run(scenario(harness))


def test_i_keypad_shows_newest_state(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.tap("new")
        await h.tap("m:rooms")
        await h.tap("in:rooms:pool")
        # fast taps: several digits one after another
        await h.tap("k:1")
        await h.tap("k:2")
        await h.tap("k:3")
        text = h.last_text()
        assert "123" in text, text
        # the keypad still works and saves
        await h.tap("k:s")
        assert "saved" in h.last_text(), h.last_text()
        await h.stop()

    run(scenario(harness))


def test_i_debounced_draft_save(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.tap("new")
        await h.tap("m:rooms")
        await h.tap("in:rooms:pool")
        await h.tap("k:4")
        await h.tap("k:0")
        await asyncio.sleep(2)                 # let the debounce write the draft
        await h.tap("k:s")
        d = storage.load_draft(TODAY)
        assert d["data"]["rooms"]["pool"] == [40.0, 0.0, 0.0]
        await h.stop()

    run(scenario(harness))


def test_i_fast_taps_concurrent(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.tap("new")
        await h.tap("m:rooms")
        await h.tap("in:rooms:pool")
        await asyncio.gather(h.tap("k:1"), h.tap("k:2"))
        text = h.last_text()
        assert "12" in text, text          # the newest state is drawn
        await h.stop()

    run(scenario(harness))


def test_i_cash_and_month_unchanged(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await fill_pool_bar(h)
        await h.tap("cash:main")
        await h.tap("c:+:20")
        text = h.last_text()
        assert "CASH CHECK" in text, text
        assert h.has_button("$20"), h.button_texts()
        await h.tap("c:done")
        # month summary still renders
        await h.tap("home")
        await h.tap("mon:cur")
        assert "OCTOBER" in h.last_text().upper() or "NOVEMBER" in h.last_text().upper(), h.last_text()
        await h.stop()

    run(scenario(harness))
