# tests/conftest.py - fake Telegram so the bot can be tested without the internet.
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("TELEGRAM_CHAT_ID", "984488956")
os.environ.setdefault("FORCE_IPV4", "0")
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import history  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.request import HTTPXRequest  # noqa: E402

OWNER = 984488956
DATE = 1700000000


def _ok(result):
    return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")


class FakeTelegram:
    """Stands in for the Telegram Bot API. Records what the bot sends."""

    def __init__(self):
        self.messages = []      # every API call, in order
        self.files = {}         # file_id -> bytes
        self.file_paths = {}    # file_path -> bytes
        self._id = 1000

    def _next_id(self):
        self._id += 1
        return self._id

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rstrip("/").rsplit("/", 1)[-1]
        params = dict(request_data.json_parameters) if request_data and request_data.json_parameters else {}
        if "/file/bot" in url:                       # a file download
            m = re.search(r"/file/bot[^/]+/(.+)$", url)
            return 200, self.file_paths.get(m.group(1) if m else "", b"")
        if name == "getMe":
            return _ok({"id": 999, "is_bot": True, "first_name": "TestBot", "username": "test_bot"})
        if name == "sendMessage":
            mid = self._next_id()
            self.messages.append({"method": "sendMessage", "params": params, "message_id": mid})
            return _ok({"message_id": mid, "date": DATE, "text": params.get("text"),
                        "chat": {"id": params.get("chat_id", OWNER), "type": "private", "first_name": "Me"},
                        "from": {"id": 999, "is_bot": True, "first_name": "TestBot"}})
        if name == "editMessageText":
            mid = int(params.get("message_id"))
            self.messages.append({"method": "editMessageText", "params": params, "message_id": mid})
            return _ok({"message_id": mid, "date": DATE, "text": params.get("text"),
                        "chat": {"id": params.get("chat_id", OWNER), "type": "private", "first_name": "Me"},
                        "from": {"id": 999, "is_bot": True, "first_name": "TestBot"}})
        if name == "deleteMessage":
            self.messages.append({"method": "deleteMessage", "params": params})
            return _ok(True)
        if name == "sendDocument":
            mid = self._next_id()
            doc = None
            if request_data and request_data.multipart_data:
                for key, val in request_data.multipart_data.items():
                    if key == "document":
                        doc = val
            self.messages.append({"method": "sendDocument", "params": params, "message_id": mid, "document": doc})
            return _ok({"message_id": mid, "date": DATE,
                        "chat": {"id": params.get("chat_id", OWNER), "type": "private", "first_name": "Me"},
                        "from": {"id": 999, "is_bot": True, "first_name": "TestBot"},
                        "document": {"file_id": "doc1", "file_unique_id": "udoc1",
                                     "file_name": params.get("filename", "file")}})
        if name == "getFile":
            file_id = params["file_id"]
            path = f"uploads/{file_id}.xlsx"
            self.file_paths[path] = self.files.get(file_id, b"")
            return _ok({"file_id": file_id, "file_unique_id": "u1", "file_path": path,
                        "file_size": len(self.file_paths[path])})
        if name == "answerCallbackQuery":
            self.messages.append({"method": "answerCallbackQuery", "params": params})
            return _ok(True)
        if name == "setMyCommands":
            self.messages.append({"method": "setMyCommands", "params": params})
            return _ok(True)
        return _ok(True)


class Harness:
    """Builds the real Application and feeds it Updates."""

    def __init__(self, fake, tmp_path):
        self.fake = fake
        self.tmp_path = tmp_path
        self.app = None

    async def start(self):
        if self.app is not None:
            await self.app.shutdown()
        import bot
        self.app = bot.build_app()
        await self.app.initialize()

    async def stop(self):
        if self.app is not None:
            await self.app.shutdown()
            self.app = None

    async def _send(self, payload):
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    def _message(self, text=None, document=None):
        msg = {"message_id": self.fake._next_id(), "date": DATE,
               "chat": {"id": OWNER, "type": "private", "first_name": "Me"},
               "from": {"id": OWNER, "is_bot": False, "first_name": "Me"}}
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):                    # real Telegram marks commands
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if document is not None:
            msg["document"] = document
        return msg

    async def msg(self, text=None, document=None):
        await self._send({"update_id": self.fake._next_id(), "message": self._message(text, document)})

    async def voice(self, file_id="v1", file_size=1234):
        await self._send({"update_id": self.fake._next_id(), "message": {
            "message_id": self.fake._next_id(), "date": DATE,
            "chat": {"id": OWNER, "type": "private", "first_name": "Me"},
            "from": {"id": OWNER, "is_bot": False, "first_name": "Me"},
            "voice": {"file_id": file_id, "file_unique_id": "u" + file_id, "duration": 3,
                      "mime_type": "audio/ogg"}}})

    def panel_id(self):
        for m in reversed(self.fake.messages):
            if m["method"] in ("sendMessage", "editMessageText"):
                return m["message_id"]
        return None

    async def tap(self, data, message_id=None):
        if message_id is None:
            message_id = self.panel_id() or 200
        await self._send({"update_id": self.fake._next_id(),
                          "callback_query": {"id": f"cb{self.fake._next_id()}",
                                             "from": {"id": OWNER, "is_bot": False, "first_name": "Me"},
                                             "message": {"message_id": message_id, "date": DATE,
                                                         "chat": {"id": OWNER, "type": "private", "first_name": "Me"},
                                                         "text": "panel"},
                                             "chat_instance": "ci", "data": data}})

    # helpers to read what the bot did
    def last_text(self):
        for m in reversed(self.fake.messages):
            if m["method"] in ("sendMessage", "editMessageText"):
                return m["params"].get("text") or ""
        return ""

    def last_buttons(self):
        for m in reversed(self.fake.messages):
            if m["method"] in ("sendMessage", "editMessageText"):
                return m["params"].get("reply_markup")
        return None

    def sent_texts(self):
        return [m["params"].get("text") or "" for m in self.fake.messages
                if m["method"] in ("sendMessage", "editMessageText")]

    def button_texts(self):
        out = []
        for m in self.fake.messages:
            if m["method"] in ("sendMessage", "editMessageText"):
                rm = m["params"].get("reply_markup") or {}
                if isinstance(rm, str):
                    rm = json.loads(rm)
                for row in rm.get("inline_keyboard", []):
                    for b in row:
                        out.append(b.get("text"))
        return out

    def has_button(self, label):
        return any(label in (t or "") for t in self.button_texts())

    def last_document(self):
        for m in reversed(self.fake.messages):
            if m["method"] == "sendDocument":
                doc = m.get("document")
                if isinstance(doc, (tuple, list)):
                    return doc[1]
                return doc
        return None


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(HTTPXRequest, "do_request", fake.do_request)
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    h = Harness(fake, tmp_path)
    yield h
    run(h.stop())
