# The step-by-step entry and the expense editor must still work.
from conftest import run
import storage


def test_guided_entry(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.tap("new")
        await h.tap("g:start")                 # step 1 of 15: pool
        text = h.last_text()
        assert "STEP 1 of 15" in text, text
        await h.msg("40 0 20")                 # commit -> step 2
        text = h.last_text()
        assert "STEP 2 of 15" in text, text
        await h.tap("g:stop")
        assert "HOSTEL REPORT" in h.last_text(), h.last_text()
        await h.tap("review")
        assert "40" in h.last_text(), h.last_text()
        assert storage.load_draft(storage_today())["data"]["rooms"]["pool"] == [40.0, 0.0, 20.0]
        await h.stop()

    run(scenario(harness))


def storage_today():
    import datetime
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d")


def test_expense_editor(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.tap("new")
        await h.tap("in:expense")
        await h.msg("water 12")
        await h.msg("rice 8")
        text = h.last_text()
        assert "water" in text and "rice" in text, text
        # edit list -> toggle "not from the drawer" -> delete one
        await h.tap("exp:list:main")
        await h.tap("exp:tog:0")
        await h.tap("exp:item:1")
        await h.tap("exp:delq:1")
        await h.tap("exp:delyes:1")
        text = h.last_text()
        assert "1. water" in text and "2. rice" not in text, text
        d = storage.load_draft(storage_today())
        assert d["data"]["expenses"] == [{"name": "water", "amount": 12.0, "cash": False}]
        await h.stop()

    run(scenario(harness))


def test_free_form_message(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.msg("pool 40 0 20, bar 50 10 5, discount 5 long stay, water 12")
        text = h.last_text()
        assert "Saved from your message" in text, text
        d = storage.load_draft(storage_today())
        assert d["data"]["rooms"]["pool"] == [40.0, 0.0, 20.0]
        assert d["data"]["bar"] == [50.0, 10.0, 5.0]
        assert d["data"]["discount"] == {"amount": 5.0, "reason": "long stay"}
        assert d["data"]["expenses"] == [{"name": "water", "amount": 12.0}]
        await h.stop()

    run(scenario(harness))
