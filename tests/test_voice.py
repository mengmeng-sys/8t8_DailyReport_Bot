# Voice input: say "pool forty zero twenty" and the bot fills it in.
import datetime

import pytest

from conftest import run
import bot
import speech
import storage


TODAY = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")


def test_voice_free_form(harness, monkeypatch):
    async def scenario(h):
        monkeypatch.setattr(speech, "transcribe", lambda path: "pool forty zero twenty")
        h.fake.files["v1"] = b"fake audio"
        await h.start()
        await h.msg("/start")
        await h.voice()
        d = storage.load_draft(TODAY)
        assert d["data"]["rooms"]["pool"] == [40.0, 0.0, 20.0]
        assert d["data"]["bar"] == []
        await h.stop()

    run(scenario(harness))


def test_voice_in_keypad_flow(harness, monkeypatch):
    async def scenario(h):
        monkeypatch.setattr(speech, "transcribe", lambda path: "forty zero twenty")
        h.fake.files["v1"] = b"fake audio"
        await h.start()
        await h.msg("/start")
        await h.tap("new")
        await h.tap("m:rooms")
        await h.tap("in:rooms:pool")
        await h.voice()
        d = storage.load_draft(TODAY)
        assert d["data"]["rooms"]["pool"] == [40.0, 0.0, 20.0]
        await h.stop()

    run(scenario(harness))


def test_voice_bar(harness, monkeypatch):
    async def scenario(h):
        monkeypatch.setattr(speech, "transcribe", lambda path: "bar fifty ten five")
        h.fake.files["v1"] = b"fake audio"
        await h.start()
        await h.msg("/start")
        await h.voice()
        d = storage.load_draft(TODAY)
        assert d["data"]["bar"] == [50.0, 10.0, 5.0]
        await h.stop()

    run(scenario(harness))


def test_voice_unclear(harness, monkeypatch):
    async def scenario(h):
        monkeypatch.setattr(speech, "transcribe", lambda path: "")
        h.fake.files["v1"] = b"fake audio"
        await h.start()
        await h.msg("/start")
        await h.voice()
        assert "could not understand" in h.last_text().lower(), h.last_text()
        await h.stop()

    run(scenario(harness))


def test_spoken_numbers():
    cases = {
        "pool forty zero twenty": "pool 40 0 20",
        "bar fifty ten five": "bar 50 10 5",
        "twenty one": "21",
        "forty five": "45",
        "ninety nine": "99",
        "one hundred": "100",
        "pool forty oh twenty": "pool 40 0 20",
        "water twelve": "water 12",
    }
    for said, want in cases.items():
        assert speech.spoken_numbers(said) == want, (said, speech.spoken_numbers(said))
