# bot.py - Telegram bot: clean aligned screens, step-by-step entry, review before sending,
# always-visible shortcuts, History (open / update / delete), chat cleans itself.
import asyncio, calendar, copy, html, logging, os, re, tempfile, zlib
from datetime import datetime, time
from time import monotonic, perf_counter
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults,
                          MessageHandler, PicklePersistence, filters)
from telegram.request import HTTPXRequest

import db
import history
import monthly
import storage
import importer
import speech
import lang
from fill import fill_template, summarize
from reader import parse as parse_rules, _split as split_item

load_dotenv()
logging.basicConfig(level=logging.INFO)
for noisy in ("httpx", "httpcore"):          # these lines would print your bot token in the log
    logging.getLogger(noisy).setLevel(logging.WARNING)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])  # only you can use the bot
TZ = ZoneInfo("Asia/Phnom_Penh")
PAGE = 8  # reports per page in History
VERSION = "2026-11-03 mongodb+storage+drafts+import"   # shown in /ping and in the log, to see which file runs

# ---------- language (KH / EN) ----------
_LANG = "en"


def set_lang(ctx):
    """Remember the language of this chat for this update."""
    global _LANG
    _LANG = (ctx.user_data or {}).get("lang", "en")


def tr(key, **fmt):
    """A text in the current language. Falls back to English."""
    d = lang.STRINGS.get(_LANG) or lang.STRINGS["en"]
    text = d.get(key) or lang.STRINGS["en"].get(key) or key
    try:
        return text.format(**fmt) if fmt else text
    except (KeyError, IndexError, ValueError):
        return text


def noun(n, one, many):
    return tr(one) if n == 1 else tr(many)


KHMER_MONTHS = ["មករា", "កុម្ភៈ", "មីនា", "មេសា", "ឧសភា", "មិថុនា",
                "កក្កដា", "សីហា", "កញ្ញា", "តុលា", "វិច្ឆិកា", "ធ្នូ"]


def month_name(m):
    return KHMER_MONTHS[m - 1] if _LANG == "kh" else calendar.month_name[m]


def month_abbr(m):
    return KHMER_MONTHS[m - 1] if _LANG == "kh" else calendar.month_abbr[m]


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

SHORTCUTS = ReplyKeyboardMarkup([[KeyboardButton("📝 Today"), KeyboardButton("📚 History"), KeyboardButton("📊 Month")]],
                                resize_keyboard=True, is_persistent=True,
                                input_field_placeholder="Tap a button, or type numbers")
SHORTCUT_TEXTS = ("📝 Today", "📚 History", "📊 Month")


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
    return {"rooms": {}, "tours": {}, "bar": [], "discount": {}, "expenses": [], "cash_check": {}}


def complete(data):
    base = fresh()
    base.update(data or {})
    return base


def three(nums):
    return (list(nums) + [0, 0, 0])[:3]


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def numbers(text):
    """Read the numbers in a message. '1,087.85' is one number, '40,0,20' is three."""
    out = []
    for t in _THOUSANDS.sub("", text).replace(",", " ").split():
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
def start_session(ctx, iso, explicit=False, force_new=False):
    """Open a report. A draft is resumed, so unfinished work is never thrown away.
    force_new=True starts empty (the caller has already trashed the draft)."""
    draft = None if force_new else storage.load_draft(iso)
    if draft:
        data = complete(copy.deepcopy(draft["data"]))
        explicit, saved = draft["explicit"], draft["saved"]
    else:
        saved_report = history.load_report(iso)
        data = complete(copy.deepcopy(saved_report)) if saved_report else fresh()
        saved = saved_report is not None
    ctx.user_data["session"] = {"date": iso, "explicit": explicit, "saved": saved, "data": data}
    for k in ("waiting", "guided", "pending", "pad", "cash_ui"):
        ctx.user_data.pop(k, None)
    return ctx.user_data["session"]


def get_session(ctx):
    s = ctx.user_data.get("session")
    # a normal session from an earlier day starts again for today
    if s is None or (not s["explicit"] and s["date"] != today_iso()):
        s = start_session(ctx, today_iso())
    return s


_DRAFT_TASKS = {}


def save_draft_now(ctx, s):
    """Write the draft at once (after a value was saved)."""
    t = _DRAFT_TASKS.pop(OWNER, None)
    if t:
        t.cancel()
    try:
        storage.save_draft(s)
    except Exception:
        logging.exception("could not save the draft")


def schedule_draft_save(ctx, s, delay=1.5):
    """Debounce keypad digits: write the draft after a short pause."""
    t = _DRAFT_TASKS.pop(OWNER, None)
    if t:
        t.cancel()

    async def later():
        try:
            await asyncio.sleep(delay)
            if ctx.user_data.get("session") is s:
                storage.save_draft(s)
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("could not save the draft")

    _DRAFT_TASKS[OWNER] = asyncio.create_task(later())


# ---------- the aligned table ----------
W = 13


def trow(label, v):
    c, k, a = three(v)
    return f"{label:<{W}}{n(c):>7}{n(k):>7}{n(a):>7}"


def table_block(data):
    lines = [f"{'':<{W}}{tr('cash_hdr'):>7}{tr('card_hdr'):>7}{tr('aba_hdr'):>7}"]
    for group, key in (("rooms", "rooms_hdr"), ("tours", "tours_hdr")):
        rows = [trow(SHORT[group][k], data[group][k]) for k in LABELS[group]
                if data[group].get(k) and sum(three(data[group][k]))]
        if rows:
            lines += [tr(key)] + rows
    if sum(three(data["bar"])):
        lines += [tr("bar_hdr"), trow(tr("bar"), data["bar"])]
    lines += ["-" * (W + 21), trow(tr("total_hdr"), method_totals(data))]
    return "\n".join(lines)


def report_body(data, iso, title):
    sm, d = summarize(data), data["discount"]
    empty = not any(sum(three(v)) for g in ("rooms", "tours") for v in data[g].values()) \
        and not sum(three(data["bar"]))
    parts = [f"<b>{title}</b> · {dmy(iso)}", ""]
    if empty:
        parts += [tr("nothing_filled")]
    else:
        parts += [f"<pre>{esc(table_block(data))}</pre>", tr("revenue", rev=n(sm['total']))]
    if d.get("amount"):
        line = tr("discount_line", amt=n(d['amount']))
        if d.get("reason"):
            line += tr("discount_reason", reason=esc(d['reason']))
        parts.append(line)
    if data["expenses"]:
        items = "\n".join(f"{e['name']}: {n(e['amount'])}" + ("" if e.get("cash", True) else tr("not_from_cash"))
                          for e in data["expenses"])
        parts += [tr("expenses_line", exp=n(sm['expenses'])), f"<pre>{esc(items)}</pre>"]
    parts += cash_lines(data)
    return "\n".join(parts)


# ---------- screens: each returns (text, buttons) ----------
def draft_banner():
    """The unfinished drafts, as (text lines, buttons). Today first, then older days."""
    today = today_iso()
    lines, rows = [], []
    draft = storage.load_draft(today)
    if draft:
        rev = summarize(complete(draft["data"]))["total"]
        lines.append(tr("draft_in_progress", date=dmy(today), rev=n(rev)))
        rows.append([Btn(tr("continue_draft"), callback_data="d:cont"), Btn(tr("start_new_btn"), callback_data="d:new")])
    for d in storage.list_drafts(exclude=today):
        iso = d["date"]
        lines.append(tr("draft_older", date=dmy(iso)))
        rows.append([Btn(tr("continue_date", date=dmy(iso)[:5]), callback_data=f"d:cont:{iso}"),
                     Btn(tr("discard"), callback_data=f"d:disc:{iso}")])
    return lines, rows


def home_screen():
    saved = history.load_report(today_iso()) is not None
    text = (tr("home_title") + "\n\n"
            + (tr("home_saved") + "\n" if saved else tr("home_not_made") + "\n"))
    banner, banner_rows = draft_banner()
    if banner:
        text += "\n" + "\n".join(banner)
    text += "\n\n" + tr("home_what")
    start = tr("update_today") if saved else tr("start_new")
    rows = [[Btn(start, callback_data="new")]] + banner_rows + [
        [Btn(tr("history"), callback_data="hist:0"), Btn(tr("month"), callback_data="mon:cur")],
        [Btn(tr("other_date"), callback_data="newdate"), Btn(tr("storage"), callback_data="stor:0")],
        [Btn(tr("language"), callback_data="lang:menu")],
    ]
    return text, Markup(rows)


def lang_screen():
    return (tr("lang_title") + "\n\n" + tr("lang_choose"),
            Markup([[Btn(tr("lang_en"), callback_data="lang:en"), Btn(tr("lang_kh"), callback_data="lang:kh")]]))


def report_screen(s, note=""):
    data, sm, d = s["data"], summarize(s["data"]), s["data"]["discount"]
    mode = tr("mode_update") if s.get("saved") else tr("mode_new")
    rooms, tours = filled(data, "rooms"), filled(data, "tours")
    rows = [
        f"{tr('rooms'):<10}{dots(rooms)}  {rooms}/6",
        f"{tr('tours'):<10}{dots(tours)}  {tours}/6",
        f"{tr('bar'):<10}{n(sm['bar'])}" if sm["bar"] else f"{tr('bar'):<10}{tr('dash')}",
        f"{tr('discount'):<10}{n(d['amount'])}" if d.get("amount") else f"{tr('discount'):<10}{tr('dash')}",
        f"{tr('expenses'):<10}{len(data['expenses'])} {tr('items')} · {n(sm['expenses'])}" if data["expenses"]
        else f"{tr('expenses'):<10}{tr('dash')}",
    ]
    cf = cash_figures(data)
    rows.append(f"{tr('cash'):<10}" + (f"{cash_status(cf).replace('⚠️ ', '').replace('✅ ', '')}" if cf["has"] else tr("not_counted")))
    cash, card, aba = method_totals(data)
    text = ((esc(note) + "\n\n") if note else "") + (
        tr("report_title", date=dmy(s['date'])) + f"\n{mode}\n\n"
        f"<pre>{esc(chr(10).join(rows))}</pre>"
        f"<pre>{'':<10}{tr('cash_hdr'):>7}{tr('card_hdr'):>7}{tr('aba_hdr'):>7}\n"
        f"{tr('total_hdr'):<10}{n(cash):>7}{n(card):>7}{n(aba):>7}</pre>"
        + tr("revenue", rev=n(sm['total'])))
    return text, Markup([
        [Btn(tr("fill_step"), callback_data="g:start")],
        [Btn(tr("rooms_btn"), callback_data="m:rooms"), Btn(tr("tours_btn"), callback_data="m:tours"),
         Btn(tr("bar_btn"), callback_data="in:bar")],
        [Btn(tr("discount_btn"), callback_data="in:discount"), Btn(tr("expenses_btn"), callback_data="in:expense")],
        [Btn(tr("cash_btn"), callback_data="cash:main"), Btn(tr("review_btn"), callback_data="review")],
        [Btn(tr("home_btn"), callback_data="home"), Btn(tr("clear_all"), callback_data="act:reset")],
    ])


def group_screen(s, group, note=""):
    data = s["data"]
    text = ((esc(note) + "\n\n") if note else "") + tr("rooms_title" if group == "rooms" else "tours_title") \
        + "\n" + tr("tap_one")
    rows = []
    for key, label in LABELS[group].items():
        vals = data[group].get(key)
        rows.append([Btn(tr("filled", label=label, vals=short(vals)) if vals and sum(three(vals))
                         else tr("empty_item", label=label), callback_data=f"in:{group}:{key}")])
    rows.append([Btn(tr("back_report"), callback_data="m:main")])
    return text, Markup(rows)


def prompt_screen(title, example, back, note="", extra=""):
    text = ((esc(note) + "\n\n") if note else "") + f"<b>{title}</b>\n\n{extra}" \
        + tr("type_now", example=example)
    return text, Markup([[Btn(tr("back"), callback_data=back)]])


def discount_prompt(note=""):
    return prompt_screen(tr("discount_title"), tr("example_discount"), "m:main", note, tr("discount_extra"))


def expense_lines(s):
    items = s["data"]["expenses"]
    lines = [f"{i}. {esc(e['name'])}: {n(e['amount'])}" + ("" if e.get("cash", True) else tr("not_from_drawer_short"))
             for i, e in enumerate(items, 1)] or [tr("none_yet")]
    return lines + ["", tr("total_b", total=n(sum(e['amount'] for e in items)))]


def name_key(name):
    return zlib.crc32(str(name).strip().lower().encode()) & 0xFFFF


def frequent_expenses(s, limit=6):
    """Your usual expenses, learned from your past reports: (name, newest amount), most used first."""
    have = {str(e.get("name", "")).strip().lower() for e in s["data"]["expenses"]}
    seen = {}
    for iso in previous_isos(s)[:60]:                       # newest first
        for e in (history.load_report(iso) or {}).get("expenses") or []:
            key = str(e.get("name", "")).strip().lower()
            if key and key not in have:
                rec = seen.setdefault(key, {"name": str(e["name"]).strip(), "amount": e.get("amount", 0), "count": 0})
                rec["count"] += 1                          # the amount stays the newest one
    ranked = sorted(seen.values(), key=lambda r: -r["count"])
    return [(r["name"], r["amount"]) for r in ranked[:limit]]


def last_expenses(s):
    """The newest earlier report that has expenses: (date, list), or (None, [])."""
    for iso in previous_isos(s)[:60]:
        old = (history.load_report(iso) or {}).get("expenses") or []
        if old:
            return iso, old
    return None, []


def quick_expense_rows(s):
    """Buttons: your usual expenses, and 'same as the last report'."""
    btns = [Btn(tr("quick_add", name=name[:16], amt=n(amt)), callback_data=f"exp:q:{i}:{name_key(name)}")
            for i, (name, amt) in enumerate(frequent_expenses(s))]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    iso, old = last_expenses(s)
    if iso and not s["data"]["expenses"]:
        rows.append([Btn(tr("same_as", date=dmy(iso)[:5], n=len(old),
                            noun=noun(len(old), "report_one", "report_many")), callback_data="exp:yday")])
    return rows


def expense_screen(s, note=""):
    items = s["data"]["expenses"]
    quick = quick_expense_rows(s)
    lines = ([esc(note), ""] if note else []) + [tr("expenses_title"), ""] + expense_lines(s) + [
        "", tr("type_next"), tr("example_line", example=tr("example_expense"))]
    if quick:
        lines.append(tr("usual_expenses"))
    row = ([Btn(tr("undo_last"), callback_data="exp:undo"), Btn(tr("edit_list"), callback_data="exp:list:main")]
           if items else []) + [Btn(tr("done"), callback_data="m:main")]
    return "\n".join(lines), Markup(quick + [row])


# ---------- edit the expenses: change a name or amount, delete one, say it was NOT paid from the drawer ----------
def expense_list_screen(s, note=""):
    items = s["data"]["expenses"]
    lines = ([esc(note), ""] if note else []) + [tr("edit_expenses"), ""]
    lines += expense_lines(s)[:-2] if items else [tr("none_yet")]
    if items:
        drawer = sum(e["amount"] for e in items if e.get("cash", True))
        lines += ["", tr("drawer_line", total=n(sum(e['amount'] for e in items)), n=n(drawer))]
    lines += ["", tr("tap_expense")]
    rows = [[Btn(f"{i}. {e['name'][:20]} · {n(e['amount'])}" + ("" if e.get("cash", True) else " 🚫"),
                 callback_data=f"exp:item:{i - 1}")] for i, e in enumerate(items, 1)]
    rows.append([Btn(tr("back"), callback_data="exp:back")])
    return "\n".join(lines), Markup(rows)


def expense_item_screen(s, idx, note=""):
    e = s["data"]["expenses"][idx]
    cash = e.get("cash", True)
    lines = ([esc(note), ""] if note else []) + [
        tr("expense_n", n=idx + 1), "", tr("name", name=esc(e['name'])), tr("amount", amt=n(e['amount'])),
        tr("drawer_yes") if cash else tr("drawer_no")]
    return "\n".join(lines), Markup([
        [Btn(tr("new_name"), callback_data=f"exp:ename:{idx}"), Btn(tr("new_amount"), callback_data=f"exp:eamt:{idx}")],
        [Btn(tr("not_from_drawer") if not cash else tr("from_drawer"), callback_data=f"exp:tog:{idx}")],
        [Btn(tr("delete"), callback_data=f"exp:delq:{idx}"), Btn(tr("back"), callback_data="exp:list:keep")],
    ])


def step_screen(s, idx, note=""):
    step, data = STEPS[idx], s["data"]
    lines = ([esc(note), ""] if note else []) + [tr("step", n=idx + 1, total=len(STEPS), date=dmy(s['date'])), ""]
    if step[0] == "discount":
        d = data["discount"]
        lines += [tr("discount_optional"), tr("discount_type"), tr("example_line", example=tr("example_discount"))]
        if d.get("amount"):
            lines += ["", tr("discount_now", amt=n(d['amount']), reason=esc(d.get('reason', ''))).strip()]
        lines += ["", tr("nothing_today")]
    else:  # expense
        lines += [tr("expenses_optional"), ""] + expense_lines(s) + [
            "", tr("type_each"), tr("example_line", example=tr("example_expense")), tr("tap_done")]
    nav = []
    if step[0] == "expense":
        if data["expenses"]:
            nav.append(Btn(tr("undo_last"), callback_data="exp:undo"))
        nav.append(Btn(tr("done"), callback_data="g:next"))
    else:
        nav.append(Btn(tr("skip"), callback_data="g:next"))
    if idx > 0:
        nav.append(Btn(tr("back"), callback_data="g:back"))
    second = ([Btn(tr("edit_list"), callback_data="exp:list:guided")] if step[0] == "expense" and data["expenses"] else []) \
        + [Btn(tr("menu"), callback_data="g:stop")]
    quick = quick_expense_rows(s) if step[0] == "expense" else []
    if quick:
        lines.append(tr("usual_expenses"))
    return "\n".join(lines), Markup(quick + [nav, second])


# ---------- the number keypad (for a room, a tour or the bar: cash / card / aba) ----------
def fmt_input(x):
    x = float(x)
    if x == 0:
        return ""
    return str(int(x)) if x == int(x) else f"{x:g}"


def val(v):
    try:
        return float(v)
    except ValueError:
        return 0.0


def init_pad(s, step, nums=None):
    """Start the keypad. It shows the saved numbers (or the numbers you typed before a warning).
    On saved numbers, the first digit you tap replaces the number; the numbers you typed before
    a warning are kept, so you can fix them with the delete key."""
    keep = nums is not None
    if nums is None:
        cur = s["data"][step[1]].get(step[2]) if step[0] == "item" else s["data"]["bar"]
        nums = cur if cur and sum(three(cur)) else None
    return {"vals": [fmt_input(x) for x in three(nums)] if nums else ["", "", ""], "field": 0,
            "fresh": [not keep] * 3}


def pad_screen(ctx, s, note=""):
    w, pad = ctx.user_data["waiting"], ctx.user_data["pad"]
    title = f"{tr('rooms_btn' if w[1] == 'rooms' else 'tours_btn')} › {LABELS[w[1]][w[2]]}" \
        if w[0] == "item" else tr("bar_restaurant")
    shown = [v or "0" for v in pad["vals"]]
    cells = [f"[{v}]" if i == pad["field"] else v for i, v in enumerate(shown)]
    g = ctx.user_data.get("guided")
    lines = ([esc(note), ""] if note else [])
    if g is not None:
        lines.append(tr("step", n=g + 1, total=len(STEPS), date=dmy(s['date'])))
    lines += [f"<b>{title}</b>", "",
              f"<pre>{tr('cash_hdr'):>9}{tr('card_hdr'):>9}{tr('aba_hdr'):>9}\n{cells[0]:>9}{cells[1]:>9}{cells[2]:>9}</pre>",
              tr("total", total=n(sum(val(v) for v in pad['vals']))), "",
              tr("pick_field"), tr("or_type"), tr("or_voice")]
    f = pad["field"]
    names = [tr("cash"), tr("card"), tr("aba")]
    rows = [[Btn(("▸ " if i == f else "") + names[i], callback_data=f"k:f{i}") for i in range(3)]]
    for r in (("7", "8", "9"), ("4", "5", "6"), ("1", "2", "3")):
        rows.append([Btn(d, callback_data=f"k:{d}") for d in r])
    rows.append([Btn("0", callback_data="k:0"), Btn(".", callback_data="k:."), Btn("⌫", callback_data="k:b")])
    rows.append([Btn(tr("clear"), callback_data="k:c"), Btn(tr("next_field"), callback_data="k:n")])
    rows.append([Btn(tr("save"), callback_data="k:s")])
    if g is not None:
        rows.append([Btn(tr("skip"), callback_data="g:next")] + ([Btn(tr("back"), callback_data="g:back")] if g > 0 else [])
                    + [Btn(tr("menu"), callback_data="g:stop")])
    else:
        rows.append([Btn(tr("back"), callback_data=f"m:{w[1]}" if w[0] == "item" else "m:main")])
    return "\n".join(lines), Markup(rows)


def press_key(pad, key):
    """One tap on the keypad."""
    v, f = pad["vals"], pad["field"]
    fresh = pad.setdefault("fresh", [False] * 3)
    if (key.isdigit() or key == ".") and fresh[f]:       # first digit on a saved number: start new
        v[f] = ""
    if key not in ("n",) and not key.startswith("f"):
        fresh[f] = False
    if key.isdigit():
        cur = "" if v[f] == "0" else v[f]
        if len(cur.replace(".", "")) < 9:
            v[f] = cur + key
    elif key == ".":
        if "." not in v[f]:
            v[f] = (v[f] or "0") + "."
    elif key == "b":
        v[f] = v[f][:-1]
    elif key == "c":
        v[f] = ""
    elif key == "n":
        pad["field"] = (f + 1) % 3
    elif key.startswith("f") and key[1:].isdigit() and int(key[1:]) < 3:
        pad["field"] = int(key[1:])


def enter_step(ctx, s, idx, note=""):
    """Go to a step of the guided entry (after the last step: the review)."""
    if idx >= len(STEPS):
        for k in ("guided", "waiting"):
            ctx.user_data.pop(k, None)
        return review_screen(s, note)
    idx = max(0, idx)
    ctx.user_data["guided"] = idx
    ctx.user_data["waiting"] = STEPS[idx]
    if STEPS[idx][0] in ("item", "bar"):
        ctx.user_data["pad"] = init_pad(s, STEPS[idx])
        return pad_screen(ctx, s, note)
    ctx.user_data.pop("pad", None)
    return step_screen(s, idx, note)


def expense_view(ctx, s, note=""):
    g = ctx.user_data.get("guided")
    return step_screen(s, g, note) if g is not None else expense_screen(s, note)


def review_screen(s, note=""):
    data = s["data"]
    empty = not any(sum(three(v)) for grp in ("rooms", "tours") for v in data[grp].values()) \
        and not sum(three(data["bar"])) and not data["expenses"]
    text = ((esc(note) + "\n\n") if note else "") + report_body(data, s["date"], tr("review_title"))
    cmp = compare_lines(s)
    text += ("\n" + "\n".join(cmp) if cmp else "")
    text += (("\n\n" + tr("empty_report")) if empty else "") + "\n\n" + tr("is_correct")
    return text, Markup([
        [Btn(tr("confirm"), callback_data="act:generate")],
        [Btn(tr("cash_btn"), callback_data="cash:review"), Btn(tr("fix_something"), callback_data="m:main")],
    ])


def previous_isos(s):
    """Days with a saved report before this one, newest first."""
    return [i for i in history.list_reports() if i < s["date"]]


def compare_lines(s):
    """How today's revenue compares with the last report and with the last 7 days."""
    cur = summarize(s["data"])["total"]
    prev = previous_isos(s)[:7]
    if not cur or not prev:
        return []
    vals = [summarize(complete(history.load_report(i) or {}))["total"] for i in prev]
    out = []
    if vals[0]:
        p = (cur - vals[0]) / vals[0] * 100
        out.append(f"{'📈' if p >= 0 else '📉'} {p:+.0f}% " + tr("vs_day", date=dmy(prev[0])[:5], rev=n(vals[0])))
    if len(vals) >= 3 and sum(vals) > 0:
        avg = sum(vals) / len(vals)
        p = (cur - avg) / avg * 100
        out.append(f"{'📈' if p >= 0 else '📉'} {p:+.0f}% " + tr("vs_avg", n=len(vals), avg=n(avg)))
    return out


def done_screen(s):
    sm = summarize(s["data"])
    return (tr("report_saved", date=dmy(s['date'])) + "\n"
            + tr("revenue_expenses", rev=n(sm['total']), exp=n(sm['expenses'])) + "\n\n"
            + tr("file_above"),
            Markup([[Btn(tr("change_something"), callback_data="m:main")],
                    [Btn(tr("history"), callback_data="hist:0"), Btn(tr("home_btn"), callback_data="home")]]))


def warn_screen(label, nums, cap):
    return (tr("warn_title") + "\n\n" + tr("warn_body", label=esc(label), nums=esc(fmt(nums))) + "\n"
            + tr("warn_hint", cap=n(cap)),
            Markup([[Btn(tr("yes_keep"), callback_data="w:ok"), Btn(tr("type_again"), callback_data="w:no")]]))


def history_screen(page, note=""):
    items = history.list_reports()
    if not items:
        return (esc(note) + tr("no_reports"),
                Markup([[Btn(tr("start_new"), callback_data="new"), Btn(tr("home_btn"), callback_data="home")],
                        [Btn(tr("storage"), callback_data="stor:0")]]))
    pages = (len(items) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = [[Btn(tr("month_summary"), callback_data="mon:cur")]]
    for iso in items[page * PAGE:(page + 1) * PAGE]:
        data = history.load_report(iso)
        total = summarize(complete(data))["total"] if data else 0
        rows.append([Btn(tr("day_row", date=dmy(iso), rev=n(total)), callback_data=f"h:v:{iso}")])
    nav = []
    if page > 0:
        nav.append(Btn(tr("newer"), callback_data=f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn(tr("older"), callback_data=f"hist:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(tr("storage"), callback_data="stor:0"), Btn(tr("home_btn"), callback_data="home")])
    return ((esc(note) + "\n\n" if note else "")
            + tr("history_count", n=len(items), noun=noun(len(items), "report_one", "report_many"))
            + "\n" + tr("tap_open")), Markup(rows)


def report_view_markup(iso):
    return Markup([
        [Btn(tr("send_file"), callback_data=f"h:f:{iso}"), Btn(tr("update"), callback_data=f"h:e:{iso}")],
        [Btn(tr("delete"), callback_data=f"h:d:{iso}")],
        [Btn(tr("history"), callback_data="hist:0"), Btn(tr("home_btn"), callback_data="home")],
    ])


def report_view(data, iso):
    return report_body(complete(data), iso, tr("report")), report_view_markup(iso)


# ---------- storage: the report files you sent me ----------
def storage_screen(page, note=""):
    items = storage.list_reports()
    if not items:
        return (esc(note) + tr("storage_empty"),
                Markup([[Btn(tr("home_btn"), callback_data="home")]]))
    pages = (len(items) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = [[Btn(tr("storage_day", date=dmy(iso)), callback_data=f"st:v:{iso}")]
            for iso in items[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(Btn(tr("newer"), callback_data=f"stor:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn(tr("older"), callback_data=f"stor:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(tr("home_btn"), callback_data="home")])
    return ((esc(note) + "\n\n" if note else "")
            + tr("storage_title", n=len(items), noun=noun(len(items), "file_one", "file_many"))
            + "\n" + tr("storage_tap")), Markup(rows)


def import_body(data, date_label, title="📥 IMPORT"):
    """The same aligned table as a report, for an imported file."""
    sm = summarize(data)
    empty = not any(sum(three(v)) for g in ("rooms", "tours") for v in data[g].values()) \
        and not sum(three(data["bar"]))
    parts = [tr("import_body", title=title, date=date_label), ""]
    if empty:
        parts += [tr("nothing_in_file")]
    else:
        parts += [f"<pre>{esc(table_block(data))}</pre>", tr("import_revenue", rev=n(sm['total']))]
    d = data["discount"]
    if d.get("amount"):
        line = tr("discount_line", amt=n(d['amount']))
        if d.get("reason"):
            line += tr("discount_reason", reason=esc(d['reason']))
        parts.append(line)
    if data["expenses"]:
        items = "\n".join(f"{esc(e['name'])}: {n(e['amount'])}" for e in data["expenses"])
        parts += [tr("import_expenses", exp=n(sm['expenses'])), f"<pre>{items}</pre>"]
    return "\n".join(parts)


def next_missing_date(ctx):
    """Ask for the date of the first file that has none. Returns True if one is asked for."""
    imports = ctx.user_data.get("imports") or []
    for i, imp in enumerate(imports):
        if not imp["date"]:
            ctx.user_data["waiting"] = ("importdate", i)
            return True
    ctx.user_data.pop("waiting", None)
    return False


def import_screen(ctx, note=""):
    """A batch of imported files: send several at once, then Save all."""
    imports = ctx.user_data.get("imports") or []
    if not imports:
        return home_screen()
    n = len(imports)
    lines = [tr("import_title", n=n, noun=noun(n, "file_one", "file_many")), ""]
    existing = []
    for i, imp in enumerate(imports, 1):
        title = tr("import_title_plain") if n == 1 else tr("import_file_plain", n=i)
        if imp["date"]:
            lines.append(import_body(imp["data"], dmy(imp["date"]), title))
            if history.load_report(imp["date"]) is not None:
                existing.append(imp["date"])
        else:
            lines.append(tr("no_date", name=esc(imp['name'])))
        if imp["warnings"]:
            lines.append("⚠️ " + " · ".join(esc(w) for w in imp["warnings"]))
        lines.append("")
    if existing:
        lines.append(tr("already_saved", days=", ".join(dmy(d) for d in existing)))
    lines.append(tr("send_every"))
    if note:
        lines.insert(0, esc(note) + "\n")
    btns = [[Btn(tr("replace_all") if existing else tr("save_all"), callback_data="imp:save"),
             Btn(tr("cancel"), callback_data="imp:cancel")]]
    return "\n".join(lines), Markup(btns)


def mark_undo(ctx, items, screen="home"):
    """Copy the data to trash before a destructive action, so it can be undone."""
    entries = [{"name": storage.trash(kind, iso, payload), "kind": kind, "iso": iso}
               for kind, iso, payload in items]
    ctx.user_data["undo"] = {"items": entries, "screen": screen, "at": monotonic()}


def add_undo(ctx, screen):
    """Add an [↩️ Undo] button if there is a recent destructive action."""
    u = ctx.user_data.get("undo")
    if not u or monotonic() - u["at"] > 600:
        return screen
    rows = [list(r) for r in screen[1].inline_keyboard] + [[Btn("↩️ Undo", callback_data="undo")]]
    return screen[0], Markup(rows)


# ---------- cash check: what the drawer should hold, and what you counted (dollars) ----------
USD_NOTES = [100, 50, 20, 10, 5, 2, 1]


def sgn(v):
    v = round(float(v), 2)
    return f"+{n(v)}" if v > 0 else n(v)


def cash_figures(data):
    cc = data.get("cash_check") or {}
    notes = (cc.get("counts") or {}).get("usd") or {}
    counted_notes = sum(int(d) * c for d, c in notes.items())
    typed = cc.get("typed")
    counted = float(typed) if typed is not None else float(counted_notes)
    cash_rev = method_totals(data)[0]
    all_exp = sum(float(e.get("amount") or 0) for e in data.get("expenses") or [])
    expenses = sum(float(e.get("amount") or 0) for e in data.get("expenses") or [] if e.get("cash", True))
    opening = float(cc.get("opening") or 0)
    expected = opening + cash_rev - expenses
    return {"typed": typed, "counted": counted, "has": typed is not None or bool(counted_notes),
            "cash_rev": cash_rev, "expenses": expenses, "other_expenses": round(all_exp - expenses, 2),
            "opening": opening, "expected": expected, "diff": round(counted - expected, 2)}


def cash_status(f):
    if not f["has"]:
        return ""
    if abs(f["diff"]) < 0.005:
        return tr("matches")
    return tr("short", n=n(-f['diff'])) if f["diff"] < 0 else tr("over", n=n(f['diff']))


def cash_screen(ctx, s, note=""):
    data = s["data"]
    step = (ctx.user_data.get("cash_ui") or {}).get("step", 1)
    f = cash_figures(data)
    rows = [(tr("cash_revenue"), n(f["cash_rev"])), (tr("expenses"), "-" + n(f["expenses"]))]
    if f["opening"]:
        rows.append((tr("opening_cash"), n(f["opening"])))
    rows += [(tr("expected"), n(f["expected"])), (tr("counted"), n(f["counted"]) if f["has"] else tr("dash"))]
    if f["has"]:
        rows.append((tr("difference"), sgn(f["diff"])))
    lines = ([esc(note), ""] if note else []) + [
        tr("cash_check", date=dmy(s['date'])), "",
        "<pre>" + esc("\n".join(f"{a:<14}{b:>10}" for a, b in rows)) + "</pre>"]
    if f["has"]:
        lines.append(f"<b>{cash_status(f)}</b>")
        if f["typed"] is not None:
            lines.append(tr("typed_total"))
    if f["other_expenses"]:
        lines.append(tr("not_from_drawer_n", n=n(f["other_expenses"])))
    lines += ["", tr("cash_hint")]
    counts = ((data.get("cash_check") or {}).get("counts") or {}).get("usd", {})
    rows_b = [[Btn(("▸ " if step == x else "") + tr("step_n", n=x), callback_data=f"c:step:{x}") for x in (1, 5, 10)]]
    for d in USD_NOTES:
        rows_b.append([Btn("➖", callback_data=f"c:-:{d}"),
                       Btn(f"${d} ×{counts.get(str(d), 0)}", callback_data="c:noop"),
                       Btn("➕", callback_data=f"c:+:{d}")])
    rows_b.append([Btn(tr("type_total"), callback_data="c:total"), Btn(tr("opening_btn"), callback_data="c:open")])
    rows_b.append([Btn(tr("edit_expenses_btn"), callback_data="exp:list:cash")])
    rows_b.append([Btn(tr("reset_count"), callback_data="c:reset"), Btn(tr("done"), callback_data="c:done")])
    return "\n".join(lines), Markup(rows_b)


def cash_change(ctx, s, cmd):
    """One tap on the cash screen. Returns True if the screen needs to be drawn again."""
    ui = ctx.user_data.setdefault("cash_ui", {"step": 1})
    cc = s["data"].setdefault("cash_check", {})
    parts = cmd.split(":")                       # c:+:20  |  c:-:5  |  c:step:5  |  c:reset
    if parts[1] in ("+", "-") and parts[2].isdigit():
        counts = cc.setdefault("counts", {}).setdefault("usd", {})
        step = ui.get("step", 1) if parts[1] == "+" else -ui.get("step", 1)
        counts[parts[2]] = max(0, counts.get(parts[2], 0) + step)
        cc["typed"] = None                       # counting notes replaces a typed total
        return True
    if parts[1] == "step" and parts[2].isdigit():
        ui["step"] = int(parts[2])
        return True
    if parts[1] == "reset":
        cc["counts"], cc["typed"] = {}, None
        return True
    return False


def cash_lines(data):
    f = cash_figures(data)
    if not f["has"]:
        return []
    return [tr("cash_line", exp=n(f['expected']), cnt=n(f['counted']), status=cash_status(f))]


# ---------- month summary ----------
def day_row(iso, data):
    d = complete(data)
    sm, (cash, card, aba), cf = summarize(d), method_totals(d), cash_figures(d)
    return {"date": datetime.strptime(iso, "%Y-%m-%d").date(), "rooms": sm["rooms"], "tours": sm["tours"],
            "bar": sm["bar"], "cash": cash, "card": card, "aba": aba, "expenses": sm["expenses"],
            "counted": cf["counted"] if cf["has"] else None, "diff": cf["diff"] if cf["has"] else None}


def month_rows(ym):
    rows = []
    for iso in sorted(i for i in history.list_reports() if i.startswith(ym)):
        data = history.load_report(iso)
        if data:
            rows.append(day_row(iso, data))
    return rows


def shift_month(ym, k):
    y, m = int(ym[:4]), int(ym[5:7]) + k
    return f"{y + (m - 1) // 12:04d}-{(m - 1) % 12 + 1:02d}"


def month_title(ym):
    return f"{month_name(int(ym[5:7]))} {ym[:4]}"


def month_screen(ym, note=""):
    rows = month_rows(ym)
    st = monthly.stats(rows)
    prev_, next_ = shift_month(ym, -1), shift_month(ym, 1)
    nav = [Btn(f"⬅️ {month_abbr(int(prev_[5:7]))}", callback_data=f"mon:{prev_}")]
    if next_ <= today_iso()[:7]:
        nav.append(Btn(f"{month_abbr(int(next_[5:7]))} ➡️", callback_data=f"mon:{next_}"))
    foot = [Btn(tr("history"), callback_data="hist:0"), Btn(tr("home_btn"), callback_data="home")]
    head = ((esc(note) + "\n\n") if note else "") + tr("month_title", title=month_title(ym).upper())
    if not st:
        return head + "\n\n" + tr("no_month"), Markup([nav, foot])
    sd = lambda d: d.strftime("%d/%m")
    lines = [head + tr("days", n=st['days'], noun=noun(st['days'], "day_one", "day_many")), "",
             f"<pre>{'':<10}{tr('cash_hdr'):>9}{tr('card_hdr'):>9}{tr('aba_hdr'):>9}\n"
             f"{tr('rooms'):<10}{n(st['cash']):>9}{n(st['card']):>9}{n(st['aba']):>9}</pre>",
             tr("revenue", rev=n(st['revenue'])),
             tr("rooms_line", rooms=n(st['rooms']), tours=n(st['tours']), bar=n(st['bar'])),
             tr("expenses_line", exp=n(st['expenses'])),
             tr("net", net=n(st['net'])),
             tr("average", avg=n(st['avg'])),
             tr("best", date=sd(st['best'][0]), rev=n(st['best'][1]))]
    if st["days"] > 1:
        lines.append(tr("lowest", date=sd(st['worst'][0]), rev=n(st['worst'][1])))
    if st["counted_days"]:
        parts = [tr("cash_month", n=st['counted_days'], noun=noun(st['counted_days'], "day_one", "day_many"))]
        if st["short"]:
            parts.append(tr("short", n=n(-st['short'])))
        if st["over"]:
            parts.append(tr("over", n=n(st['over'])))
        lines.append(" · ".join(parts))
    return "\n".join(lines), Markup([nav, [Btn(tr("month_xlsx"), callback_data=f"monx:{ym}")], foot])


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


def submit_numbers(ctx, s, nums):
    """Save the numbers for the item or bar being filled (asks first if a number looks too big)."""
    w, nums = ctx.user_data["waiting"], three(nums)
    if w[0] == "item":
        _, group, key = w
        cap = unusual(s, nums, "item", group, key)
        if cap:
            ctx.user_data["pending"] = ("item", group, key, nums)
            return warn_screen(LABELS[group][key], nums, cap)
        return commit_item(ctx, s, group, key, nums)
    cap = unusual(s, nums, "bar")
    if cap:
        ctx.user_data["pending"] = ("bar", None, None, nums)
        return warn_screen("Bar", nums, cap)
    return commit_bar(ctx, s, nums)


def commit_item(ctx, s, group, key, nums):
    s["data"][group][key] = three(nums)
    save_draft_now(ctx, s)
    ctx.user_data.pop("pending", None)
    ctx.user_data.pop("pad", None)
    note = tr("item_saved", label=LABELS[group][key], fmt=fmt(nums))
    g = ctx.user_data.get("guided")
    if g is not None:
        return enter_step(ctx, s, g + 1, note)
    ctx.user_data.pop("waiting", None)
    return group_screen(s, group, note)


def commit_bar(ctx, s, nums):
    s["data"]["bar"] = three(nums)
    save_draft_now(ctx, s)
    ctx.user_data.pop("pending", None)
    ctx.user_data.pop("pad", None)
    note = tr("bar_saved", fmt=fmt(nums))
    g = ctx.user_data.get("guided")
    if g is not None:
        return enter_step(ctx, s, g + 1, note)
    ctx.user_data.pop("waiting", None)
    return report_screen(s, note)


def commit_discount(ctx, s, amount, reason):
    s["data"]["discount"] = {"amount": amount, "reason": reason}
    save_draft_now(ctx, s)
    note = tr("discount_saved", amt=n(amount), reason=reason).strip()
    g = ctx.user_data.get("guided")
    if g is not None:
        return enter_step(ctx, s, g + 1, note)
    ctx.user_data.pop("waiting", None)
    return report_screen(s, note)


# ---------- sending screens (one panel message that is edited) ----------
async def show(q, screen, _retry=True):
    """Button tap: edit the message that was tapped."""
    try:
        await q.edit_message_text(screen[0], reply_markup=screen[1])
    except RetryAfter as e:      # Telegram: "too many edits, wait a moment"
        wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else e.retry_after
        logging.warning("Telegram asked to wait %ss", wait)
        if _retry:
            await asyncio.sleep(min(float(wait), 10) + 0.5)
            await show(q, screen, _retry=False)
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
_LOCKS = {}


def pad_lock():
    """Keypad taps are drawn one at a time, always showing the NEWEST numbers."""
    return _LOCKS.setdefault(asyncio.get_running_loop(), asyncio.Lock())


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


async def flash(ctx, text, seconds=8):
    """A short message that removes itself, so answers to commands do not fill the chat."""
    set_lang(ctx)
    msg = await ctx.bot.send_message(OWNER, text)

    async def later():
        await asyncio.sleep(seconds)
        await delete_messages(ctx, [msg.message_id])
    background(later())


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


async def deliver_path(ctx, path, filename, screen, old_panel_id, keep=False):
    """Like deliver_file, for any file on disk (the month xlsx). keep=True for stored files."""
    clear_files_later(ctx)
    try:
        with open(path, "rb") as f:
            msg = await ctx.bot.send_document(OWNER, f, filename=filename)
    finally:
        if not keep and os.path.exists(path):
            os.remove(path)
    ctx.user_data.setdefault("files", []).append(msg.message_id)
    panel = await ctx.bot.send_message(OWNER, screen[0], reply_markup=screen[1])
    ctx.user_data["panel"] = panel.message_id
    if old_panel_id:
        background(delete_messages(ctx, [old_panel_id]))


async def deliver_bytes(ctx, raw, filename, screen, old_panel_id):
    """Like deliver_path, for bytes that are not on disk (a stored file from MongoDB)."""
    clear_files_later(ctx)
    msg = await ctx.bot.send_document(OWNER, raw, filename=filename)
    ctx.user_data.setdefault("files", []).append(msg.message_id)
    panel = await ctx.bot.send_message(OWNER, screen[0], reply_markup=screen[1])
    ctx.user_data["panel"] = panel.message_id
    if old_panel_id:
        background(delete_messages(ctx, [old_panel_id]))


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
    set_lang(ctx)
    for k in ("waiting", "guided", "pending", "pad", "cash_ui", "exp_from", "imports"):
        ctx.user_data.pop(k, None)
    old = ctx.user_data.get("panel")
    background(delete_messages(ctx, ctx.user_data.pop("files", []) + ([old] if old else [])))
    if not ctx.user_data.get("kb"):        # the two shortcut buttons at the bottom (sent once)
        await update.message.reply_text(tr("shortcuts"), reply_markup=SHORTCUTS)
        ctx.user_data["kb"] = True
    msg, _ = await asyncio.gather(update.message.reply_text(screen[0], reply_markup=screen[1]), tidy(update))
    ctx.user_data["panel"] = msg.message_id


async def home_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_lang(ctx)
    await open_panel(update, ctx, home_screen())


async def today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_lang(ctx)
    await open_panel(update, ctx, report_screen(start_session(ctx, today_iso())))


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_lang(ctx)
    await open_panel(update, ctx, history_screen(0))


async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_lang(ctx)
    await open_panel(update, ctx, month_screen(today_iso()[:7]))


async def keyboard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["kb"] = False            # send the shortcut buttons again
    set_lang(ctx)
    await open_panel(update, ctx, home_screen())


async def lang_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_lang(ctx)
    await open_panel(update, ctx, lang_screen())


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
    set_lang(ctx)
    cmd = q.data
    current = ctx.user_data.get("panel")
    if current and q.message.message_id < current:         # an OLDER menu higher up in the chat
        answer_later(q, tr("old_menu"))
        return background(delete_messages(ctx, [q.message.message_id]))
    ctx.user_data["panel"] = q.message.message_id
    if cmd != "act:generate" and not cmd.startswith("k:"):   # these answer later, with their own message
        answer_later(q, tr("sending_file") if cmd.startswith(("h:f:", "st:f:")) else None)
    if cmd != "act:generate" and not cmd.startswith("h:f:"):
        clear_files_later(ctx)                              # new task: the old file leaves the chat
    if not cmd.startswith(("g:", "w:", "exp:", "k:")) and cmd not in ("act:generate", "review"):
        for k in ("guided", "pending", "pad"):                     # leaving step-by-step / a warning
            ctx.user_data.pop(k, None)

    # ----- home and history -----
    if cmd.startswith("lang:"):
        lang = cmd.split(":", 1)[1]
        ctx.user_data["lang"] = lang if lang in ("en", "kh") else "en"
        set_lang(ctx)
        return await show(q, (tr("lang_title") + "\n\n"
                              + (tr("lang_set") if ctx.user_data["lang"] == "en" else tr("lang_set_kh")),
                              Markup([[Btn(tr("home_btn"), callback_data="home")]])))
    if cmd == "home":
        ctx.user_data.pop("waiting", None)
        return await show(q, home_screen())
    if cmd == "new":
        return await show(q, report_screen(start_session(ctx, today_iso())))
    if cmd == "newdate":
        ctx.user_data["waiting"] = ("newdate",)
        return await show(q, prompt_screen(tr("which_day"), tr("example_date"), "home",
                                           extra=tr("date_format") + "\n"))
    if cmd.startswith("hist:"):
        ctx.user_data.pop("waiting", None)
        return await show(q, history_screen(int(cmd.split(":")[1])))
    if cmd.startswith("mon:"):
        ctx.user_data.pop("waiting", None)
        ym = today_iso()[:7] if cmd[4:] == "cur" else cmd[4:]
        return await show(q, month_screen(ym))
    if cmd.startswith("monx:"):
        ym = cmd[5:]
        rows = month_rows(ym)
        if not rows:
            return await show(q, month_screen(ym, tr("no_month_reports")))
        path = os.path.join(tempfile.gettempdir(), f"month_{ym}.xlsx")
        monthly.build_xlsx(path, month_title(ym), rows)
        return await deliver_path(ctx, path, f"{ym[5:7]}-{ym[:4]}_Month_Report.xlsx", month_screen(ym), q.message.message_id)
    if cmd.startswith("h:"):
        _, action, iso = cmd.split(":", 2)
        data = history.load_report(iso)
        if action != "dy" and data is None:
            return await show(q, history_screen(0, tr("not_found")))
        if action == "v":                                   # open one report
            return await show(q, report_view(data, iso))
        if action == "f":                                   # send its file again
            return await deliver_file(ctx, complete(data), iso, report_view(data, iso), q.message.message_id)
        if action == "e":                                   # update (edit) it
            return await show(q, report_screen(start_session(ctx, iso, explicit=True)))
        if action == "d":                                   # ask before deleting
            return await show(q, (tr("delete_report", date=dmy(iso)),
                                  Markup([[Btn(tr("yes_delete"), callback_data=f"h:dy:{iso}"),
                                           Btn(tr("cancel"), callback_data=f"h:v:{iso}")]])))
        if action == "dy":                                  # delete it
            old = history.load_report(iso)
            if old:
                mark_undo(ctx, [("history", iso, {"data": old})], screen="history")
            history.delete_report(iso)
            return await show(q, add_undo(ctx, history_screen(0, tr("deleted", date=dmy(iso)))))

    # ----- storage, drafts and imports -----
    if cmd.startswith("stor:"):
        return await show(q, storage_screen(int(cmd.split(":")[1])))
    if cmd.startswith("st:v:"):
        iso = cmd.split(":", 2)[2]
        raw = storage.get_stored_report(iso)
        if raw is None:
            return await show(q, storage_screen(0, tr("file_gone")))
        fd, tmp = tempfile.mkstemp(prefix=f"stored_{iso}_", suffix=".xlsx")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
            d, data, warnings = importer.read_report(tmp, original_name=f"{iso}.xlsx")
        except importer.BadFile as e:
            return await show(q, storage_screen(0, e.message))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        text = import_body(data, dmy(iso))
        if warnings:
            text += "\n\n⚠️ " + "\n⚠️ ".join(esc(w) for w in warnings)
        return await show(q, (text, Markup([
            [Btn(tr("send_file"), callback_data=f"st:f:{iso}"), Btn(tr("delete"), callback_data=f"st:d:{iso}")],
            [Btn(tr("storage_back"), callback_data="stor:0")]])))
    if cmd.startswith("st:f:"):
        iso = cmd.split(":", 2)[2]
        raw = storage.get_stored_report(iso)
        if raw is None:
            return await show(q, storage_screen(0, tr("file_gone")))
        return await deliver_bytes(ctx, raw, file_name(iso), storage_screen(0), q.message.message_id)
    if cmd.startswith("st:d:"):
        iso = cmd.split(":", 2)[2]
        return await show(q, (tr("delete_stored", date=dmy(iso)),
                              Markup([[Btn(tr("yes_delete"), callback_data=f"st:dy:{iso}"),
                                       Btn(tr("cancel"), callback_data=f"st:v:{iso}")]])))
    if cmd.startswith("st:dy:"):
        iso = cmd.split(":", 2)[2]
        b64 = storage.stored_file_b64(iso)
        if b64:
            mark_undo(ctx, [("stored", iso, {"file_b64": b64})], screen="stored")
        storage.delete_stored_report(iso)
        return await show(q, add_undo(ctx, storage_screen(0, tr("deleted", date=dmy(iso)))))
    if cmd == "d:cont":
        return await show(q, report_screen(start_session(ctx, today_iso())))
    if cmd.startswith("d:cont:"):
        iso = cmd.split(":", 2)[2]
        return await show(q, report_screen(start_session(ctx, iso, explicit=True)))
    if cmd == "d:new":
        return await show(q, (tr("start_new_q"),
                              Markup([[Btn(tr("yes_start_new"), callback_data="d:newy"),
                                       Btn(tr("cancel"), callback_data="home")]])))
    if cmd == "d:newy":
        iso = today_iso()
        draft = storage.load_draft(iso)
        if draft:
            mark_undo(ctx, [("draft", iso, {"session": draft})], screen="report")
        s = start_session(ctx, iso, force_new=True)
        return await show(q, add_undo(ctx, report_screen(s, tr("started_new"))))
    if cmd.startswith("d:disc:"):
        iso = cmd.split(":", 2)[2]
        return await show(q, (tr("discard_q", date=dmy(iso)),
                              Markup([[Btn(tr("yes_discard"), callback_data=f"d:discy:{iso}"),
                                       Btn(tr("cancel"), callback_data="home")]])))
    if cmd.startswith("d:discy:"):
        iso = cmd.split(":", 2)[2]
        draft = storage.load_draft(iso)
        if draft:
            mark_undo(ctx, [("draft", iso, {"session": draft})], screen="home")
        storage.delete_draft(iso)
        return await show(q, add_undo(ctx, home_screen()))
    if cmd == "imp:save":
        imports = ctx.user_data.get("imports") or []
        items, saved_dates = [], []
        for imp in imports:
            if not imp["date"]:
                continue
            iso = imp["date"]
            old = history.load_report(iso)
            if old:
                items.append(("history", iso, {"data": old}))
            b64 = storage.stored_file_b64(iso)
            if b64:
                items.append(("stored", iso, {"file_b64": b64}))
            history.save_report(iso, imp["data"])
            storage.store_report(iso, imp["raw"])
            saved_dates.append(iso)
        if items:
            mark_undo(ctx, items, screen="report")
        ctx.user_data.pop("imports", None)
        ctx.user_data.pop("waiting", None)
        if not saved_dates:
            return await show(q, add_undo(ctx, import_screen(ctx, tr("no_date_given"))))
        s = start_session(ctx, saved_dates[-1], explicit=True)
        return await show(q, add_undo(ctx, report_screen(
            s, tr("import_saved", n=len(saved_dates), noun=noun(len(saved_dates), "file_one", "file_many")))))
    if cmd == "imp:cancel":
        ctx.user_data.pop("imports", None)
        ctx.user_data.pop("waiting", None)
        return await show(q, home_screen())

    # ----- filling the report -----
    s = get_session(ctx)
    data = s["data"]

    if cmd == "undo":                                       # put back what was just deleted
        u = ctx.user_data.pop("undo", None)
        if not u:
            return
        for it in reversed(u["items"]):
            payload = storage.restore_trash(it["name"])
            storage.delete_trash(it["name"])
            if payload is None:
                continue
            kind, iso = it["kind"], it["iso"]
            if kind == "history":
                history.save_report(iso, payload["data"])
            elif kind == "stored":
                storage.restore_stored(iso, payload["file_b64"])
            elif kind == "draft":
                storage.save_draft(payload["session"])
                s = start_session(ctx, iso, explicit=True)   # put the draft back in memory too
            elif kind == "clear":
                s["data"] = complete(copy.deepcopy(payload["data"]))
                save_draft_now(ctx, s)
        scr = {"history": lambda: history_screen(0, tr("restored")),
               "stored": lambda: storage_screen(0, tr("restored")),
               "draft": home_screen,
               "report": lambda: report_screen(s, tr("restored")),
               "home": home_screen}.get(u["screen"], home_screen)
        return await show(q, add_undo(ctx, scr()))

    if cmd == "m:main":
        ctx.user_data.pop("waiting", None)
        await show(q, report_screen(s))
    elif cmd == "g:start":
        await show(q, enter_step(ctx, s, 0))
    elif cmd in ("g:next", "g:back"):
        g = ctx.user_data.get("guided", 0)
        await show(q, enter_step(ctx, s, g + (1 if cmd == "g:next" else -1)))
    elif cmd == "g:stop":
        for k in ("guided", "waiting", "pending", "pad"):
            ctx.user_data.pop(k, None)
        await show(q, report_screen(s))
    elif cmd == "review":
        for k in ("guided", "waiting", "pending", "pad"):
            ctx.user_data.pop(k, None)
        await show(q, review_screen(s))
    elif cmd == "w:ok":                                     # keep the number that looked too big
        p = ctx.user_data.pop("pending", None)
        if not p:
            return await show(q, report_screen(s))
        kind, group, key, nums = p
        await show(q, commit_item(ctx, s, group, key, nums) if kind == "item" else commit_bar(ctx, s, nums))
    elif cmd == "w:no":                                     # change it: the keypad keeps what you typed
        p = ctx.user_data.pop("pending", None)
        if p:
            ctx.user_data["waiting"] = ("item", p[1], p[2]) if p[0] == "item" else ("bar",)
            ctx.user_data["pad"] = init_pad(s, ctx.user_data["waiting"], p[3])
            await show(q, pad_screen(ctx, s))
        else:
            await show(q, report_screen(s))
    elif cmd.startswith("cash:"):                           # open the cash check
        ctx.user_data.pop("waiting", None)
        ctx.user_data["cash_ui"] = {"step": 1, "from": cmd.split(":")[1]}
        await show(q, cash_screen(ctx, s))
    elif cmd.startswith("c:"):                              # a tap on the cash check screen
        ui = ctx.user_data.setdefault("cash_ui", {"step": 1})
        what = cmd.split(":")[1]
        if what == "noop":
            return
        if what == "done":
            ctx.user_data.pop("waiting", None)
            return await show(q, review_screen(s) if ui.get("from") == "review" else report_screen(s))
        if what in ("total", "open"):
            ctx.user_data["waiting"] = ({"total": ("cash_total",), "open": ("cash_open",)})[what]
            title, example, extra = {
                "total": (tr("total_counted"), tr("example_160"), tr("total_counted_extra")),
                "open": (tr("opening_btn"), tr("example_50"), tr("opening_extra"))}[what]
            return await show(q, prompt_screen(title, example, "c:back", extra=extra))
        if what == "back":
            ctx.user_data.pop("waiting", None)
            return await show(q, cash_screen(ctx, s))
        if cash_change(ctx, s, cmd):
            save_draft_now(ctx, s)
            ui["v"] = ui.get("v", 0) + 1
            async with pad_lock():                          # fast taps: draw only the newest numbers
                if ctx.user_data.get("cash_ui") is ui and ui.get("shown") != ui["v"] and not ctx.user_data.get("waiting"):
                    ui["shown"] = ui["v"]
                    await show(q, cash_screen(ctx, s))
    elif cmd.startswith("k:"):                              # a key on the keypad
        w, pad = ctx.user_data.get("waiting"), ctx.user_data.get("pad")
        if not pad or not w or w[0] not in ("item", "bar"):   # e.g. a double tap after Save: do nothing
            answer_later(q, tr("keypad_closed"))
            if tr("pick_field") in (q.message.text or ""):   # an old keypad still on screen
                await show(q, report_screen(s, tr("keypad_closed_choose")))
            return
        answer_later(q)
        if cmd == "k:s":
            screen = submit_numbers(ctx, s, [val(v) for v in pad["vals"]])
            async with pad_lock():                            # wait until earlier keypad drawings are done
                await show(q, screen)
        else:
            press_key(pad, cmd[2:])                         # the numbers change at once, in tap order
            schedule_draft_save(ctx, s)                     # debounced: do not write on every digit
            pad["v"] = pad.get("v", 0) + 1
            async with pad_lock():
                still_open = ctx.user_data.get("pad") is pad and ctx.user_data.get("waiting")
                if still_open and pad.get("shown") != pad["v"]:   # a later tap may have drawn it already
                    pad["shown"] = pad["v"]
                    await show(q, pad_screen(ctx, s))
    elif cmd in ("m:rooms", "m:tours"):
        ctx.user_data.pop("waiting", None)
        await show(q, group_screen(s, cmd.split(":")[1]))
    elif cmd.startswith("in:rooms:") or cmd.startswith("in:tours:"):
        _, group, key = cmd.split(":")
        ctx.user_data["waiting"] = ("item", group, key)
        ctx.user_data["pad"] = init_pad(s, ctx.user_data["waiting"])
        await show(q, pad_screen(ctx, s))
    elif cmd == "in:bar":
        ctx.user_data["waiting"] = ("bar",)
        ctx.user_data["pad"] = init_pad(s, ctx.user_data["waiting"])
        await show(q, pad_screen(ctx, s))
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
            note = tr("removed", name=gone['name'], amt=n(gone['amount']))
            save_draft_now(ctx, s)
        ctx.user_data["waiting"] = ("expense",)
        await show(q, expense_view(ctx, s, note))
    elif cmd.startswith("exp:") and cmd != "exp:undo":      # edit the expenses
        parts = cmd.split(":")
        what, arg = parts[1], (parts[2] if len(parts) > 2 else "")
        items = data["expenses"]
        if what == "list":
            if arg != "keep":
                ctx.user_data["exp_from"] = arg            # where Back goes: cash | main | guided
            ctx.user_data.pop("waiting", None)
            return await show(q, expense_list_screen(s))
        if what == "back":
            src = ctx.user_data.get("exp_from", "main")
            ctx.user_data.pop("waiting", None)
            if src == "cash":
                ctx.user_data.setdefault("cash_ui", {"step": 1})
                return await show(q, cash_screen(ctx, s))
            ctx.user_data["waiting"] = ("expense",)
            return await show(q, expense_view(ctx, s) if src == "guided" else expense_screen(s))
        if what in ("q", "yday"):                           # quick expenses
            ctx.user_data["waiting"] = ("expense",)
            note = ""
            if what == "q":
                freq = frequent_expenses(s)
                k = int(arg) if arg.isdigit() else -1
                if 0 <= k < len(freq) and (len(parts) < 4 or str(name_key(freq[k][0])) == parts[3]):
                    items.append({"name": freq[k][0], "amount": freq[k][1]})
                    note = tr("added", name=freq[k][0], amt=n(freq[k][1])) + tr("added_hint")
                else:
                    note = tr("list_changed")
            else:
                iso, old = last_expenses(s)
                if old and not items:
                    items.extend(copy.deepcopy(old))
                    note = tr("added_from", n=len(old), noun=noun(len(old), "report_one", "report_many"),
                              date=dmy(iso)[:5])
            save_draft_now(ctx, s)
            return await show(q, expense_view(ctx, s, note))
        i = int(arg) if arg.isdigit() else -1
        if not 0 <= i < len(items):                         # the list changed: show it again
            return await show(q, expense_list_screen(s, tr("expense_gone")))
        ctx.user_data.pop("waiting", None)
        if what == "item":
            await show(q, expense_item_screen(s, i))
        elif what == "tog":
            items[i]["cash"] = not items[i].get("cash", True)
            save_draft_now(ctx, s)
            await show(q, expense_item_screen(s, i, tr("changed")))
        elif what in ("ename", "eamt"):
            ctx.user_data["waiting"] = ("exp_name" if what == "ename" else "exp_amount", i)
            await show(q, prompt_screen(tr("new_name") if what == "ename" else tr("new_amount"),
                                        tr("example_tea") if what == "ename" else tr("example_12"), f"exp:item:{i}",
                                        extra=tr("now", value=esc(items[i]['name']) if what == 'ename'
                                                 else n(items[i]['amount'])) + "\n"))
        elif what == "delq":
            e = items[i]
            await show(q, (tr("delete_expense") + f"\n{esc(e['name'])}: {n(e['amount'])}",
                           Markup([[Btn(tr("yes_delete"), callback_data=f"exp:delyes:{i}"),
                                    Btn(tr("cancel"), callback_data=f"exp:item:{i}")]])))
        elif what == "delyes":
            gone = items.pop(i)
            save_draft_now(ctx, s)
            await show(q, expense_list_screen(s, tr("deleted_item", name=gone['name'], amt=n(gone['amount']))))
    elif cmd == "act:reset":
        await show(q, (tr("clear_q"),
                       Markup([[Btn(tr("yes_clear"), callback_data="act:resety"),
                                Btn(tr("no"), callback_data="m:main")]])))
    elif cmd == "act:resety":
        mark_undo(ctx, [("clear", s["date"], {"data": copy.deepcopy(data)})], screen="report")
        s["data"] = fresh()
        ctx.user_data.pop("waiting", None)
        save_draft_now(ctx, s)
        await show(q, add_undo(ctx, report_screen(s, tr("cleared"))))
    elif cmd == "act:generate":
        try:
            history.save_report(s["date"], data)            # saved in History (replaces the same day)
            storage.delete_draft(s["date"])                 # the report is finished: no draft any more
            s["saved"] = True
            for k in ("guided", "waiting", "pending", "pad"):
                ctx.user_data.pop(k, None)
            await deliver_file(ctx, data, s["date"], done_screen(s), q.message.message_id)
            await q.answer(tr("saved_history"))
        except Exception:
            logging.exception("could not make the report")
            await q.answer(tr("could_not_make"), show_alert=True)


# ---------- typed messages ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_text(update, ctx, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A voice message: write it down, then fill it in like typed text."""
    set_lang(ctx)
    msg = update.message
    voice = msg.voice or msg.audio or msg.video_note
    if voice is None:
        return
    await tidy(msg)
    clear_files_later(ctx)
    try:
        raw = bytes(await (await voice.get_file()).download_as_bytearray())
    except Exception:
        return await flash(ctx, tr("voice_fail"))
    if len(raw) > speech.MAX_VOICE:
        return await flash(ctx, tr("voice_too_big"))
    tmp = os.path.join(tempfile.gettempdir(), f"voice_{datetime.now(TZ).strftime('%Y%m%d%H%M%S%f')}.ogg")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        text = speech.transcribe(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if not text:
        return await flash(ctx, tr("voice_unclear"))
    await handle_text(update, ctx, speech.spoken_numbers(text))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text):
    set_lang(ctx)
    if text.strip() in SHORTCUT_TEXTS:                      # the buttons at the bottom
        return await {"📝 Today": today_cmd, "📚 History": history_cmd, "📊 Month": month_cmd}[text.strip()](update, ctx)
    w = ctx.user_data.get("waiting")
    clear_files_later(ctx)

    if w and w[0] == "newdate":
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", text)
        try:
            day = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except (AttributeError, ValueError):
            return await respond(update, ctx, prompt_screen(
                tr("which_day"), tr("example_date"), "home", tr("bad_date"), tr("date_format") + "\n"))
        return await respond(update, ctx, report_screen(start_session(ctx, day.strftime("%Y-%m-%d"), explicit=True)))

    if w and w[0] == "importdate":                           # the date of an imported file
        imports = ctx.user_data.get("imports") or []
        i = w[1] if len(w) > 1 else 0
        if not imports or not 0 <= i < len(imports):
            ctx.user_data.pop("waiting", None)
            return await respond(update, ctx, import_screen(ctx))
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", text)
        try:
            day = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except (AttributeError, ValueError):
            return await respond(update, ctx, import_screen(ctx, tr("bad_date")))
        imports[i]["date"] = day.strftime("%Y-%m-%d")
        next_missing_date(ctx)
        return await respond(update, ctx, import_screen(ctx))

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
        save_draft_now(ctx, s)
        got = parsed["rooms"] or parsed["tours"] or parsed["bar"] or parsed["discount"] or parsed["expenses"]
        note = tr("saved_from_msg") if got else tr("tap_button")
        if got and unknown:
            note += "\n" + tr("not_understood", items="; ".join(unknown))
        await respond(update, ctx, report_screen(s, note))

    elif w[0] in ("item", "bar"):
        nums = numbers(text)
        if not nums:
            if "pad" not in ctx.user_data:
                ctx.user_data["pad"] = init_pad(s, w)
            return await respond(update, ctx, pad_screen(ctx, s, tr("type_numbers")))
        await respond(update, ctx, submit_numbers(ctx, s, nums))

    elif w[0] in ("exp_name", "exp_amount"):
        _, i = w
        items = data["expenses"]
        if not 0 <= i < len(items):
            ctx.user_data.pop("waiting", None)
            return await respond(update, ctx, expense_list_screen(s, tr("expense_gone")))
        if w[0] == "exp_name":
            name = text.strip()[:40]
            if not name:
                return await respond(update, ctx, prompt_screen(tr("new_name"), tr("example_tea"), f"exp:item:{i}",
                                                                tr("type_name")))
            items[i]["name"] = name
        else:
            nums = numbers(text)
            if not nums or nums[0] < 0:
                return await respond(update, ctx, prompt_screen(tr("new_amount"), tr("example_12"), f"exp:item:{i}",
                                                                tr("type_number")))
            items[i]["amount"] = nums[0]
        save_draft_now(ctx, s)
        ctx.user_data.pop("waiting", None)
        await respond(update, ctx, expense_item_screen(s, i, tr("changed")))

    elif w[0] in ("cash_total", "cash_open"):
        nums = numbers(text)
        if not nums or nums[0] < 0:
            return await respond(update, ctx, prompt_screen(tr("cash_check_title"), tr("example_160"), "c:back",
                                                            tr("type_number")))
        cc = data.setdefault("cash_check", {})
        if w[0] == "cash_total":
            cc["typed"] = nums[0]
            note = tr("counted_total", amt=n(nums[0]))
        else:
            cc["opening"] = nums[0]
            note = tr("opening_saved", amt=n(nums[0]))
        save_draft_now(ctx, s)
        ctx.user_data.pop("waiting", None)
        ctx.user_data.setdefault("cash_ui", {"step": 1})
        await respond(update, ctx, cash_screen(ctx, s, note))

    elif w[0] == "discount":
        _, nums, rest = split_item(text)
        if not nums:
            return await respond(update, ctx, discount_prompt(tr("type_amount_first")))
        await respond(update, ctx, commit_discount(ctx, s, nums[0], " ".join(rest)))

    elif w[0] == "expense":
        words, nums, _ = split_item(text)
        if not words or not nums:
            return await respond(update, ctx, expense_view(ctx, s, tr("type_name_amount")))
        data["expenses"].append({"name": " ".join(words), "amount": nums[0]})
        save_draft_now(ctx, s)
        await respond(update, ctx, expense_view(ctx, s, tr("added", name=" ".join(words), amt=n(nums[0]))))


# ---------- evening reminder ----------
async def reminder(ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data if ctx.user_data is not None else {}
    set_lang(ctx)
    if history.load_report(today_iso()) is not None:      # today's report is already made: no need to bother you
        return
    old = ud.get("panel")
    background(delete_messages(ctx, ud.pop("files", []) + ([old] if old else [])))
    text, markup = home_screen()
    at = reminder_time().strftime("%I:%M %p").lstrip("0")
    msg = await ctx.bot.send_message(OWNER, tr("reminder", at=at) + "\n\n" + text,
                                     reply_markup=markup)
    ud["panel"] = msg.message_id


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Short, clear messages instead of long error traces."""
    err = ctx.error
    if isinstance(err, Conflict):      # Telegram: two copies of this bot are asking for messages
        last = ctx.application.bot_data.get("conflict_at", -1e9)
        if monotonic() - last > 3600:  # warn at most once an hour
            ctx.application.bot_data["conflict_at"] = monotonic()
            logging.error("CONFLICT: another copy of this bot is running (Render, another terminal, "
                          "another computer). Stop the other copy, or the buttons will act strangely.")
            try:
                await ctx.bot.send_message(OWNER, tr("conflict"))
            except Exception:
                pass
        return
    if isinstance(err, NetworkError):  # includes TimedOut: the internet is slow, it tries again by itself
        logging.warning("Network problem (the bot tries again): %s", err)
        return
    logging.error("Unexpected error", exc_info=err)


async def keep_warm(ctx: ContextTypes.DEFAULT_TYPE):
    """Every 40 seconds: two tiny requests keep two connections to Telegram ready (one for the edit,
    one for the button answer). It also tells you in the log if Telegram is slow."""
    t0 = perf_counter()
    try:
        await asyncio.gather(ctx.bot.get_me(), ctx.bot.get_me())
    except Exception as e:
        logging.warning("Keep-warm request failed (%s)", e)
        return
    took = perf_counter() - t0
    ctx.application.bot_data["ping"] = took
    if took > 1.5:
        logging.warning("Telegram is slow right now: %.1fs for a tiny request. This is your internet or Telegram, "
                        "not the bot.", took)


def self_url():
    """The public address of this bot on Render (Render sets RENDER_EXTERNAL_URL). Or write SELF_URL in .env."""
    return (os.getenv("SELF_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


async def self_ping(ctx: ContextTypes.DEFAULT_TYPE):
    """On Render: the bot visits its own public address, so Render sees traffic and does not put it to sleep.
    (Asking localhost would not count: the visit must come in from outside.)"""
    url = self_url()
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url + "/")
        if r.status_code >= 400:
            logging.warning("Self-ping got HTTP %s from %s", r.status_code, url)
    except Exception as e:
        logging.warning("Self-ping failed (%s)", e)


async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/ping: how fast is the line between the bot and Telegram?"""
    t0 = perf_counter()
    await ctx.bot.get_me()
    took = perf_counter() - t0
    verdict = tr("fast") if took < 0.6 else (tr("ok") if took < 1.5 else tr("slow"))
    storage_line = {"mongo": "MongoDB (connected)",
                    "unreachable": "MongoDB (UNREACHABLE - using local files)",
                    "files": "local files"}[db.status()]
    await asyncio.gather(tidy(update), flash(
        ctx, tr("pong", t=f"{took:.2f}", verdict=verdict)
        + f"\n<i>version {VERSION}</i>\nstorage: {storage_line}", seconds=10))


async def unknown_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.gather(tidy(update), flash(ctx, tr("unknown_cmd"), seconds=6))


async def backup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/backup: all your reports and stored files in one zip file."""
    raw, count = storage.backup_zip()
    await tidy(update)
    if not count:
        return await flash(ctx, tr("no_backup"))
    await ctx.bot.send_document(OWNER, document=raw, filename=f"hostel-backup-{today_iso()}.zip",
                                caption=tr("backup_of", n=count, noun=noun(count, "report_one", "report_many"))
                                + " " + tr("keep_safe"))


async def restore_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.gather(tidy(update), flash(ctx, tr("restore_help"), seconds=15))


async def import_document(update, ctx, doc):
    """Read an uploaded .xlsx report and show what the bot understood."""
    name = doc.file_name or ""
    if "/" in name or "\\" in name or ".." in name:      # never trust the file name
        return await flash(ctx, tr("name_not_allowed"))
    if (doc.file_size or 0) > importer.MAX_UPLOAD:
        return await flash(ctx, tr("too_big"))
    try:
        raw = bytes(await (await doc.get_file()).download_as_bytearray())
    except Exception:
        return await flash(ctx, tr("download_fail"))
    if len(raw) > importer.MAX_UPLOAD:
        return await flash(ctx, tr("too_big"))
    tmp = os.path.join(tempfile.gettempdir(), f"upload_{datetime.now(TZ).strftime('%Y%m%d%H%M%S%f')}.xlsx")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        d, data, warnings = importer.read_report(tmp, original_name=doc.file_name)
    except importer.BadFile as e:
        return await flash(ctx, e.message, seconds=12)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    ctx.user_data.setdefault("imports", []).append(
        {"date": d, "data": data, "warnings": warnings, "raw": raw, "name": doc.file_name})
    next_missing_date(ctx)                           # ask for a date only if one is missing
    await refresh(ctx, import_screen(ctx))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A backup zip, or a report .xlsx to read back."""
    doc = update.message.document
    await tidy(update)
    name = (doc.file_name or "").lower()
    if name.endswith(".zip"):
        if (doc.file_size or 0) > 5_000_000:
            return await flash(ctx, tr("only_zip"))
        try:
            raw = bytes(await (await doc.get_file()).download_as_bytearray())
        except Exception:
            return await flash(ctx, tr("download_fail"))
        result = storage.restore_zip(raw)
        if result is None:
            return await flash(ctx, tr("bad_zip"))
        added, skipped, ignored, files_added, files_skipped = result
        await flash(ctx, tr("backup_read", added=added, noun=noun(added, "report_one", "report_many"), skipped=skipped)
                         + (tr("ignored", n=ignored) if ignored else "")
                         + (tr("files_added", added=files_added, skipped=files_skipped)
                            if (files_added or files_skipped) else "")
                         + ".", seconds=15)
    elif name.endswith(".xlsx"):
        await import_document(update, ctx, doc)
    else:
        await flash(ctx, tr("only_zip_xlsx"))


async def weekly_backup(ctx: ContextTypes.DEFAULT_TYPE):
    """Every Sunday evening: a fresh backup. The previous weekly backup message is removed."""
    set_lang(ctx)
    raw, count = storage.backup_zip()
    if not count:
        return
    msg = await ctx.bot.send_document(OWNER, document=raw, filename=f"hostel-backup-{today_iso()}.zip",
                                      caption=tr("weekly_backup", n=count, noun=noun(count, "report_one", "report_many"))
                                      + " " + tr("keep_safe"))
    old = ctx.application.bot_data.get("backup_msg")
    ctx.application.bot_data["backup_msg"] = msg.message_id
    if old:
        await delete_messages(ctx, [old])


async def post_init(app: Application):
    """The blue "Menu" button next to the typing box. If Telegram is slow now, the bot still starts."""
    backend = db.choose_backend()
    if backend == "mongo":
        logging.info("Storage: MongoDB (connected)")
    elif db.configured():
        logging.warning("MongoDB is configured but unreachable; using local files")
        try:
            await app.bot.send_message(
                OWNER,
                "⚠️ MongoDB is unreachable, so the bot is using local files. "
                "Your data will NOT survive a redeploy.\n"
                "Fix: in MongoDB Atlas → Network Access, add 0.0.0.0/0 "
                "(Allow Access from Anywhere), then redeploy.")
        except Exception:
            pass
    else:
        logging.info("Storage: local files (%s)", history.DATA_DIR)
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "🏠 Home"),
            BotCommand("today", "📝 Today's report"),
            BotCommand("history", "📚 History"),
            BotCommand("month", "📊 Month summary"),
            BotCommand("backup", "🗄 Backup file"),
            BotCommand("ping", "🏓 Speed test"),
            BotCommand("lang", "🌐 Language"),
        ])
    except Exception as e:
        logging.warning("Could not set the Menu button now (%s). The bot works anyway.", e)


def make_request():
    """The connection to Telegram.
    - It stays open for 2 minutes of silence (the default is 5 seconds), so a tap does not need a new
      secure connection first. That setup alone can cost you a second or more.
    - IPv4 only by default: a broken IPv6 route can make every click slow. (Turn off: FORCE_IPV4=0 in .env)"""
    transport = httpx.AsyncHTTPTransport(
        local_address="0.0.0.0" if os.getenv("FORCE_IPV4", "1") == "1" else None,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=120))
    return HTTPXRequest(connection_pool_size=16, connect_timeout=20, read_timeout=30,
                        write_timeout=30, pool_timeout=10, httpx_kwargs={"transport": transport})


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


def build_app():
    """Build the Application (used by main() and by the tests)."""
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
    app.add_handler(CommandHandler("ping", ping_cmd, filters=only_me))
    app.add_handler(CommandHandler("month", month_cmd, filters=only_me))
    app.add_handler(CommandHandler("backup", backup_cmd, filters=only_me))
    app.add_handler(CommandHandler("restore", restore_cmd, filters=only_me))
    app.add_handler(CommandHandler("lang", lang_cmd, filters=only_me))
    app.add_handler(MessageHandler(only_me & filters.COMMAND, unknown_cmd))     # any other command
    app.add_handler(MessageHandler(only_me & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(only_me & (filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE), on_voice))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_daily(reminder, time=reminder_time(), user_id=OWNER, chat_id=OWNER)
    app.job_queue.run_repeating(keep_warm, interval=40, first=5)
    if os.getenv("PORT") and self_url():             # on Render: keep the free web service awake by itself
        try:
            minutes = min(max(int(os.getenv("SELF_PING_MINUTES", "5")), 1), 14)       # must be under 15
        except ValueError:
            minutes = 5
        app.job_queue.run_repeating(self_ping, interval=minutes * 60, first=60)
        print(f"Self-ping every {minutes} min to {self_url()}")
    app.job_queue.run_daily(weekly_backup, time=time(22, 0, tzinfo=TZ), days=(0,))   # Sunday 22:00
    return app


def main():
    start_health_server()
    app = build_app()
    print(f"Bot is running (version {VERSION}). Daily reminder at {reminder_time():%H:%M} (Cambodia time)")
    app.run_polling(bootstrap_retries=-1)   # if Telegram cannot be reached at start, keep trying


if __name__ == "__main__":
    main()