# Language tests: switch to Khmer and back, and keep the screens valid.
from conftest import run
import bot
from test_screens import all_screens, check_html


def test_lang_switch(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.msg("/lang")
        text = h.last_text()
        assert "Language" in text, text          # the English language screen
        await h.tap("lang:kh")
        await h.msg("/start")
        text = h.last_text()
        assert "របាយការណ៍ហូស្តេល" in text, text   # now Khmer
        # a report screen in Khmer
        await h.tap("new")
        text = h.last_text()
        assert "បន្ទប់" in text, text
        # back to English
        await h.msg("/lang")
        await h.tap("lang:en")
        await h.msg("/start")
        assert "Hostel report" in h.last_text(), h.last_text()
        await h.stop()

    run(scenario(harness))


def test_kh_screens_valid_html():
    bot._LANG = "kh"
    try:
        for screen in all_screens():
            check_html(screen[0])
    finally:
        bot._LANG = "en"


def test_kh_no_missing_keys(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        await h.msg("/lang")
        await h.tap("lang:kh")
        # walk the main screens in Khmer; a missing key would show the raw key name
        for cmd in ("new", "m:rooms", "in:bar", "in:discount", "in:expense", "review",
                    "cash:main", "hist:0", "stor:0", "mon:cur"):
            await h.tap(cmd)
            text = h.last_text()
            assert "tr(" not in text, (cmd, text)
        await h.stop()

    run(scenario(harness))
