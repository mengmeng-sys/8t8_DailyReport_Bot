# storage.py - drafts, stored report files, trash (undo) and storage backup.
# Everything lives under DATA_DIR, so a persistent disk keeps it all.
import base64
import io
import json
import os
import re
import zipfile
from datetime import datetime, timedelta

import history

TRASH_DAYS = 7  # keep undo copies for this many days


def _dir(name):
    return history.DATA_DIR / "storage" / name


def _ensure(name):
    d = _dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- drafts (an unfinished report) ----------
def draft_path(iso):
    return _dir("drafts") / f"{iso}.json"


def save_draft(session):
    """Atomically write the report that is being filled."""
    d = _ensure("drafts")
    payload = {
        "date": session["date"],
        "explicit": bool(session.get("explicit")),
        "saved": bool(session.get("saved")),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": session.get("data") or {},
    }
    tmp = d / (f"{session['date']}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, draft_path(session["date"]))
    return draft_path(session["date"])


def load_draft(iso):
    p = draft_path(iso)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d.get("data"), dict):
        return None
    d.setdefault("explicit", False)
    d.setdefault("saved", False)
    return d


def delete_draft(iso):
    p = draft_path(iso)
    if p.exists():
        p.unlink()
        return True
    return False


def list_drafts(exclude=None):
    """All drafts, newest first: [{date, explicit, saved, updated_at, data}]."""
    d = _dir("drafts")
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.json"):
        iso = p.stem
        if exclude and iso == exclude:
            continue
        r = load_draft(iso)
        if r:
            out.append(r)
    return sorted(out, key=lambda r: r["date"], reverse=True)


# ---------- trash (undo a destructive action) ----------
def trash(kind, iso, payload):
    """Copy data to trash before a destructive action. Returns the file name."""
    d = _ensure("trash")
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    name = f"{ts}_{kind}_{iso}.json"
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps({"kind": kind, "iso": iso, "payload": payload}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, d / name)
    return name


def restore_trash(name):
    p = _dir("trash") / name
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("payload")
    except (OSError, ValueError):
        return None


def delete_trash(name):
    p = _dir("trash") / name
    if p.exists():
        p.unlink()


def prune_trash(days=TRASH_DAYS):
    d = _dir("trash")
    if not d.exists():
        return
    limit = datetime.now() - timedelta(days=days)
    for p in d.glob("*.json"):
        try:
            ts = datetime.strptime(p.name.split("_")[0], "%Y%m%d-%H%M%S-%f")
        except ValueError:
            continue
        if ts < limit:
            p.unlink()


# ---------- stored report files ----------
def store_report(iso, raw):
    """Keep the original uploaded file in storage/reports/YYYY-MM-DD.xlsx."""
    d = _ensure("reports")
    (d / f"{iso}.xlsx").write_bytes(raw)
    return d / f"{iso}.xlsx"


def stored_report_path(iso):
    return _dir("reports") / f"{iso}.xlsx"


def list_reports():
    """Stored report files, newest first: list of iso."""
    d = _dir("reports")
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("*.xlsx")), reverse=True)


def delete_stored_report(iso):
    p = stored_report_path(iso)
    if p.exists():
        p.unlink()
        return True
    return False


def stored_file_b64(iso):
    p = stored_report_path(iso)
    return base64.b64encode(p.read_bytes()).decode("ascii") if p.exists() else None


def restore_stored(iso, b64):
    d = _ensure("reports")
    (d / f"{iso}.xlsx").write_bytes(base64.b64decode(b64))


# ---------- backup / restore with storage ----------
def backup_zip():
    """All reports plus the stored files in one zip. Returns (bytes, number of reports)."""
    raw, count = history.backup_zip()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        d = _dir("reports")
        if d.exists():
            for p in sorted(d.glob("*.xlsx")):
                zout.write(p, arcname=f"storage/reports/{p.name}")
    return buf.getvalue(), count


def restore_zip(raw):
    """Add missing days and missing stored files. A day that exists is NEVER changed.
    Returns (added, skipped, ignored, files_added, files_skipped), or None if not a zip."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    added = skipped = ignored = files_added = files_skipped = 0
    with z:
        infos = z.infolist()
        if len(infos) > 5000:
            return None
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name):
                iso = name[:-5]
                try:
                    datetime.strptime(iso, "%Y-%m-%d")
                    data = json.loads(z.read(info).decode("utf-8"))["data"]
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                    ignored += 1
                    continue
                if history.HISTORY_DIR.joinpath(name).exists():
                    skipped += 1
                    continue
                history.save_report(iso, data)
                added += 1
            elif name.startswith("storage/reports/") and name.endswith(".xlsx"):
                iso = name[len("storage/reports/"):-len(".xlsx")]
                try:
                    datetime.strptime(iso, "%Y-%m-%d")
                except ValueError:
                    ignored += 1
                    continue
                if stored_report_path(iso).exists():
                    files_skipped += 1
                    continue
                store_report(iso, z.read(info))
                files_added += 1
            else:
                ignored += 1
    return added, skipped, ignored, files_added, files_skipped
