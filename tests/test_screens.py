# Every screen must be valid Telegram HTML and every callback_data <= 64 bytes.
import datetime
import re

import pytest

import bot
import fill
import importer


class Ctx:
    def __init__(self):
        self.user_data = {}


def data():
    return {"rooms": {"pool": [40, 0, 20]}, "tours": {}, "bar": [50, 10, 5],
            "discount": {"amount": 7, "reason": "long stay"},
            "expenses": [{"name": "water", "amount": 12}], "cash_check": {"counts": {"usd": {"20": 1}}}}


def all_screens():
    s = {"date": "2026-10-20", "explicit": True, "saved": False, "data": bot.complete(data())}
    ctx = Ctx()
    ctx.user_data["waiting"] = ("item", "rooms", "pool")
    ctx.user_data["pad"] = bot.init_pad(s, ("item", "rooms", "pool"))
    ctx.user_data["cash_ui"] = {"step": 1, "from": "main"}
    ctx.user_data["import"] = {"date": "2026-10-20", "data": bot.complete(data()), "warnings": []}
    screens = [
        bot.home_screen(),
        bot.report_screen(s), bot.report_screen(s, "note"),
        bot.group_screen(s, "rooms"), bot.group_screen(s, "tours"),
        bot.prompt_screen("t", "e", "home"), bot.discount_prompt(),
        bot.expense_screen(s), bot.expense_list_screen(s), bot.expense_item_screen(s, 0),
        bot.step_screen(s, 0), bot.step_screen(s, 13), bot.step_screen(s, 14),
        bot.pad_screen(ctx, s), bot.review_screen(s), bot.done_screen(s),
        bot.warn_screen("x", [1, 2, 3], 9), bot.history_screen(0),
        bot.report_view(data(), "2026-10-20"), bot.cash_screen(ctx, s),
        bot.storage_screen(0), bot.import_screen(ctx),
        bot.month_screen("2026-10"), bot.month_screen("2026-10", "note"),
    ]
    return screens


VOID = {"br", "hr", "img", "meta", "link"}


def check_html(text):
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>", text):
        closing, tag, rest = m.group(1), m.group(2).lower(), m.group(3)
        if tag in VOID:
            continue
        if closing:
            assert stack and stack[-1] == tag, f"unbalanced </{tag}> in {text!r}"
            stack.pop()
        elif rest.endswith("/"):
            continue
        else:
            stack.append(tag)
    assert not stack, f"unclosed {stack} in {text!r}"


def test_all_screens_valid_html():
    for screen in all_screens():
        check_html(screen[0])


def test_all_callback_data_short():
    for screen in all_screens():
        for row in screen[1].inline_keyboard:
            for b in row:
                assert b.callback_data is None or len(b.callback_data.encode()) <= 64, b.callback_data


def test_round_trip_various(tmp_path):
    cases = [
        {"rooms": {"pool": [1, 2, 3]}, "tours": {}, "bar": [], "discount": {}, "expenses": [], "cash_check": {}},
        {"rooms": {}, "tours": {"bus": [5, 0, 0], "night_city": [1, 1, 1]}, "bar": [0, 0, 9],
         "discount": {"amount": 2, "reason": ""}, "expenses": [{"name": "x", "amount": 0.5}], "cash_check": {}},
        {"rooms": {"dorm6": [10, 10, 10]}, "tours": {}, "bar": [1, 1, 1],
         "discount": {"amount": 3, "reason": "long stay"},
         "expenses": [{"name": "a", "amount": 1}, {"name": "b", "amount": 2.25}], "cash_check": {}},
    ]
    for d in cases:
        path = tmp_path / "rt.xlsx"
        fill.fill_template(d, str(path), report_date=datetime.date(2026, 10, 20))
        date, got, warnings = importer.read_report(str(path), "20-10-2026_Report.xlsx")
        assert date == "2026-10-20", warnings
        assert got["rooms"] == d["rooms"], (got["rooms"], d["rooms"])
        assert got["tours"] == d["tours"], (got["tours"], d["tours"])
        assert got["bar"] == d["bar"], (got["bar"], d["bar"])
        assert got["discount"] == d["discount"], (got["discount"], d["discount"])
        assert got["expenses"] == d["expenses"], (got["expenses"], d["expenses"])
