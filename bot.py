# bot.py - Telegram bot: clean aligned screens, step-by-step entry, review before sending,
# always-visible shortcuts, History (open / update / delete), chat cleans itself.
import asyncio, copy, html, logging, os, re, tempfile
from datetime import datetime, time
from time import perf_counter
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults,
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
SHORT = {   # short names for the aligned table
    "rooms": {"pool": "Pool view", "city": "City view", "double_twin": "Double/Twin",
              "pod": "Pod", "dorm4": "4-bed dorm", "dorm6": "6-bed dorm"},
    "tours": {"killing_field": "Killing Field", "bus": "Bus", "scooter": "Scooter",
              "airport": "Airport", "sunset_boat": "Sunset boat", "night_city": "Night city"},
}
TITLES = {"rooms": "🛏 Rooms", "tours": "🧳 Tours"}

# the step-by-step order
STEPS = ([("item", "rooms", k) for k in LABELS["rooms"]] + [("item", "tours", k) for k in LABELS["tours"]]
         + [("bar",), ("discount",), ("expense",)])

SHORTCUTS = ReplyKeyboardMarkup([[KeyboardButton("📝 Today"), KeyboardButton("📚 History")]],
                                resize_keyboard=True, is_persistent=True,
                                input_field_placeholder="Tap a button, or type numbers")
SHORTCUT_TEXTS = ("📝 Today", "📚 History")


# ---------- small helpers ----------
esc = html.escape


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


def n(v):
    v = float(v)
    return f"{int(v):,}" if v == int(v) else f"{v:,.2f}"


def fmt(v):
    c, k, a = three(v)
    return f"cash {n(c)} | card {n(k)} | aba {n(a)}"


def short(v):
    c, k, a = three(v)
    return f"{n(c)} | {n(k)} | {n(a)}"


def filled(data, group):
    return sum(1 for v in data[group].values() if sum(three(v)))


def dots(k, total=6):
    return "●" * k + "○" * (total - k)


def method_totals(data):
    t = [0, 0, 0]
    for group in ("rooms", "tours"):
        for v in data[group].values():
            for i, x in enumerate(three(v)):
                t[i] += x
    for i, x in enumerate(three(data["bar"])):
        t[i] += x
    return t


# ---------- the report you are working on ("session") ----------
def start_session(ctx, iso, explicit=False):
    saved = history.load_report(iso)
    ctx.user_data["session"] = {
        "date": iso, "explicit": explicit, "saved": saved is not None,
        "data": complete(copy.deepcopy(saved)) if saved else fresh(),
    }
    for k in ("waiting", "guided", "pending"):
        ctx.user_data.pop(k, None)
    return ctx.user_data["session"]


def get_session(ctx):
    s = ctx.user_data.get("session")
    # a normal session from an earlier day starts again for today
    if s is None or (not s["explicit"] and s["date"] != today_iso()):
        s = start_session(ctx, today_iso())
    return s


# ---------- the aligned table ----------
W = 13


def trow(label, v):
    c, k, a = three(v)
    return f"{label:<{W}}{n(c):>7}{n(k):>7}{n(a):>7}"


def table_block(data):
    lines = [f"{'':<{W}}{'CASH':>7}{'CARD':>7}{'ABA':>7}"]
    for group, title in (("rooms", "ROOMS"), ("tours", "TOURS")):
        rows = [trow(SHORT[group][k], data[group][k]) for k in LABELS[group]
                if data[group].get(k) and sum(three(data[group][k]))]
        if rows:
            lines += [title] + rows
    if sum(three(data["bar"])):
        lines += ["BAR", trow("Bar", data["bar"])]
    lines += ["-" * (W + 21), trow("TOTAL", method_totals(data))]
    return "\n".join(lines)


def report_body(data, iso, title):
    sm, d = summarize(data), data["discount"]
    empty = not any(sum(three(v)) for g in ("rooms", "tours") for v in data[g].values()) \
        and not sum(three(data["bar"]))
    parts = [f"<b>{title}</b> · {dmy(iso)}", ""]
    if empty:
        parts += ["<i>Nothing filled yet.</i>"]
    else:
        parts += [f"<pre>{esc(table_block(data))}</pre>", f"💰 <b>Revenue {n(sm['total'])}</b>"]
    if d.get("amount"):
        parts.append(f"🏷 Discount {n(d['amount'])}" + (f" · {esc(d['reason'])}" if d.get("reason") else ""))
    if data["expenses"]:
        items = "\n".join(f"{e['name']}: {n(e['amount'])}" for e in data["expenses"])
        parts += [f"💸 Expenses {n(sm['expenses'])}", f"<pre>{esc(items)}</pre>"]
    return "\n".join(parts)


# ---------- screens: each returns (text, buttons) ----------
def home_screen():
    saved = history.load_report(today_iso()) is not None
    text = ("<b>🏨 Hostel report</b>\n\n"
            + ("Today's report is already saved ✅\n" if saved else "Today's report is not made yet.\n")
            + "What do you want to do?")
    start = "✏️ Update today's report" if saved else "▶️ Start today's report"
    return text, Markup([
        [Btn(start, callback_data="new")],
        [Btn("📚 History", callback_data="hist:0"), Btn("📅 Other date", callback_data="newdate")],
    ])


def report_screen(s, note=""):
    data, sm, d = s["data"], summarize(s["data"]), s["data"]["discount"]
    mode = "Updating saved report" if s.get("saved") else "New report"
    rooms, tours = filled(data, "rooms"), filled(data, "tours")
    rows = [
        f"{'Rooms':<10}{dots(rooms)}  {rooms}/6",
        f"{'Tours':<10}{dots(tours)}  {tours}/6",
        f"{'Bar':<10}{n(sm['bar'])}" if sm["bar"] else f"{'Bar':<10}-",
        f"{'Discount':<10}{n(d['amount'])}" if d.get("amount") else f"{'Discount':<10}-",
        f"{'Expenses':<10}{len(data['expenses'])} item(s) · {n(sm['expenses'])}" if data["expenses"]
        else f"{'Expenses':<10}-",
    ]
    cash, card, aba = method_totals(data)
    text = ((esc(note) + "\n\n") if note else "") + (
        f"<b>🏨 HOSTEL REPORT</b> · {dmy(s['date'])}\n<i>{mode}</i>\n\n"
        f"<pre>{esc(chr(10).join(rows))}</pre>"
        f"<pre>{'':<10}{'CASH':>7}{'CARD':>7}{'ABA':>7}\n{'Total':<10}{n(cash):>7}{n(card):>7}{n(aba):>7}</pre>"
        f"💰 <b>Revenue {n(sm['total'])}</b>")
    return text, Markup([
        [Btn("▶️ Fill step by step", callback_data="g:start")],
        [Btn("🛏 Rooms", callback_data="m:rooms"), Btn("🧳 Tours", callback_data="m:tours"),
         Btn("🍽 Bar", callback_data="in:bar")],
        [Btn("🏷 Discount", callback_data="in:discount"), Btn("💸 Expenses", callback_data="in:expense")],
        [Btn("📋 Review & finish", callback_data="review")],
        [Btn("🏠 Home", callback_data="home"), Btn("🗑 Clear all", callback_data="act:reset")],
    ])


def group_screen(s, group, note=""):
    data = s["data"]
    text = ((esc(note) + "\n\n") if note else "") + f"<b>{TITLES[group]}</b>\nTap one, then type its numbers."
    rows = []
    for key, label in LABELS[group].items():
        vals = data[group].get(key)
        rows.append([Btn(f"✅ {label}   {short(vals)}" if vals and sum(three(vals)) else f"▫️ {label}",
                         callback_data=f"in:{group}:{key}")])
    rows.append([Btn("⬅️ Back to report", callback_data="m:main")])
    return text, Markup(rows)


# Common cash amounts for quick buttons (cash | 0 | 0)
QUICK_AMOUNTS = [0, 10, 20, 30, 40, 50, 60, 80, 100, 150]


def _quick_rows(prefix: str):
    """Build rows of quick-amount buttons. prefix example: 'q:rooms:pool' or 'q:bar'"""
    buttons = [Btn(str(a) if a else "Clear", callback_data=f"{prefix}:{a}") for a in QUICK_AMOUNTS]
    # 5 buttons per row
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    return rows


def prompt_screen(title, example, back, note="", extra="", quick_prefix=None):
    text = ((esc(note) + "\n\n") if note else "") + f"<b>{title}</b>\n\n{extra}Type it now.\nExample: <code>{example}</code>"
    rows = []
    if quick_prefix:
        rows += _quick_rows(quick_prefix)
        text += "\n\nOr tap a quick amount (cash only):"
    rows.append([Btn("⬅️ Back", callback_data=back)])
    return text, Markup(rows)


def item_prompt(group, key, note=""):
    return prompt_screen(
        LABELS[group][key], "40 0 20", f"m:{group}", note,
        "3 numbers: cash card aba\n(one number = cash only, 0 = clear)\n",
        quick_prefix=f"q:{group}:{key}",
    )


def bar_prompt(note=""):
    return prompt_screen(
        "🍽 Bar / Restaurant", "50 10 5", "m:main", note,
        "3 numbers: cash card aba\n",
        quick_prefix="q:bar",
    )


def discount_prompt(note=""):
    return prompt_screen("🏷 Discount", "5 long stay", "m:main", note, "The amount, then the reason\n")


def expense_lines(s):
    items = s["data"]["expenses"]
    lines = [f"{i}. {esc(e['name'])}: {n(e['amount'])}" for i, e in enumerate(items, 1)] or ["<i>(none yet)</i>"]
    return lines + ["", f"Total: <b>{n(sum(e['amount'] for e in items))}</b>"]


def expense_screen(s, note=""):
    items = s["data"]["expenses"]
    lines = ([esc(note), ""] if note else []) + ["<b>💸 Expenses</b>", ""] + expense_lines(s) + [
        "", "Type the next one: name then amount", "Example: <code>water 12</code>"]
    row = ([Btn("↩️ Undo last", callback_data="exp:undo")] if items else []) + [Btn("✅ Done", callback_data="m:main")]
    return "\n".join(lines), Markup([row])


def step_screen(s, idx, note=""):
    step, data = STEPS[idx], s["data"]
    lines = ([esc(note), ""] if note else []) + [f"<b>STEP {idx + 1} of {len(STEPS)}</b> · {dmy(s['date'])}", ""]
    quick_prefix = None
    if step[0] == "item":
        _, group, key = step
        cur = data[group].get(key)
        lines += [f"<b>{TITLES[group]} › {LABELS[group][key]}</b>", "Type 3 numbers: cash card aba",
                  "Example: <code>40 0 20</code>"]
        now = short(cur) if cur and sum(three(cur)) else None
        quick_prefix = f"q:{group}:{key}"
    elif step[0] == "bar":
        cur = data["bar"]
        lines += ["<b>🍽 Bar / Restaurant</b>", "Type 3 numbers: cash card aba", "Example: <code>50 10 5</code>"]
        now = short(cur) if sum(three(cur)) else None
        quick_prefix = "q:bar"
    elif step[0] == "discount":
        d = data["discount"]
        lines += ["<b>🏷 Discount</b> (optional)", "Type the amount, then the reason",
                  "Example: <code>5 long stay</code>"]
        now = f"{n(d['amount'])} {esc(d.get('reason', ''))}".strip() if d.get("amount") else None
    else:  # expense
        lines += ["<b>💸 Expenses</b> (optional)", ""] + expense_lines(s) + [
            "", "Type each one: name then amount", "Example: <code>water 12</code>", "Tap Done when finished."]
        now = None
    if now:
        lines += ["", f"Now: {now}"]
    if step[0] != "expense":
        lines += ["", "Nothing today? Tap Skip."]
    if quick_prefix:
        lines += ["", "Or tap a quick amount (cash only):"]

    rows = []
    if quick_prefix:
        rows += _quick_rows(quick_prefix)

    nav = []
    if step[0] == "expense":
        if data["expenses"]:
            nav.append(Btn("↩️ Undo last", callback_data="exp:undo"))
        nav.append(Btn("✅ Done", callback_data="g:next"))
    else:
        nav.append(Btn("⏭ Skip", callback_data="g:next"))
    if idx > 0:
        nav.append(Btn("⬅️ Back", callback_data="g:back"))
    rows.append(nav)
    rows.append([Btn("☰ Menu", callback_data="g:stop")])
    return "\n".join(lines), Markup(rows)


def enter_step(ctx, s, idx, note=""):
    """Go to a step of the guided entry (after the last step: the review)."""
    if idx >= len(STEPS):
        for k in ("guided", "waiting"):
            ctx.user_data.pop(k, None)
        return review_screen(s, note)
    idx = max(0, idx)
    ctx.user_data["guided"] = idx
    ctx.user_data["waiting"] = STEPS[idx]
    return step_screen(s, idx, note)


def expense_view(ctx, s, note=""):
    g = ctx.user_data.get("guided")
    return step_screen(s, g, note) if g is not None else expense_screen(s, note)


def review_screen(s, note=""):
    data = s["data"]
    empty = not any(sum(three(v)) for grp in ("rooms", "tours") for v in data[grp].values()) \
        and not sum(three(data["bar"])) and not data["expenses"]
    text = ((esc(note) + "\n\n") if note else "") + report_body(data, s["date"], "📋 REVIEW")
    text += ("\n\n⚠️ The report is empty." if empty else "") + "\n\nIs everything correct?"
    return text, Markup([
        [Btn("✅ Confirm - send me the xlsx", callback_data="act:generate")],
        [Btn("✏️ Fix something", callback_data="m:main")],
    ])


def done_screen(s):
    sm = summarize(s["data"])
    return (f"✅ <b>Report {dmy(s['date'])} saved</b>\n💰 Revenue {n(sm['total'])} · 💸 Expenses {n(sm['expenses'])}\n\n"
            "Your xlsx file is above ☝️\nIt leaves the chat when you start the next task.\n"
            "You can get it again any time from 📚 History.",
            Markup([[Btn("✏️ Change something", callback_data="m:main")],
                    [Btn("📚 History", callback_data="hist:0"), Btn("🏠 Home", callback_data="home")]]))


def warn_screen(label, nums, cap):
    return (f"⚠️ <b>Please check this number</b>\n\n{esc(label)}: <b>{esc(fmt(nums))}</b>\n"
            f"The highest in your past reports is <b>{n(cap)}</b>.\nDid you type an extra digit?",
            Markup([[Btn("✅ Yes, keep it", callback_data="w:ok"), Btn("✏️ Type again", callback_data="w:no")]]))


def history_screen(page, note=""):
    items = history.list_reports()
    if not items:
        return (esc(note) + "<b>📚 History</b>\n\nNo reports yet.\nFill a report and confirm it, and it will appear here.",
                Markup([[Btn("▶️ Start today's report", callback_data="new"), Btn("🏠 Home", callback_data="home")]]))
    pages = (len(items) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = []
    for iso in items[page * PAGE:(page + 1) * PAGE]:
        data = history.load_report(iso)
        total = summarize(complete(data))["total"] if data else 0
        rows.append([Btn(f"🗓 {dmy(iso)}  ·  revenue {n(total)}", callback_data=f"h:v:{iso}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Newer", callback_data=f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("Older ➡️", callback_data=f"hist:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("🏠 Home", callback_data="home")])
    noun = "report" if len(items) == 1 else "reports"
    return ((esc(note) + "\n\n" if note else "") + f"<b>📚 History</b> ({len(items)} {noun})\nTap one to open it."), Markup(rows)


def report_view_markup(iso):
    return Markup([
        [Btn("📥 Send file", callback_data=f"h:f:{iso}"), Btn("✏️ Update", callback_data=f"h:e:{iso}")],
        [Btn("🗑 Delete", callback_data=f"h:d:{iso}")],
        [Btn("⬅️ History", callback_data="hist:0"), Btn("🏠 Home", callback_data="home")],
    ])


def report_view(data, iso):
    return report_body(complete(data), iso, "📋 REPORT"), report_view_markup(iso)


# ---------- mistake warning: a number far bigger than your past reports ----------
def past_totals(s, kind, group=None, key=None):
    out = []
    for iso in history.list_reports()[:60]:
        if iso == s["date"]:
            continue
        d = history.load_report(iso) or {}
        v = d.get("bar") if kind == "bar" else (d.get(group) or {}).get(key)
        total = sum(three(v)) if v else 0
        if total:
            out.append(total)
    return out


def unusual(s, nums, kind, group=None, key=None):
    past = past_totals(s, kind, group, key)
    if len(past) >= 3 and sum(nums) > 3 * max(past):
        return max(past)
    return None


def commit_item(ctx, s, group, key, nums):
    s["data"][group][key] = three(nums)
    ctx.user_data.pop("pending", None)
    note = f"✅ {LABELS[group][key]} saved: {fmt(nums)}"
    g = ctx.user_data.get("guided")
    if g is not None:
        return enter_step(ctx, s, g + 1, note)
    ctx.user_data.pop("waiting", None)
    return group_screen(s, group, note)


def commit_bar(ctx, s, nums):
    s["data"]["bar"] = three(nums)
    ctx.user_data.pop("pending", None)
    note = f"✅ Bar saved: {fmt(nums)}"
    g = ctx.user_data.get("guided")
    if g is not None:
        return enter_step(ctx, s, g + 1, note)
    ctx.user_data.pop("waiting", None)
    return report_screen(s, note)


def commit_discount(ctx, s, amount, reason):
    s["data"]["discount"] = {"amount": amount, "reason": reason}
    note = f"✅ Discount saved: {n(amount)} {reason}".strip()
    g = ctx.user_data.get("guided")
    if g is not None:
        return enter_step(ctx, s, g + 1, note)
    ctx.user_data.pop("waiting", None)
    return report_screen(s, note)


# ---------- sending screens (one panel message that is edited) ----------
async def show(q, screen):
    """Button tap: edit the message that was tapped."""
    try:
        await q.edit_message_text(screen[0], reply_markup=screen[1])
    except BadRequest as e:  # "message is not modified" is fine
        if "not modified" not in str(e).lower():
            logging.exception("could not show screen")


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


# ---------- commands and shortcuts: each opens a FRESH menu at the bottom ----------
async def open_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE, screen):
    for k in ("waiting", "guided", "pending"):
        ctx.user_data.pop(k, None)
    old = ctx.user_data.get("panel")
    background(delete_messages(ctx, ctx.user_data.pop("files", []) + ([old] if old else [])))
    if not ctx.user_data.get("kb"):        # the two shortcut buttons at the bottom (sent once)
        await update.message.reply_text("⌨️ Shortcuts are now at the bottom of your screen.", reply_markup=SHORTCUTS)
        ctx.user_data["kb"] = True
    msg, _ = await asyncio.gather(update.message.reply_text(screen[0], reply_markup=screen[1]), tidy(update))
    ctx.user_data["panel"] = msg.message_id


async def home_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await open_panel(update, ctx, home_screen())


async def today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await open_panel(update, ctx, report_screen(start_session(ctx, today_iso())))


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await open_panel(update, ctx, history_screen(0))


async def keyboard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["kb"] = False            # send the shortcut buttons again
    await open_panel(update, ctx, home_screen())


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
    if not cmd.startswith(("g:", "w:", "exp:", "q:")) and cmd not in ("act:generate", "review"):
        for k in ("guided", "pending"):                     # leaving step-by-step / a warning
            ctx.user_data.pop(k, None)

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
            return await show(q, history_screen(0, "That report was not found."))
        if action == "v":                                   # open one report
            return await show(q, report_view(data, iso))
        if action == "f":                                   # send its file again
            return await deliver_file(ctx, complete(data), iso, report_view(data, iso), q.message.message_id)
        if action == "e":                                   # update (edit) it
            return await show(q, report_screen(start_session(ctx, iso, explicit=True)))
        if action == "d":                                   # ask before deleting
            return await show(q, (f"🗑 <b>Delete the report of {dmy(iso)}?</b>\nThis cannot be undone.",
                                  Markup([[Btn("Yes, delete", callback_data=f"h:dy:{iso}"),
                                           Btn("Cancel", callback_data=f"h:v:{iso}")]])))
        if action == "dy":                                  # delete it
            history.delete_report(iso)
            return await show(q, history_screen(0, f"🗑 Deleted {dmy(iso)}"))

    # ----- filling the report -----
    s = get_session(ctx)
    data = s["data"]

    if cmd == "m:main":
        ctx.user_data.pop("waiting", None)
        await show(q, report_screen(s))
    elif cmd == "g:start":
        await show(q, enter_step(ctx, s, 0))
    elif cmd in ("g:next", "g:back"):
        g = ctx.user_data.get("guided", 0)
        await show(q, enter_step(ctx, s, g + (1 if cmd == "g:next" else -1)))
    elif cmd == "g:stop":
        for k in ("guided", "waiting", "pending"):
            ctx.user_data.pop(k, None)
        await show(q, report_screen(s))
    elif cmd == "review":
        for k in ("guided", "waiting", "pending"):
            ctx.user_data.pop(k, None)
        await show(q, review_screen(s))
    elif cmd == "w:ok":                                     # keep the number that looked too big
        p = ctx.user_data.pop("pending", None)
        if not p:
            return await show(q, report_screen(s))
        kind, group, key, nums = p
        await show(q, commit_item(ctx, s, group, key, nums) if kind == "item" else commit_bar(ctx, s, nums))
    elif cmd == "w:no":                                     # type it again
        p = ctx.user_data.pop("pending", None)
        g = ctx.user_data.get("guided")
        if g is not None:
            screen = enter_step(ctx, s, g)
        elif p and p[0] == "item":
            ctx.user_data["waiting"] = ("item", p[1], p[2])
            screen = item_prompt(p[1], p[2])
        else:
            ctx.user_data["waiting"] = ("bar",)
            screen = bar_prompt()
        await show(q, screen)
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
    # ----- quick amount buttons (cash only) -----
    elif cmd.startswith("q:"):
        parts = cmd.split(":")
        # q:bar:50  or  q:rooms:pool:40
        if len(parts) == 3 and parts[1] == "bar":
            amount = float(parts[2])
            nums = [amount, 0, 0]
            cap = unusual(s, nums, "bar")
            if cap:
                ctx.user_data["pending"] = ("bar", None, None, nums)
                await show(q, warn_screen("Bar", nums, cap))
            else:
                await show(q, commit_bar(ctx, s, nums))
        elif len(parts) == 4:
            _, group, key, amount_str = parts
            amount = float(amount_str)
            nums = [amount, 0, 0]
            cap = unusual(s, nums, "item", group, key)
            if cap:
                ctx.user_data["pending"] = ("item", group, key, nums)
                await show(q, warn_screen(LABELS[group][key], nums, cap))
            else:
                await show(q, commit_item(ctx, s, group, key, nums))
        else:
            await show(q, report_screen(s))
    elif cmd == "exp:undo":
        note = ""
        if data["expenses"]:
            gone = data["expenses"].pop()
            note = f"↩️ Removed {gone['name']}: {n(gone['amount'])}"
        ctx.user_data["waiting"] = ("expense",)
        await show(q, expense_view(ctx, s, note))
    elif cmd == "act:reset":
        await show(q, ("🗑 <b>Clear all numbers of this report?</b>\n(History is not changed.)",
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
            for k in ("guided", "waiting", "pending"):
                ctx.user_data.pop(k, None)
            await deliver_file(ctx, data, s["date"], done_screen(s), q.message.message_id)
            await q.answer("✅ Saved to history")
        except Exception:
            logging.exception("could not make the report")
            await q.answer("Sorry, could not make the file", show_alert=True)


# ---------- typed messages ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.strip() in SHORTCUT_TEXTS:                      # the two buttons at the bottom
        return await (today_cmd if text.strip() == "📝 Today" else history_cmd)(update, ctx)
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

    if w is None:  # nothing chosen: still understands the one-message format
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
        nums = three(nums)
        cap = unusual(s, nums, "item", group, key)
        if cap:
            ctx.user_data["pending"] = ("item", group, key, nums)
            return await respond(update, ctx, warn_screen(LABELS[group][key], nums, cap))
        await respond(update, ctx, commit_item(ctx, s, group, key, nums))

    elif w[0] == "bar":
        nums = numbers(text)
        if not nums:
            return await respond(update, ctx, bar_prompt("⚠️ Please type numbers."))
        nums = three(nums)
        cap = unusual(s, nums, "bar")
        if cap:
            ctx.user_data["pending"] = ("bar", None, None, nums)
            return await respond(update, ctx, warn_screen("Bar", nums, cap))
        await respond(update, ctx, commit_bar(ctx, s, nums))

    elif w[0] == "discount":
        _, nums, rest = split_item(text)
        if not nums:
            return await respond(update, ctx, discount_prompt("⚠️ Please type the amount first."))
        await respond(update, ctx, commit_discount(ctx, s, nums[0], " ".join(rest)))

    elif w[0] == "expense":
        words, nums, _ = split_item(text)
        if not words or not nums:
            return await respond(update, ctx, expense_view(ctx, s, "⚠️ Type the name, then the amount. Example: water 12"))
        data["expenses"].append({"name": " ".join(words), "amount": nums[0]})
        await respond(update, ctx, expense_view(ctx, s, f"✅ Added {' '.join(words)}: {n(nums[0])}"))


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


async def post_init(app: Application):
    """The blue "Menu" button next to the typing box."""
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 Home"),
        BotCommand("today", "📝 Today's report"),
        BotCommand("history", "📚 History"),
    ])


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
           .defaults(Defaults(parse_mode=ParseMode.HTML))
           .post_init(post_init)
           .request(make_request()).get_updates_request(make_request())
           .persistence(PicklePersistence(filepath=str(history.DATA_DIR / "bot_data.pickle")))
           .concurrent_updates(True)   # a slow click does not make the next click wait
           .build())
    only_me = filters.Chat(chat_id=OWNER)
    app.add_handler(CommandHandler(["start", "menu"], home_cmd, filters=only_me))
    app.add_handler(CommandHandler("today", today_cmd, filters=only_me))
    app.add_handler(CommandHandler("history", history_cmd, filters=only_me))
    app.add_handler(CommandHandler("keyboard", keyboard_cmd, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_daily(reminder, time=reminder_time(), user_id=OWNER, chat_id=OWNER)
    print(f"Bot is running. Daily reminder at {reminder_time():%H:%M} (Cambodia time)")
    app.run_polling()


if __name__ == "__main__":
    main()