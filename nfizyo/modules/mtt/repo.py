"""Daten der MTT-Fläche: Geräte, Pläne, Besuche, Sätze, Vorschläge."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.security import new_token
from ...core.tenant import current_practice_id, current_user_id
from ...core.timeutil import today
from . import rules

KINDS = ("strength", "cardio", "free")


# --------------------------------------------------------------------------- Geräte
def machines(db: Database, include_inactive: bool = False):
    sql = "SELECT * FROM mtt_machines WHERE practice_id = ?" + ("" if include_inactive else " AND active = 1")
    return db.query(sql + " ORDER BY active DESC, position, name", (current_practice_id(),))


def machine(db: Database, machine_id: int):
    return db.one("SELECT * FROM mtt_machines WHERE id = ? AND practice_id = ?", (machine_id, current_practice_id()))


def machine_by_token(db: Database, token: str):
    return db.one("SELECT * FROM mtt_machines WHERE qr_token = ? AND practice_id = ? AND active = 1", (token, current_practice_id()))


def add_machine(db: Database, name: str, kind: str = "strength", step: float = 2.5, location_id: int | None = None) -> int:
    pos = db.value("SELECT COALESCE(MAX(position), 0) + 1 FROM mtt_machines WHERE practice_id = ?", (current_practice_id(),), 1)
    mid = db.execute("INSERT INTO mtt_machines (practice_id, location_id, name, kind, step, qr_token, position, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)", (current_practice_id(), location_id, name.strip()[:80], kind if kind in KINDS else "strength",
                                                   step if step and step > 0 else 2.5, new_token(12), pos, utcnow()))
    audit(db, "create", "mtt_machine", mid)
    return mid


def set_machine_active(db: Database, machine_id: int, active: bool) -> None:
    db.execute("UPDATE mtt_machines SET active = ? WHERE id = ? AND practice_id = ?", (1 if active else 0, machine_id, current_practice_id()))
    audit(db, "update", "mtt_machine", machine_id, None, {"active": active})


# --------------------------------------------------------------------------- Pläne
def current_plan(db: Database, patient_id: int, create: bool = False):
    p = db.one("SELECT * FROM mtt_plans WHERE practice_id = ? AND patient_id = ? AND status IN ('draft', 'active') ORDER BY id DESC LIMIT 1",
               (current_practice_id(), patient_id))
    if p is None and create:
        now = utcnow()
        pid = db.execute("INSERT INTO mtt_plans (practice_id, patient_id, author_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         (current_practice_id(), patient_id, current_user_id(), "draft", now, now))
        audit(db, "create", "mtt_plan", pid, patient_id)
        p = db.one("SELECT * FROM mtt_plans WHERE id = ?", (pid,))
    return p


def active_plan(db: Database, patient_id: int):
    return db.one("SELECT * FROM mtt_plans WHERE practice_id = ? AND patient_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                  (current_practice_id(), patient_id))


def items(db: Database, plan_id: int):
    return db.query("SELECT i.*, m.name AS machine_name, m.kind, m.step, m.qr_token FROM mtt_items i JOIN mtt_machines m ON m.id = i.machine_id"
                    " WHERE i.practice_id = ? AND i.plan_id = ? AND i.removed = 0 ORDER BY i.position, i.id", (current_practice_id(), plan_id))


def item(db: Database, item_id: int):
    return db.one("SELECT i.*, m.name AS machine_name, m.kind, m.step, p.patient_id, p.status AS plan_status FROM mtt_items i"
                  " JOIN mtt_machines m ON m.id = i.machine_id JOIN mtt_plans p ON p.id = i.plan_id"
                  " WHERE i.id = ? AND i.practice_id = ? AND i.removed = 0", (item_id, current_practice_id()))


def add_item(db: Database, plan, machine_row) -> int:
    pos = db.value("SELECT COALESCE(MAX(position), 0) + 1 FROM mtt_items WHERE plan_id = ?", (plan["id"],), 1)
    cardio = machine_row["kind"] == "cardio"
    iid = db.execute("INSERT INTO mtt_items (practice_id, plan_id, machine_id, sets, reps, load, minutes, position) VALUES (?,?,?,?,?,?,?,?)",
                     (current_practice_id(), plan["id"], machine_row["id"], None if cardio else 3, None if cardio else 12, None,
                      10 if cardio else None, pos))
    _touch(db, plan)
    return iid


def update_item(db: Database, item_id: int, **f) -> None:
    allowed = {k: v for k, v in f.items() if k in ("sets", "reps", "load", "minutes", "setting", "note", "note_lang")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    db.execute(f"UPDATE mtt_items SET {sets} WHERE id = ? AND practice_id = ?", (*allowed.values(), item_id, current_practice_id()))
    plan = db.one("SELECT p.* FROM mtt_plans p JOIN mtt_items i ON i.plan_id = p.id WHERE i.id = ? AND p.practice_id = ?",
                  (item_id, current_practice_id()))
    if plan:
        _touch(db, plan, {"item": item_id, "fields": sorted(allowed)})


def remove_item(db: Database, item_id: int) -> None:
    db.execute("UPDATE mtt_items SET removed = 1 WHERE id = ? AND practice_id = ?", (item_id, current_practice_id()))
    plan = db.one("SELECT p.* FROM mtt_plans p JOIN mtt_items i ON i.plan_id = p.id WHERE i.id = ? AND p.practice_id = ?",
                  (item_id, current_practice_id()))
    if plan:
        _touch(db, plan, {"removed": item_id})


def _touch(db: Database, plan, detail: dict | None = None) -> None:
    db.execute("UPDATE mtt_plans SET updated_at = ? WHERE id = ? AND practice_id = ?", (utcnow(), plan["id"], current_practice_id()))
    audit(db, "update", "mtt_plan", plan["id"], plan["patient_id"], detail)


def approve(db: Database, plan, goal: str | None = None, goal_lang: str = "de") -> None:
    db.execute("UPDATE mtt_plans SET status = 'active', goal = COALESCE(?, goal), goal_lang = CASE WHEN ? IS NULL THEN goal_lang ELSE ? END,"
               " approved_at = ?, approved_by = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
               (goal, goal, goal_lang, utcnow(), current_user_id(), utcnow(), plan["id"], current_practice_id()))
    audit(db, "approve", "mtt_plan", plan["id"], plan["patient_id"])


# --------------------------------------------------------------------------- Besuche und Sätze
def today_visit(db: Database, patient_id: int, day: str):
    return db.one("SELECT * FROM mtt_visits WHERE practice_id = ? AND patient_id = ? AND day = ?", (current_practice_id(), patient_id, day))


def start_visit(db: Database, patient_id: int, plan_id: int | None, day: str, source: str) -> tuple[int, bool]:
    """(Besuch, neu?) – höchstens ein Besuch pro Tag; ein beendeter wird wieder geöffnet."""
    v = today_visit(db, patient_id, day)
    if v is not None:
        if v["ended_at"]:
            db.execute("UPDATE mtt_visits SET ended_at = NULL WHERE id = ? AND practice_id = ?", (v["id"], current_practice_id()))
        return v["id"], False
    vid = db.execute("INSERT INTO mtt_visits (practice_id, patient_id, plan_id, day, started_at, source) VALUES (?,?,?,?,?,?)",
                     (current_practice_id(), patient_id, plan_id, day, utcnow(), source if source in ("app", "qr", "desk") else "app"))
    audit(db, "create", "mtt_visit", vid, patient_id, {"source": source})
    return vid, True


def end_visit(db: Database, visit_id: int) -> None:
    db.execute("UPDATE mtt_visits SET ended_at = ? WHERE id = ? AND practice_id = ? AND ended_at IS NULL",
               (utcnow(), visit_id, current_practice_id()))


def log_set(db: Database, visit_id: int, it, *, reps: int | None, load: float | None, minutes: int | None,
            effort: int | None, pain: int | None) -> int:
    return db.execute("INSERT INTO mtt_sets (practice_id, visit_id, item_id, patient_id, reps, load, minutes, effort, pain, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)", (current_practice_id(), visit_id, it["id"], it["patient_id"], reps, load, minutes,
                                                        effort, pain, utcnow()))


def sets_of_visit(db: Database, visit_id: int):
    return db.query("SELECT * FROM mtt_sets WHERE practice_id = ? AND visit_id = ? ORDER BY id", (current_practice_id(), visit_id))


def history(db: Database, item_id: int, days: int = 10) -> list[dict]:
    """Je Trainingstag: Sätze an diesem Gerät (ältester zuerst)."""
    rows = db.query("SELECT s.*, v.day FROM mtt_sets s JOIN mtt_visits v ON v.id = s.visit_id WHERE s.practice_id = ? AND s.item_id = ?"
                    " ORDER BY v.day, s.id", (current_practice_id(), item_id))
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["day"], []).append(r)
    out = [{"day": d, "sets": s} for d, s in by.items()]
    return out[-days:]


def visits(db: Database, patient_id: int, limit: int = 12):
    return db.query("SELECT v.*, (SELECT COUNT(*) FROM mtt_sets s WHERE s.visit_id = v.id) AS n_sets,"
                    " (SELECT MAX(pain) FROM mtt_sets s WHERE s.visit_id = v.id) AS max_pain,"
                    " (SELECT MAX(effort) FROM mtt_sets s WHERE s.visit_id = v.id) AS max_effort"
                    " FROM mtt_visits v WHERE v.practice_id = ? AND v.patient_id = ? ORDER BY v.day DESC LIMIT ?",
                    (current_practice_id(), patient_id, limit))


def live(db: Database, *, only: tuple[str, list] | None = None, hours: int = 3) -> list:
    """Wer trainiert gerade (Besuch heute offen, in den letzten Stunden begonnen)?"""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    sql = ("SELECT v.*, p.first_name, p.last_name FROM mtt_visits v JOIN patients p ON p.id = v.patient_id"
           " WHERE v.practice_id = ? AND v.ended_at IS NULL AND v.started_at >= ?")
    params: list = [current_practice_id(), since]
    if only:
        sql += f" AND v.patient_id IN ({only[0]})"
        params += list(only[1])
    return db.query(sql + " ORDER BY v.started_at", params)


def live_count(db: Database, hours: int = 3) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    return db.value("SELECT COUNT(*) FROM mtt_visits WHERE practice_id = ? AND ended_at IS NULL AND started_at >= ?",
                    (current_practice_id(), since), 0)


# --------------------------------------------------------------------------- Vorschläge
def open_suggestions(db: Database, *, patient_id: int | None = None, only: tuple[str, list] | None = None):
    sql = ("SELECT g.*, i.sets, i.reps, i.load AS current_load, m.name AS machine_name, p.first_name, p.last_name FROM mtt_suggestions g"
           " JOIN mtt_items i ON i.id = g.item_id JOIN mtt_machines m ON m.id = i.machine_id JOIN patients p ON p.id = g.patient_id"
           " WHERE g.practice_id = ? AND g.status = 'open' AND i.removed = 0")
    params: list = [current_practice_id()]
    if patient_id:
        sql += " AND g.patient_id = ?"
        params.append(patient_id)
    if only:
        sql += f" AND g.patient_id IN ({only[0]})"
        params += list(only[1])
    return db.query(sql + " ORDER BY g.id", params)


def suggestion(db: Database, sid: int):
    return db.one("SELECT * FROM mtt_suggestions WHERE id = ? AND practice_id = ?", (sid, current_practice_id()))


def refresh_suggestion(db: Database, it) -> int | None:
    """Regeln auf die letzten Trainingstage anwenden; höchstens ein offener Vorschlag pro Gerät.
    Ein verworfener Vorschlag derselben Art kommt frühestens nach 7 Tagen wieder."""
    if it["kind"] == "cardio" or not it["load"]:
        return None
    days = []
    for d in history(db, it["id"], days=2):
        days.append(rules.Day(reps=[s["reps"] for s in d["sets"]], loads=[s["load"] for s in d["sets"]],
                              effort=[s["effort"] for s in d["sets"]], pain=[s["pain"] for s in d["sets"]]))
    s = rules.suggest(sets=it["sets"], reps=it["reps"], load=it["load"], step=it["step"], days=days)
    current = db.one("SELECT * FROM mtt_suggestions WHERE practice_id = ? AND item_id = ? AND status = 'open'", (current_practice_id(), it["id"]))
    if s is None:
        return None
    if current is not None:
        if current["kind"] == s.kind and current["to_load"] == s.to_load:
            return current["id"]
        db.execute("UPDATE mtt_suggestions SET status = 'dismissed', decided_at = ? WHERE id = ?", (utcnow(), current["id"]))
    week = (today() - timedelta(days=7)).isoformat()
    if db.one("SELECT 1 FROM mtt_suggestions WHERE practice_id = ? AND item_id = ? AND status = 'dismissed' AND kind = ?"
              " AND decided_by IS NOT NULL AND decided_at >= ?", (current_practice_id(), it["id"], s.kind, week)):
        return None
    return db.execute("INSERT INTO mtt_suggestions (practice_id, patient_id, item_id, kind, reason, from_load, to_load, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?)", (current_practice_id(), it["patient_id"], it["id"], s.kind, s.reason, s.from_load,
                                                    s.to_load, utcnow()))


def decide(db: Database, sid: int, accept: bool) -> None:
    g = suggestion(db, sid)
    if g is None or g["status"] != "open":
        return
    db.execute("UPDATE mtt_suggestions SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? AND practice_id = ?",
               ("accepted" if accept else "dismissed", current_user_id(), utcnow(), sid, current_practice_id()))
    if accept and g["to_load"] is not None:
        update_item(db, g["item_id"], load=g["to_load"])
    audit(db, "update", "mtt_suggestion", sid, g["patient_id"], {"accepted": accept, "to": g["to_load"]})
