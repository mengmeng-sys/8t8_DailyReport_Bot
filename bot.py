# bot.py - Telegram bot with ONE screen that updates (no long chat of old menus)
#   Home -> Start -> fill the report -> Finish (file is sent) -> History (open / update / delete)
import asyncio, copy, logging, os, re, tempfile
from datetime import datetime, time
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, filters)
from telegram.request import HTTPXRequest

import history
from fill import fill_template, summarize
from reader import parse as parse_rules, _split as split_item

load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])  # only you can use the bot
TZ = ZoneInfo("Asia/Phnom_Penh")
PAGE = 8  # reports per page in History


def reminder_time():
    """Daily reminder time, Cambodia time. Default 21:30. Change it with REMINDER_TIME=HH:MM (24-hour)."""
    raw = os.getenv("REMINDER_TIME", "21:30")
    try:
        hour, minute = (int(x) for x in raw.split(":"))
        return time(hour, minute, tzinfo=TZ)
    except ValueError:
        logging.warning("REMINDER_TIME %r is not HH:MM, using 21:30", raw)
        return time(21, 30, tzinfo=TZ)

LABELS = {
    "rooms": {"pool": "Pool view", "city": "City view", "double_twin": "Double/Twin",
              "pod": "Pod", "dorm4": "4-bed dorm", "dorm6": "6-bed dorm"},
    "tours": {"killing_field": "Killing Field", "bus": "Bus transport", "scooter": "Scooter hire",
              "airport": "Airport pick up", "sunset_boat": "Sunset boat", "night_city": "Night city tour"},
}
TITLES = {"rooms": "🛏 Rooms", "tours": "🧳 Tours"}


# ---------- small helpers ----------
def today_iso():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def dmy(iso):                      # 2026-10-26 -> 26/10/2026
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def file_name(iso):                # 2026-10-26 -> 26-10-2026_Report.xlsx
    y, m, d = iso.split("-")
    return f"{d}-{m}-{y}_Report.xlsx"


def fresh():
    return {"rooms": {}, "tours": {}, "bar": [], "discount": {}, "expenses": []}


def complete(data):
    base = fresh()
    base.update(data or {})
    return base


def three(nums):
    return (list(nums) + [0, 0, 0])[:3]


def numbers(text):
    out = []
    for t in text.replace(",", " ").split():
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def fmt(v):
    c, k, a = three(v)
    return f"cash {c:g} | card {k:g} | aba {a:g}"


def short(v):
    c, k, a = three(v)
    return f"{c:g} | {k:g} | {a:g}"


def filled(data, group):
    return sum(1 for v in data[group].values() if sum(three(v)))


# ---------- the report you are working on ("session") ----------
def start_session(ctx, iso, explicit=False):
    saved = history.load_report(iso)
    ctx.user_data["session"] = {
        "date": iso, "explicit": explicit, "saved": saved is not None,
        "data": complete(copy.deepcopy(saved)) if saved else fresh(),
    }
    ctx.user_data.pop("waiting", None)
    return ctx.user_data["session"]


def get_session(ctx):
    s = ctx.user_data.get("session")
    # a normal session from an earlier day starts again for today
    if s is None or (not s["explicit"] and s["date"] != today_iso()):
        s = start_session(ctx, today_iso())
    return s


# ---------- screens: each returns (text, buttons) ----------
def home_screen():
    saved = history.load_report(today_iso()) is not None
    text = ("🏨 Hostel report\n\n"
            + ("Today's report is already saved.\n" if saved else "Today's report is not made yet.\n")
            + "What do you want to do?")
    start = "✏️ Update today's report" if saved else "▶️ Start today's report"
    return text, Markup([
        [Btn(start, callback_data="new")],
        [Btn("📚 History", callback_data="hist:0"), Btn("📅 Other date", callback_data="newdate")],
    ])


def report_screen(s, note=""):
    data, sm, d = s["data"], summarize(s["data"]), s["data"]["discount"]
    head = "✏️ Updating report" if s.get("saved") else "📝 New report"
    lines = ([note, ""] if note else []) + [
        f"{head} · {dmy(s['date'])}", "",
        f"🛏 Rooms: {filled(data, 'rooms')} of 6 filled",
        f"🧳 Tours: {filled(data, 'tours')} of 6 filled",
        f"🍽 Bar: {sm['bar']:g}" if sm["bar"] else "🍽 Bar: -",
        f"🏷 Discount: {d['amount']:g}" if d.get("amount") else "🏷 Discount: -",
        f"💸 Expenses: {len(data['expenses'])} item(s) · {sm['expenses']:g}" if data["expenses"] else "💸 Expenses: -",
        "", f"💰 Revenue so far: {sm['total']:g}", "",
        "Tap an item to fill it. When you are done, tap ✅ Finish.",
    ]
    return "\n".join(lines), Markup([
        [Btn("🛏 Rooms", callback_data="m:rooms"), Btn("🧳 Tours", callback_data="m:tours")],
        [Btn("🍽 Bar", callback_data="in:bar"), Btn("🏷 Discount", callback_data="in:discount")],
        [Btn("💸 Expenses", callback_data="in:expense")],
        [Btn("✅ Finish - send me the xlsx file", callback_data="act:generate")],
        [Btn("🏠 Home", callback_data="home"), Btn("🗑 Clear all", callback_data="act:reset")],
    ])


def group_screen(s, group, note=""):
    data = s["data"]
    text = (note + "\n\n" if note else "") + f"{TITLES[group]}\nTap one, then type its numbers."
    rows = []
    for key, label in LABELS[group].items():
        vals = data[group].get(key)
        rows.append([Btn(f"✅ {label}   {short(vals)}" if vals and sum(three(vals)) else f"▫️ {label}",
                         callback_data=f"in:{group}:{key}")])
    rows.append([Btn("⬅️ Back to report", callback_data="m:main")])
    return text, Markup(rows)


def prompt_screen(title, example, back, note="", extra=""):
    text = (note + "\n\n" if note else "") + f"{title}\n\n{extra}Type it now.\nExample: {example}"
    return text, Markup([[Btn("⬅️ Back", callback_data=back)]])


def item_prompt(group, key, note=""):
    return prompt_screen(LABELS[group][key], "40 0 20", f"m:{group}", note,
                         "3 numbers: cash card aba\n(one number = cash only, 0 = clear)\n")


def bar_prompt(note=""):
    return prompt_screen("🍽 Bar / Restaurant", "50 10 5", "m:main", note, "3 numbers: cash card aba\n")


def discount_prompt(note=""):
    return prompt_screen("🏷 Discount", "5 long stay", "m:main", note, "The amount, then the reason\n")


def expense_screen(s, note=""):
    items = s["data"]["expenses"]
    lines = ([note, ""] if note else []) + ["💸 Expenses", ""]
    lines += [f"{i}. {e['name']}: {e['amount']:g}" for i, e in enumerate(items, 1)] or ["(none yet)"]
    lines += ["", f"Total: {sum(e['amount'] for e in items):g}", "",
              "Type the next one: name then amount\nExample: water 12"]
    row = ([Btn("↩️ Undo last", callback_data="exp:undo")] if items else []) + [Btn("✅ Done", callback_data="m:main")]
    return "\n".join(lines), Markup([row])


def done_screen(s):
    sm = summarize(s["data"])
    return (f"✅ Report {dmy(s['date'])} saved\nRevenue {sm['total']:g} · Expenses {sm['expenses']:g}\n\n"
            "Your xlsx file is above ☝️\nIt leaves the chat when you start the next task.\nYou can get it again any time from 📚 History.",
            Markup([[Btn("✏️ Change something", callback_data="m:main")],
                    [Btn("📚 History", callback_data="hist:0"), Btn("🏠 Home", callback_data="home")]]))


def preview_text(data, iso):
    lines = [f"📋 Report {dmy(iso)}"]
    for group in ("rooms", "tours"):
        items = [f"• {LABELS[group][k]}: {fmt(v)}" for k, v in data[group].items() if sum(three(v))]
        if items:
            lines += ["", TITLES[group]] + items
    if sum(three(data["bar"])):
        lines += ["", "🍽 Bar", "• " + fmt(data["bar"])]
    if data["discount"].get("amount"):
        d = data["discount"]
        lines += ["", f"🏷 Discount: {d['amount']:g} {d.get('reason', '')}".strip()]
    if data["expenses"]:
        lines += ["", "💸 Expenses"] + [f"• {e['name']}: {e['amount']:g}" for e in data["expenses"]]
    if len(lines) == 1:
        lines.append("\nNothing filled yet.")
    s = summarize(data)
    lines += ["", f"Rooms {s['rooms']:g} | Tours {s['tours']:g} | Bar {s['bar']:g}",
              f"TOTAL revenue: {s['total']:g}", f"Expenses: {s['expenses']:g}"]
    return "\n".join(lines)


def history_screen(page, note=""):
    items = history.list_reports()
    if not items:
        return (note + "📚 History\n\nNo reports yet.\nFill a report and tap Finish, and it will appear here.",
                Markup([[Btn("▶️ Start today's report", callback_data="new"), Btn("🏠 Home", callback_data="home")]]))
    pages = (len(items) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = []
    for iso in items[page * PAGE:(page + 1) * PAGE]:
        data = history.load_report(iso)
        total = summarize(complete(data))["total"] if data else 0
        rows.append([Btn(f"🗓 {dmy(iso)}  ·  revenue {total:g}", callback_data=f"h:v:{iso}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Newer", callback_data=f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("Older ➡️", callback_data=f"hist:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("🏠 Home", callback_data="home")])
    noun = "report" if len(items) == 1 else "reports"
    return f"{note}📚 History ({len(items)} {noun})\nTap one to open it.", Markup(rows)


def report_view_markup(iso):
    return Markup([
        [Btn("📥 Send file", callback_data=f"h:f:{iso}"), Btn("✏️ Update", callback_data=f"h:e:{iso}")],
        [Btn("🗑 Delete", callback_data=f"h:d:{iso}")],
        [Btn("⬅️ History", callback_data="hist:0"), Btn("🏠 Home", callback_data="home")],
    ])


# ---------- sending screens (one panel message that is edited) ----------
async def show(q, screen):
    """Button tap: edit the message that was tapped."""
    try:
        await q.edit_message_text(screen[0], reply_markup=screen[1])
    except BadRequest:  # "message is not modified"
        pass


async def refresh(ctx, screen):
    """Typed message: edit the panel; if it is gone, send a new one."""
    mid = ctx.user_data.get("panel")
    if mid:
        try:
            await ctx.bot.edit_message_text(screen[0], chat_id=OWNER, message_id=mid, reply_markup=screen[1])
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    msg = await ctx.bot.send_message(OWNER, screen[0], reply_markup=screen[1])
    ctx.user_data["panel"] = msg.message_id


async def tidy(update):
    """Delete what you typed, so the chat stays clean."""
    try:
        await update.message.delete()
    except Exception:
        pass


async def respond(update, ctx, screen):
    await asyncio.gather(refresh(ctx, screen), tidy(update))


_TASKS = set()


def background(coro):
    """Run something at the same time, without making you wait for it."""
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)


def answer_later(q, text=None):
    """Answer the button tap at the same time as editing the screen (faster)."""
    async def run():
        try:
            await q.answer(text)
        except Exception:
            pass
    background(run())


async def delete_messages(ctx, ids):
    for mid in ids:
        try:
            await ctx.bot.delete_message(OWNER, mid)
        except Exception:      # already deleted, or too old for Telegram to allow
            pass


def clear_files_later(ctx):
    """Take the xlsx files the bot sent out of the chat (they stay in History)."""
    ids = ctx.user_data.pop("files", [])
    if ids:
        background(delete_messages(ctx, ids))


async def send_report(ctx, data, iso):
    """Make the xlsx from your template and send ONLY the file. Returns the message id."""
    path = os.path.join(tempfile.gettempdir(), file_name(iso))
    try:
        fill_template(data, path, report_date=datetime.strptime(iso, "%Y-%m-%d").date())
        with open(path, "rb") as f:
            msg = await ctx.bot.send_document(OWNER, f, filename=file_name(iso))
        return msg.message_id
    finally:
        if os.path.exists(path):
            os.remove(path)


async def deliver_file(ctx, data, iso, screen, old_panel_id):
    """Old files out -> new file in -> a fresh menu UNDER the file, so the menu you use is always last."""
    clear_files_later(ctx)
    file_id = await send_report(ctx, data, iso)
    ctx.user_data.setdefault("files", []).append(file_id)
    msg = await ctx.bot.send_message(OWNER, screen[0], reply_markup=screen[1])
    ctx.user_data["panel"] = msg.message_id
    if old_panel_id:
        background(delete_messages(ctx, [old_panel_id]))


# ---------- commands ----------
async def home_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("waiting", None)
    old = ctx.user_data.get("panel")
    background(delete_messages(ctx, ctx.user_data.pop("files", []) + ([old] if old else [])))
    text, markup = home_screen()
    msg, _ = await asyncio.gather(update.message.reply_text(text, reply_markup=markup), tidy(update))
    ctx.user_data["panel"] = msg.message_id


# ---------- button taps ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t0 = perf_counter()
    try:
        await _on_button(update, ctx)
    finally:
        logging.info("button %s handled in %.2fs", update.callback_query.data, perf_counter() - t0)


async def _on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.id != OWNER:
        return
    cmd = q.data
    current = ctx.user_data.get("panel")
    if current and q.message.message_id != current:        # an old menu higher up in the chat
        answer_later(q, "Old menu removed. Please use the last message.")
        return background(delete_messages(ctx, [q.message.message_id]))
    ctx.user_data["panel"] = q.message.message_id
    if cmd != "act:generate":
        answer_later(q, "Sending file..." if cmd.startswith("h:f:") else None)
    if cmd != "act:generate" and not cmd.startswith("h:f:"):
        clear_files_later(ctx)                              # new task: the old file leaves the chat

    # ----- home and history -----
    if cmd == "home":
        ctx.user_data.pop("waiting", None)
        return await show(q, home_screen())
    if cmd == "new":
        return await show(q, report_screen(start_session(ctx, today_iso())))
    if cmd == "newdate":
        ctx.user_data["waiting"] = ("newdate",)
        return await show(q, prompt_screen("📅 Which day is the report for?", "25/10/2026", "home",
                                           extra="Date as day/month/year\n"))
    if cmd.startswith("hist:"):
        ctx.user_data.pop("waiting", None)
        return await show(q, history_screen(int(cmd.split(":")[1])))
    if cmd.startswith("h:"):
        _, action, iso = cmd.split(":", 2)
        data = history.load_report(iso)
        if action != "dy" and data is None:
            return await show(q, history_screen(0, "That report was not found.\n\n"))
        if action == "v":                                   # open one report
            return await show(q, (preview_text(complete(data), iso), report_view_markup(iso)))
        if action == "f":                                   # send its file again
            return await deliver_file(ctx, complete(data), iso,
                                      (preview_text(complete(data), iso), report_view_markup(iso)),
                                      q.message.message_id)
        if action == "e":                                   # update (edit) it
            return await show(q, report_screen(start_session(ctx, iso, explicit=True)))
        if action == "d":                                   # ask before deleting
            return await show(q, (f"🗑 Delete the report of {dmy(iso)}?\nThis cannot be undone.",
                                  Markup([[Btn("Yes, delete", callback_data=f"h:dy:{iso}"),
                                           Btn("Cancel", callback_data=f"h:v:{iso}")]])))
        if action == "dy":                                  # delete it
            history.delete_report(iso)
            return await show(q, history_screen(0, f"🗑 Deleted {dmy(iso)}\n\n"))

    # ----- filling the report -----
    s = get_session(ctx)
    data = s["data"]

    if cmd == "m:main":
        ctx.user_data.pop("waiting", None)
        await show(q, report_screen(s))
    elif cmd in ("m:rooms", "m:tours"):
        ctx.user_data.pop("waiting", None)
        await show(q, group_screen(s, cmd.split(":")[1]))
    elif cmd.startswith("in:rooms:") or cmd.startswith("in:tours:"):
        _, group, key = cmd.split(":")
        ctx.user_data["waiting"] = ("item", group, key)
        await show(q, item_prompt(group, key))
    elif cmd == "in:bar":
        ctx.user_data["waiting"] = ("bar",)
        await show(q, bar_prompt())
    elif cmd == "in:discount":
        ctx.user_data["waiting"] = ("discount",)
        await show(q, discount_prompt())
    elif cmd == "in:expense":
        ctx.user_data["waiting"] = ("expense",)
        await show(q, expense_screen(s))
    elif cmd == "exp:undo":
        note = ""
        if data["expenses"]:
            gone = data["expenses"].pop()
            note = f"↩️ Removed {gone['name']}: {gone['amount']:g}"
        ctx.user_data["waiting"] = ("expense",)
        await show(q, expense_screen(s, note))
    elif cmd == "act:reset":
        await show(q, ("🗑 Clear all numbers of this report?\n(History is not changed.)",
                       Markup([[Btn("Yes, clear", callback_data="act:resety"),
                                Btn("No", callback_data="m:main")]])))
    elif cmd == "act:resety":
        s["data"] = fresh()
        ctx.user_data.pop("waiting", None)
        await show(q, report_screen(s, "🗑 Cleared"))
    elif cmd == "act:generate":
        try:
            history.save_report(s["date"], data)            # saved in History (replaces the same day)
            s["saved"] = True
            await deliver_file(ctx, data, s["date"], done_screen(s), q.message.message_id)
            await q.answer("✅ Saved to history")
        except Exception:
            logging.exception("could not make the report")
            await q.answer("Sorry, could not make the file", show_alert=True)


# ---------- typed messages ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    w = ctx.user_data.get("waiting")
    clear_files_later(ctx)

    if w and w[0] == "newdate":
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", text)
        try:
            day = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except (AttributeError, ValueError):
            return await respond(update, ctx, prompt_screen(
                "📅 Which day is the report for?", "25/10/2026", "home",
                "⚠️ I cannot read that date.", "Date as day/month/year\n"))
        return await respond(update, ctx, report_screen(start_session(ctx, day.strftime("%Y-%m-%d"), explicit=True)))

    s = get_session(ctx)
    data = s["data"]

    if w is None:  # no item chosen: still understands the one-message format
        parsed, unknown = parse_rules(text)
        for g in ("rooms", "tours"):
            data[g].update(parsed[g])
        if parsed["bar"]:
            data["bar"] = parsed["bar"]
        if parsed["discount"]:
            data["discount"] = parsed["discount"]
        data["expenses"] += parsed["expenses"]
        got = parsed["rooms"] or parsed["tours"] or parsed["bar"] or parsed["discount"] or parsed["expenses"]
        note = "✅ Saved from your message" if got else "⚠️ Tap a button below to choose what to fill."
        if got and unknown:
            note += "\nNot understood: " + "; ".join(unknown)
        await respond(update, ctx, report_screen(s, note))

    elif w[0] == "item":
        _, group, key = w
        nums = numbers(text)
        if not nums:
            return await respond(update, ctx, item_prompt(group, key, "⚠️ Please type numbers."))
        data[group][key] = three(nums)
        ctx.user_data.pop("waiting")
        await respond(update, ctx, group_screen(s, group, f"✅ {LABELS[group][key]} saved: {fmt(nums)}"))

    elif w[0] == "bar":
        nums = numbers(text)
        if not nums:
            return await respond(update, ctx, bar_prompt("⚠️ Please type numbers."))
        data["bar"] = three(nums)
        ctx.user_data.pop("waiting")
        await respond(update, ctx, report_screen(s, f"✅ Bar saved: {fmt(nums)}"))

    elif w[0] == "discount":
        _, nums, rest = split_item(text)
        if not nums:
            return await respond(update, ctx, discount_prompt("⚠️ Please type the amount first."))
        data["discount"] = {"amount": nums[0], "reason": " ".join(rest)}
        ctx.user_data.pop("waiting")
        await respond(update, ctx, report_screen(s, f"✅ Discount saved: {nums[0]:g} {' '.join(rest)}".strip()))

    elif w[0] == "expense":
        words, nums, _ = split_item(text)
        if not words or not nums:
            return await respond(update, ctx, expense_screen(s, "⚠️ Type the name, then the amount. Example: water 12"))
        data["expenses"].append({"name": " ".join(words), "amount": nums[0]})
        await respond(update, ctx, expense_screen(s, f"✅ Added {' '.join(words)}: {nums[0]:g}"))


# ---------- evening reminder ----------
async def reminder(ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data if ctx.user_data is not None else {}
    if history.load_report(today_iso()) is not None:      # today's report is already made: no need to bother you
        return
    old = ud.get("panel")
    background(delete_messages(ctx, ud.pop("files", []) + ([old] if old else [])))
    text, markup = home_screen()
    at = reminder_time().strftime("%I:%M %p").lstrip("0")
    msg = await ctx.bot.send_message(OWNER, f"⏰ It is {at} - time to make today's hostel report.\n\n" + text,
                                     reply_markup=markup)
    ud["panel"] = msg.message_id


def make_request():
    """Telegram connection. IPv4 only by default: a broken IPv6 route can make every click slow.
    (To turn this off, write FORCE_IPV4=0 in .env)"""
    kwargs = {}
    if os.getenv("FORCE_IPV4", "1") == "1":
        kwargs["httpx_kwargs"] = {"transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0")}
    return HTTPXRequest(connection_pool_size=16, connect_timeout=10, read_timeout=15,
                        write_timeout=15, **kwargs)


def start_health_server():
    """Only used when the bot runs as a Render *Web Service* (Render sets PORT).
    It answers "ok" to web visits, so an uptime pinger can keep the free service awake."""
    port = os.getenv("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    class Health(BaseHTTPRequestHandler):
        def _ok(self, body=b""):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._ok(b"ok")

        def do_HEAD(self):
            self._ok()

        def log_message(self, *args):  # keep the logs clean
            pass

    Thread(target=HTTPServer(("0.0.0.0", int(port)), Health).serve_forever, daemon=True).start()
    print(f"Health page on port {port}")


def main():
    start_health_server()
    app = (Application.builder().token(TOKEN)
           .request(make_request()).get_updates_request(make_request())
           .persistence(PicklePersistence(filepath=str(history.DATA_DIR / "bot_data.pickle")))
           .concurrent_updates(True)   # a slow click does not make the next click wait
           .build())
    only_me = filters.Chat(chat_id=OWNER)
    app.add_handler(CommandHandler(["start", "menu"], home_cmd, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_daily(reminder, time=reminder_time(), user_id=OWNER, chat_id=OWNER)  # 20:00 Cambodia time
    print(f"Bot is running. Daily reminder at {reminder_time():%H:%M} (Cambodia time)")
    app.run_polling()


if __name__ == "__main__":
    main()