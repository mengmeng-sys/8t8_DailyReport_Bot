# MANUAL.md - Storage, drafts and Undo

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

- Send the bot an `.xlsx` report file (any time). The bot:
  1. Reads it (openpyxl, `data_only=True`, nothing is executed).
  2. Shows a preview (same aligned table as a report, plus revenue, expenses and
     the date).
  3. Reads the date from cell **B23**; if it is missing, tries the file name
     (`dd-mm-yyyy`); if still missing, asks you to type the date.
  4. Recomputes the totals from the cells and compares them with the cached
     totals in the file (C27 revenue, B44 expenses). If they differ, it warns.
  5. Asks **✅ Save** (or **🔁 Replace** if the day already exists).
- On Save it writes the day to History **and** keeps the original file in
  `DATA_DIR/storage/reports/YYYY-MM-DD.xlsx`.
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

- `storage.py` - drafts, stored files, trash (undo), storage backup/restore.
- `importer.py` - `read_report(path) -> (date, data, warnings)`.
- `tests/` - pytest tests (fake Telegram, no internet).

## 6. Try it by hand in Telegram (about 5 minutes)

1. **Draft survives Home.** Send `/start`, tap **▶️ Start today's report**,
   tap **🛏 Rooms** → **Pool view**, type `40 0 20`. Tap **🏠 Home**, then
   **▶️ Start today's report** again: the numbers are still there and Home says
   "Draft in progress".
2. **Undo.** On the report screen tap **🗑 Clear all** → **Yes, clear**. The
   numbers are gone and an **↩️ Undo** button appears. Tap it: the numbers come
   back.
3. **Read a report.** Make a report and confirm it, then send that `.xlsx` file
   back to the bot. It shows a preview; tap **✅ Save**. Open **🗂 Storage**:
   the file is listed. Tap it to see the preview, **📥 Send file**, and
   **🗑 Delete** (asks first).
4. **Replace safely.** Send the same file again: the bot says the day already
   exists and offers **🔁 Replace**. Tap it, then **↩️ Undo** to get the old one
   back.
5. **Backup.** Send `/backup`, forward the zip to Saved Messages. Delete a day
   in History, then send the zip back: the missing day is added, existing days
   are not changed.
