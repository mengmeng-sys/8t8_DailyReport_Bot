# MANUAL.md - Storage, drafts, Undo, language and voice

## Voice input (type or say)

You can fill the report by **typing** or by **talking**. Send a voice message and the
bot writes down what you say and fills it in — with the same checks as typing
(the "did you type an extra digit?" warning, the same flow).

- Say: `pool forty zero twenty` → Pool view cash 40, card 0, aba 20
- Say: `bar fifty ten five` → Bar 50 / 10 / 5
- Say: `water twelve` → an expense "water" for 12
- Say: `discount five long stay` → a discount of 5

Spoken numbers are turned into digits: `forty` → 40, `oh`/`zero` → 0,
`twenty one` → 21, `fifty ten five` → 50, 10, 5.

**Setup (one time):** `pip install faster-whisper` (already in
`requirements.txt`). The first voice message downloads a small model (~150 MB,
model `base` by default). Set `STT_MODEL=tiny` in `.env` for a smaller/faster
model. The model is cached in `DATA_DIR/models`, so it stays on the persistent
disk on Render. It works offline and costs nothing.

If the bot cannot understand you, it says so and asks you to repeat or type.

## Keep your data safe (MongoDB)

On Render's free tier the disk is wiped every time the service restarts or you
redeploy, so anything written to disk (History, drafts, stored files) is lost.
The bot can store everything in **MongoDB** instead, which survives redeploys.

The bot picks the storage automatically:

- **`MONGODB_URI` set** → everything is stored in MongoDB (reports, drafts,
  undo trash, and the files you sent).
- **`MONGODB_URI` empty** → the bot uses the local `history/` and
  `storage/` folders (good for your computer and for the tests).

### Set it up (about 10 minutes, free)

1. Create a free cluster at **[mongodb.com/atlas](https://www.mongodb.com/atlas)**
   (M0 free tier is enough).
2. **Database Access** → add a database user (remember the password).
3. **Network Access** → add `0.0.0.0/0` (allow anywhere). Render's IP changes on
   every deploy, so a fixed IP does not work. This is safe enough for a small
   private database protected by a username and password.
4. **Database** → **Connect** → copy the **connection string**. It looks like:
   `mongodb+srv://myuser:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`
5. In your **Render dashboard** → the bot service → **Environment**, add:
   - `MONGODB_URI` = the connection string (replace `<password>` with the real
     password, and URL-encode any `@ : / ? #` characters in it)
   - `MONGODB_DB` = `hostel_bot` (optional, this is the default)
6. **Deploy** the service again.

Send `/ping`: the answer now ends with `storage: MongoDB (connected)`. From then
on, every report, draft and stored file is kept in MongoDB, so a redeploy no
longer loses anything. `/backup` still works and produces the same zip, so you
can keep a copy in Telegram Saved Messages.

> If `/ping` says `MongoDB (UNREACHABLE)`, check the connection string, the
> database user password, and the Network Access list.

### Troubleshooting: `SSL handshake failed ... TLSV1_ALERT_INTERNAL_ERROR`

This exact error means **Atlas refused the connection because the IP address is
not on the Network Access allowlist** (Atlas rejects the TLS handshake for
non-allow-listed IPs). Fix it:

1. Atlas → **Network Access** → **Add IP Address** → **Allow Access from
   Anywhere** (`0.0.0.0/0`).
2. Wait until the entry says **Active** (this can take a few minutes).
3. Redeploy the Render service and send `/ping` again.

While Atlas is unreachable, the bot does **not** crash: it logs a warning and
uses the local files instead, and `/ping` says
`MongoDB (UNREACHABLE - using local files)`. Data written in that state is not
safe across a redeploy, so fix the allowlist as soon as you can.

## 0. Language (KH / EN)

The bot speaks **English** and **Khmer**. Switch with the `/lang` command or the
**🌐 Language** button on Home, then tap **🇬🇧 English** or **🇰🇭 ភាសាខ្មែរ**.
The choice is remembered for your chat. The room and tour names (Pool view,
Killing Field, …) stay in English because they are your business names.

The bot now never loses an unfinished report, can read a report `.xlsx` you send
it, and keeps the files you send in a **Storage** folder.

## 1. Drafts (never lose an unfinished report)

- After **every** change (keypad save, typed numbers, discount, expenses, cash
  check, Clear all) the bot writes a **draft** to
  `DATA_DIR/storage/drafts/YYYY-MM-DD.json`. It writes atomically (temp file,
  then rename), and keypad digits are written after a short pause (debounce).
- `Start`, `/today` and the `📝 Today` shortcut now **resume** a draft instead of
  throwing it away.
- Home shows the draft clearly:
  - `📝 Draft in progress: 26/10/2026 · revenue 235 (not saved yet)` with
    **▶️ Continue draft** and **🆕 Start new**.
  - `🕓 Unfinished draft from 29/10` with **▶️ Continue 29/10** and **🗑 Discard**.
- Drafts from earlier days stay until you finish or discard them (they do not
  disappear at midnight).
- When you confirm a report (✅ Confirm → xlsx), that day's draft is deleted.

## 2. Destructive actions ask first, and can be undone

Clear all, Start new over a draft, Replace a day, Discard draft and Delete all
show a confirmation screen. Before the action, the data is copied to
`DATA_DIR/storage/trash/` and the bot shows an **↩️ Undo** button right after.
Undo puts the data back. Trash copies are removed after 7 days.

## 3. Storage (read a report you send)

- Send the bot an `.xlsx` report file (any time). You can send **several files
  one after another**; the bot keeps them in a batch and the screen updates to
  show them all. Tap **✅ Save all** when you have sent every file.
- For each file the bot:
  1. Reads it (openpyxl, `data_only=True`, nothing is executed).
  2. Shows a preview (same aligned table as a report, plus revenue, expenses and
     the date).
  3. Reads the date from cell **B23**; if it is missing, tries the file name
     (`dd-mm-yyyy`); if still missing, asks you to type the date.
  4. Recomputes the totals from the cells and compares them with the cached
     totals in the file (C27 revenue, B44 expenses). If they differ, it warns.
  5. When you tap **Save all**, writes every day to History **and** keeps the
     original files in `DATA_DIR/storage/reports/YYYY-MM-DD.xlsx`. Days that
     already exist are replaced (the old version goes to trash, with Undo).
- The **🗂 Storage** screen (on Home and in History) lists the stored files.
  Tap one for **👁 Open (preview)**, **📥 Send file** and **🗑 Delete**.

### Upload safety

- Only `.xlsx` (`.xls`, `.xlsm`, `.txt`, `.csv` are rejected).
- Max 2 MB per file; the uncompressed size is checked (max 20 MB) so zip bombs
  are refused.
- External links are ignored.
- Numbers must be numeric and not negative; text is limited to 40 characters and
  control characters are removed.
- Broken or password-protected files give a friendly message; nothing is saved.
- The file name is never used for a path (the file is stored by its date).

## 4. Backup and restore

`/backup` now includes **both** the History JSON files and the files in
`storage/reports/`. Restore (send the `.zip` back) adds missing days and missing
stored files, and **never** overwrites a day or a file that already exists.

## 5. New files

- `db.py` - MongoDB connection (used only when `MONGODB_URI` is set).
- `storage.py` - drafts, stored files, trash (undo), storage backup/restore.
- `importer.py` - `read_report(path) -> (date, data, warnings)`.
- `tests/` - pytest tests (fake Telegram, no internet; MongoDB tests use mongomock).

## 6. Try it by hand in Telegram (about 5 minutes)

1. **Draft survives Home.** Send `/start`, tap **▶️ Start today's report**,
   tap **🛏 Rooms** → **Pool view**, type `40 0 20`. Tap **🏠 Home**, then
   **▶️ Start today's report** again: the numbers are still there and Home says
   "Draft in progress".
2. **Undo.** On the report screen tap **🗑 Clear all** → **Yes, clear**. The
   numbers are gone and an **↩️ Undo** button appears. Tap it: the numbers come
   back.
3. **Read a report.** Make a report and confirm it, then send that `.xlsx` file
   back to the bot. It shows a preview; tap **✅ Save all**. To read several
   reports, send 3 or 4 files one after another: the screen shows "3 files" and
   grows as you send; then tap **✅ Save all**. Open **🗂 Storage**: the files
   are listed. Tap one to see the preview, **📥 Send file**, and **🗑 Delete**
   (asks first).
4. **Replace safely.** Send the same file again: the bot says the day already
   exists and offers **🔁 Replace**. Tap it, then **↩️ Undo** to get the old one
   back.
5. **Backup.** Send `/backup`, forward the zip to Saved Messages. Delete a day
   in History, then send the zip back: the missing day is added, existing days
   are not changed.
