from conftest import run


def test_start(harness):
    async def scenario(h):
        await h.start()
        await h.msg("/start")
        text = h.last_text()
        assert "Hostel report" in text, text
        await h.stop()

    run(scenario(harness))
