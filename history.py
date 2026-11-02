# history.py - saves every report you make, so you can open, update or delete it later
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
# DATA_DIR = where your data is kept. On your computer: leave it empty (the bot folder is used).
# On Render: set DATA_DIR=/var/data (the persistent disk), or History is lost on every restart.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
HISTORY_DIR = DATA_DIR / "history"   # one small file per day: 2026-10-26.json


def _path(iso):
    return HISTORY_DIR / f"{iso}.json"


def save_report(iso, data):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _path(iso).write_text(json.dumps({"date": iso, "data": data}, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def load_report(iso):
    try:
        return json.loads(_path(iso).read_text(encoding="utf-8"))["data"]
    except (OSError, ValueError, KeyError):
        return None


def delete_report(iso):
    p = _path(iso)
    if p.exists():
        p.unlink()
        return True
    return False


def list_reports():
    """Newest first, as 'YYYY-MM-DD' texts."""
    if not HISTORY_DIR.exists():
        return []
    days = []
    for p in HISTORY_DIR.glob("*.json"):
        try:
            datetime.strptime(p.stem, "%Y-%m-%d")
            days.append(p.stem)
        except ValueError:
            pass
    return sorted(days, reverse=True)


_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def backup_zip():
    """All saved reports in one zip file. Returns (bytes, number of reports)."""
    days = list_reports()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for iso in days:
            z.write(_path(iso), arcname=f"{iso}.json")
    return buf.getvalue(), len(days)


def restore_zip(raw):
    """Add the reports of a backup zip. A day that already exists is NEVER changed.
    Only files named like 2026-10-26.json are read. Returns (added, skipped, ignored), or None if it is not a zip."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    added = skipped = ignored = 0
    with z:
        infos = z.infolist()
        if len(infos) > 5000:
            return None
        for info in infos:
            if info.is_dir():
                continue
            if not _NAME.match(info.filename) or info.file_size > 1_000_000:
                ignored += 1
                continue
            iso = info.filename[:-5]
            try:
                datetime.strptime(iso, "%Y-%m-%d")
                data = json.loads(z.read(info).decode("utf-8"))["data"]
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                ignored += 1
                continue
            if _path(iso).exists():
                skipped += 1
                continue
            save_report(iso, data)
            added += 1
    return added, skipped, ignored