"""Heimprogramm: Bibliothek, Programme, Erledigt-Protokoll."""
from __future__ import annotations

from datetime import timedelta

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import today

AREAS = ("knee", "shoulder", "hip", "lumbar", "cervical", "ankle", "general")


def library(db: Database, q: str = "", area: str = "", active: bool | None = True):
    """Übungen der Praxis. active=True: Auswahl (nur aktive), False: nur deaktivierte, None: alle."""
    sql = "SELECT * FROM exercises WHERE practice_id = ?"
    params: list = [current_practice_id()]
    if active is not None:
        sql += " AND active = ?"
        params.append(1 if active else 0)
    if q:
        sql += " AND (name_de LIKE ? OR name_fr LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if area:
        sql += " AND (',' || regions || ',') LIKE ?"
        params.append(f"%,{area},%")
    return db.query(sql + " ORDER BY name_de", params)


def exercise(db: Database, eid: int):
    return db.one("SELECT * FROM exercises WHERE id = ? AND practice_id = ?", (eid, current_practice_id()))


def add_exercise(db: Database, **f) -> int:
    return db.execute(
        "INSERT INTO exercises (practice_id, code, name_de, name_fr, instructions_de, instructions_fr, regions, icon, sets, reps,"
        " hold_sec, per_day, drawing, duration_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), f.get("code"), f["name_de"], f["name_fr"], f["instructions_de"], f["instructions_fr"],
         f.get("regions"), f.get("icon") or "fitness_center", f.get("sets"), f.get("reps"), f.get("hold_sec"), f.get("per_day") or 1,
         f.get("drawing"), f.get("duration_min")),
    )


EXERCISE_FIELDS = ("name_de", "name_fr", "instructions_de", "instructions_fr", "regions", "drawing", "sets", "reps", "hold_sec",
                   "duration_min", "per_day")


def update_exercise(db: Database, eid: int, **f) -> bool:
    """Übung der eigenen Praxis ändern. Laufende Programme zeigen Name, Anleitung und Zeichnung sofort neu;
    ihre Dosierung bleibt (sie wurde beim Hinzufügen übernommen)."""
    allowed = {k: v for k, v in f.items() if k in EXERCISE_FIELDS}
    if not allowed or exercise(db, eid) is None:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    db.execute(f"UPDATE exercises SET {sets} WHERE id = ? AND practice_id = ?", (*allowed.values(), eid, current_practice_id()))
    audit(db, "update", "exercise", eid)
    return True


def set_exercise_active(db: Database, eid: int, on: bool) -> bool:
    """Deaktivierte Übungen verschwinden aus der Auswahl, bleiben aber in bestehenden Programmen und im Verlauf."""
    if exercise(db, eid) is None:
        return False
    db.execute("UPDATE exercises SET active = ? WHERE id = ? AND practice_id = ?", (1 if on else 0, eid, current_practice_id()))
    audit(db, "activate" if on else "deactivate", "exercise", eid)
    return True


def exercise_usage(db: Database, eid: int) -> int:
    """In wie vielen laufenden Programmen (Entwurf oder freigegeben) die Übung steckt."""
    return db.value("SELECT COUNT(DISTINCT p.id) FROM program_items i JOIN programs p ON p.id = i.program_id"
                    " WHERE i.practice_id = ? AND i.exercise_id = ? AND i.removed = 0 AND p.status IN ('draft', 'active')",
                    (current_practice_id(), eid), 0)


def current_program(db: Database, patient_id: int, create: bool = False):
    p = db.one("SELECT * FROM programs WHERE practice_id = ? AND patient_id = ? AND status IN ('draft','active')"
               " ORDER BY id DESC LIMIT 1", (current_practice_id(), patient_id))
    if p is None and create:
        now = utcnow()
        pid = db.execute("INSERT INTO programs (practice_id, patient_id, author_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         (current_practice_id(), patient_id, current_user_id(), "draft", now, now))
        audit(db, "create", "program", pid, patient_id)
        p = db.one("SELECT * FROM programs WHERE id = ?", (pid,))
    return p


def items(db: Database, program_id: int):
    return db.query(
        "SELECT i.*, e.name_de, e.name_fr, e.instructions_de, e.instructions_fr, e.icon, e.regions, e.drawing FROM program_items i"
        " JOIN exercises e ON e.id = i.exercise_id WHERE i.practice_id = ? AND i.program_id = ? AND i.removed = 0"
        " ORDER BY i.position, i.id", (current_practice_id(), program_id))


def item(db: Database, item_id: int):
    return db.one(
        "SELECT i.*, e.name_de, e.name_fr, e.instructions_de, e.instructions_fr, e.icon, e.regions, e.drawing, p.patient_id,"
        " p.status AS program_status"
        " FROM program_items i JOIN exercises e ON e.id = i.exercise_id JOIN programs p ON p.id = i.program_id"
        " WHERE i.id = ? AND i.practice_id = ? AND i.removed = 0", (item_id, current_practice_id()))


def add_item(db: Database, program, exercise_id: int, note: str | None, note_lang: str) -> int:
    ex = exercise(db, exercise_id)
    if ex is None or not ex["active"]:
        raise KeyError(exercise_id)
    pos = db.value("SELECT COALESCE(MAX(position), 0) + 1 FROM program_items WHERE program_id = ?", (program["id"],), 1)
    iid = db.execute(
        "INSERT INTO program_items (practice_id, program_id, exercise_id, sets, reps, hold_sec, per_day, duration_min, note, note_lang,"
        " position) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), program["id"], exercise_id, ex["sets"], ex["reps"], ex["hold_sec"], ex["per_day"], ex["duration_min"],
         note, note_lang, pos))
    _touch(db, program)
    return iid


def update_item(db: Database, item_id: int, **f) -> None:
    allowed = {k: v for k, v in f.items() if k in ("sets", "reps", "hold_sec", "duration_min", "per_day", "note", "note_lang")}
    if allowed:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        db.execute(f"UPDATE program_items SET {sets} WHERE id = ? AND practice_id = ?", (*allowed.values(), item_id, current_practice_id()))
        prog = db.one("SELECT p.* FROM programs p JOIN program_items i ON i.program_id = p.id WHERE i.id = ?", (item_id,))
        if prog:
            _touch(db, prog)


def remove_item(db: Database, item_id: int) -> None:
    db.execute("UPDATE program_items SET removed = 1 WHERE id = ? AND practice_id = ?", (item_id, current_practice_id()))
    prog = db.one("SELECT p.* FROM programs p JOIN program_items i ON i.program_id = p.id WHERE i.id = ?", (item_id,))
    if prog:
        _touch(db, prog)


def _touch(db: Database, program) -> None:
    db.execute("UPDATE programs SET updated_at = ? WHERE id = ?", (utcnow(), program["id"]))
    audit(db, "update", "program", program["id"], program["patient_id"])


def approve(db: Database, program) -> None:
    db.execute("UPDATE programs SET status = 'active', approved_at = ?, approved_by = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), current_user_id(), utcnow(), program["id"], current_practice_id()))
    audit(db, "approve", "program", program["id"], program["patient_id"])


def active_program(db: Database, patient_id: int):
    return db.one("SELECT * FROM programs WHERE practice_id = ? AND patient_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                  (current_practice_id(), patient_id))


def log_done(db: Database, patient_id: int, item_id: int, pain: int | None) -> None:
    db.execute("INSERT INTO exercise_logs (practice_id, patient_id, item_id, done_on, pain, created_at) VALUES (?,?,?,?,?,?)",
               (current_practice_id(), patient_id, item_id, today().isoformat(), pain, utcnow()))


def done_today(db: Database, patient_id: int) -> set[int]:
    return {r["item_id"] for r in db.query("SELECT DISTINCT item_id FROM exercise_logs WHERE practice_id = ? AND patient_id = ? AND done_on = ?",
                                           (current_practice_id(), patient_id, today().isoformat()))}


def today_summary(db: Database, patient_id: int) -> dict | None:
    prog = active_program(db, patient_id)
    if prog is None:
        return None
    its = items(db, prog["id"])
    if not its:
        return None
    done = done_today(db, patient_id)
    return {"total": len(its), "done_today": sum(1 for i in its if i["id"] in done)}


def adherence(db: Database, patient_id: int, days: int = 7) -> list[dict]:
    """Pro Tag: wie viele Übungen erledigt (für die Therapie-Ansicht)."""
    start = today() - timedelta(days=days - 1)
    rows = db.query("SELECT done_on, COUNT(DISTINCT item_id) AS n, MAX(pain) AS pain FROM exercise_logs WHERE practice_id = ?"
                    " AND patient_id = ? AND done_on >= ? GROUP BY done_on", (current_practice_id(), patient_id, start.isoformat()))
    by = {r["done_on"]: r for r in rows}
    skips = {r["day"]: r for r in db.query("SELECT day, reason, note FROM exercise_skips WHERE practice_id = ? AND patient_id = ?"
                                             " AND day >= ?", (current_practice_id(), patient_id, start.isoformat()))}
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        r = by.get(d.isoformat())
        sk = skips.get(d.isoformat())
        out.append({"date": d, "n": r["n"] if r else 0, "pain": r["pain"] if r else None,
                    "skip": sk["reason"] if sk else None, "skip_note": sk["note"] if sk else None})
    return out


# --------------------------------------------------------------------------- «Bei wenig Zeit zuerst», eigenes Video
def set_essential(db: Database, item_id: int, on: bool) -> None:
    db.execute("UPDATE program_items SET essential = ? WHERE id = ? AND practice_id = ?", (1 if on else 0, item_id, current_practice_id()))


def set_video(db: Database, item_id: int, path: str, mime: str, size: int) -> None:
    db.execute("UPDATE program_items SET video_path = ?, video_mime = ?, video_size = ?, video_consent_at = ?, video_by = ?"
               " WHERE id = ? AND practice_id = ?", (path, mime, size, utcnow(), current_user_id(), item_id, current_practice_id()))


def swap_video_file(db: Database, item_id: int, old_path: str, new_path: str, mime: str, size: int) -> bool:
    """Umgewandelte Datei eintragen – nur wenn inzwischen nicht ein anderes Video hochgeladen oder gelöscht wurde."""
    return db.changes("UPDATE program_items SET video_path = ?, video_mime = ?, video_size = ? WHERE id = ? AND practice_id = ?"
                      " AND video_path = ?", (new_path, mime, size, item_id, current_practice_id(), old_path)) == 1


def clear_video(db: Database, item_id: int) -> str | None:
    """Video entfernen; gibt den alten Dateipfad zurück (die Datei löscht der Aufrufer)."""
    row = db.one("SELECT video_path FROM program_items WHERE id = ? AND practice_id = ?", (item_id, current_practice_id()))
    db.execute("UPDATE program_items SET video_path = NULL, video_mime = NULL, video_size = NULL, video_consent_at = NULL,"
               " video_by = NULL WHERE id = ? AND practice_id = ?", (item_id, current_practice_id()))
    return row["video_path"] if row else None


# --------------------------------------------------------------------------- Nicht geübt – warum?
SKIP_REASONS = ("forgot", "unclear", "no_time", "pain", "app", "no_benefit")


def log_skip(db: Database, patient_id: int, reason: str, note: str | None, day: str | None = None) -> None:
    """Ein Grund pro Tag (ein neuer ersetzt den alten)."""
    if reason not in SKIP_REASONS:
        raise ValueError(reason)
    db.execute("INSERT INTO exercise_skips (practice_id, patient_id, day, reason, note, created_at) VALUES (?,?,?,?,?,?)"
               " ON CONFLICT (practice_id, patient_id, day) DO UPDATE SET reason = excluded.reason, note = excluded.note,"
               " created_at = excluded.created_at",
               (current_practice_id(), patient_id, day or today().isoformat(), reason, (note or "").strip()[:300] or None, utcnow()))


def skip_today(db: Database, patient_id: int):
    return db.one("SELECT * FROM exercise_skips WHERE practice_id = ? AND patient_id = ? AND day = ?",
                  (current_practice_id(), patient_id, today().isoformat()))


def skips_since(db: Database, patient_id: int, since: str):
    return db.query("SELECT day, reason, note FROM exercise_skips WHERE practice_id = ? AND patient_id = ? AND day >= ? ORDER BY day",
                    (current_practice_id(), patient_id, since))

