# storage.py - drafts, stored report files, trash (undo) and storage backup.
# Storage: MongoDB when MONGODB_URI is set (Render), otherwise under DATA_DIR.
import base64
import io
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone

import db
import history

TRASH_DAYS = 7  # keep undo copies for this many days


def _dir(name):
    return history.DATA_DIR / "storage" / name


def _ensure(name):
    d = _dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drafts():
    return db.mongo()["drafts"]


def _trash():
    return db.mongo()["trash"]


def _fs():
    from gridfs import GridFS
    return GridFS(db.mongo())


# ---------- drafts (an unfinished report) ----------
def draft_path(iso):
    return _dir("drafts") / f"{iso}.json"


def save_draft(session):
    """Atomically write the report that is being filled."""
    payload = {
        "date": session["date"],
        "explicit": bool(session.get("explicit")),
        "saved": bool(session.get("saved")),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": session.get("data") or {},
    }
    if db.use_mongo():
        _drafts().replace_one({"_id": session["date"]}, dict(payload, _id=session["date"]),
                              upsert=True)
        return None
    d = _ensure("drafts")
    tmp = d / (f"{session['date']}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, draft_path(session["date"]))
    return draft_path(session["date"])


def _draft_doc(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
        return None
    out = {k: v for k, v in doc.items() if k != "_id"}
    out.setdefault("explicit", False)
    out.setdefault("saved", False)
    return out


def load_draft(iso):
    if db.use_mongo():
        return _draft_doc(_drafts().find_one({"_id": iso}))
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
    if db.use_mongo():
        return _drafts().delete_one({"_id": iso}).deleted_count > 0
    p = draft_path(iso)
    if p.exists():
        p.unlink()
        return True
    return False


def list_drafts(exclude=None):
    """All drafts, newest first: [{date, explicit, saved, updated_at, data}]."""
    if db.use_mongo():
        out = []
        for doc in _drafts().find({}):
            if exclude and doc.get("_id") == exclude:
                continue
            d = _draft_doc(doc)
            if d:
                out.append(d)
        return sorted(out, key=lambda r: r["date"], reverse=True)
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
    """Copy data to trash before a destructive action. Returns the id / file name."""
    if db.use_mongo():
        doc = {"kind": kind, "iso": iso, "payload": payload, "at": datetime.now(timezone.utc)}
        return str(_trash().insert_one(doc).inserted_id)
    d = _ensure("trash")
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    name = f"{ts}_{kind}_{iso}.json"
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps({"kind": kind, "iso": iso, "payload": payload}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, d / name)
    return name


def _object_id(name):
    from bson import ObjectId
    try:
        return ObjectId(name)
    except Exception:
        return None


def restore_trash(name):
    if db.use_mongo():
        oid = _object_id(name)
        if oid is None:
            return None
        doc = _trash().find_one({"_id": oid})
        return doc.get("payload") if doc else None
    p = _dir("trash") / name
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("payload")
    except (OSError, ValueError):
        return None


def delete_trash(name):
    if db.use_mongo():
        oid = _object_id(name)
        if oid is not None:
            _trash().delete_one({"_id": oid})
        return
    p = _dir("trash") / name
    if p.exists():
        p.unlink()


def prune_trash(days=TRASH_DAYS):
    if db.use_mongo():
        limit = datetime.now(timezone.utc) - timedelta(days=days)
        _trash().delete_many({"at": {"$lt": limit}})
        return
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
    """Keep the original uploaded file (GridFS in MongoDB, or storage/reports/YYYY-MM-DD.xlsx)."""
    if db.use_mongo():
        fs = _fs()
        for d in db.mongo()["fs.files"].find({"filename": f"{iso}.xlsx"}):
            fs.delete(d["_id"])
        fs.put(raw, filename=f"{iso}.xlsx")
        return None
    d = _ensure("reports")
    (d / f"{iso}.xlsx").write_bytes(raw)
    return d / f"{iso}.xlsx"


def stored_report_path(iso):
    return _dir("reports") / f"{iso}.xlsx"


def get_stored_report(iso):
    """The stored file's bytes, or None. Works with local files and MongoDB."""
    if db.use_mongo():
        doc = db.mongo()["fs.files"].find_one({"filename": f"{iso}.xlsx"})
        if not doc:
            return None
        return _fs().get(doc["_id"]).read()
    p = stored_report_path(iso)
    return p.read_bytes() if p.exists() else None


def stored_report_exists(iso):
    if db.use_mongo():
        return db.mongo()["fs.files"].count_documents({"filename": f"{iso}.xlsx"}) > 0
    return stored_report_path(iso).exists()


def list_reports():
    """Stored report files, newest first: list of iso."""
    if db.use_mongo():
        names = db.mongo()["fs.files"].distinct("filename")
        return sorted((n[:-5] for n in names if isinstance(n, str) and n.endswith(".xlsx")),
                      reverse=True)
    d = _dir("reports")
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("*.xlsx")), reverse=True)


def delete_stored_report(iso):
    if db.use_mongo():
        fs = _fs()
        ids = [d["_id"] for d in db.mongo()["fs.files"].find({"filename": f"{iso}.xlsx"})]
        for i in ids:
            fs.delete(i)
        return bool(ids)
    p = stored_report_path(iso)
    if p.exists():
        p.unlink()
        return True
    return False


def stored_file_b64(iso):
    raw = get_stored_report(iso)
    return base64.b64encode(raw).decode("ascii") if raw is not None else None


def restore_stored(iso, b64):
    store_report(iso, base64.b64decode(b64))


# ---------- backup / restore with storage ----------
def backup_zip():
    """All reports plus the stored files in one zip. Returns (bytes, number of reports)."""
    raw, count = history.backup_zip()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        if db.use_mongo():
            for iso in list_reports():
                data = get_stored_report(iso)
                if data is not None:
                    zout.writestr(f"storage/reports/{iso}.xlsx", data)
        else:
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
                if history.load_report(iso) is not None:
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
                if stored_report_exists(iso):
                    files_skipped += 1
                    continue
                store_report(iso, z.read(info))
                files_added += 1
            else:
                ignored += 1
    return added, skipped, ignored, files_added, files_skipped
