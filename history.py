# history.py - saves every report you make, so you can open, update or delete it later
import json
from datetime import datetime
from pathlib import Path

HISTORY_DIR = Path(__file__).parent / "history"   # one small file per day: 2026-10-26.json


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
