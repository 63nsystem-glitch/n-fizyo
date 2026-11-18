"""Öffentliche Schnittstelle des Heimprogramms – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from datetime import date, timedelta

from ...core.db import Database
from ...core.tenant import current_practice_id
from . import repo


def practice_days(db: Database, patient_id: int, since: date, until: date) -> dict | None:
    """Übungstage zwischen since und until (inklusive): {"days": [{"date","n","pain","skip"}], "done", "total", "max_pain",
    "last_pain", "items", "skips": [{"date","reason","note"}], "skip_pain"} – oder None ohne freigegebenes Programm.
    skip = Grund, den die Patientin / der Patient selbst angegeben hat (forgot, unclear, no_time, pain, app, no_benefit)."""
    prog = repo.active_program(db, patient_id)
    if prog is None:
        return None
    if until < since:
        since = until
    rows = db.query("SELECT done_on, COUNT(DISTINCT item_id) AS n, MAX(pain) AS pain FROM exercise_logs WHERE practice_id = ?"
                    " AND patient_id = ? AND done_on BETWEEN ? AND ? GROUP BY done_on",
                    (current_practice_id(), patient_id, since.isoformat(), until.isoformat()))
    by = {r["done_on"]: r for r in rows}
    skipped = {r["day"]: r for r in repo.skips_since(db, patient_id, since.isoformat()) if r["day"] <= until.isoformat()}
    days = []
    d = since
    while d <= until:
        r = by.get(d.isoformat())
        sk = skipped.get(d.isoformat())
        days.append({"date": d, "n": r["n"] if r else 0, "pain": r["pain"] if r else None, "skip": sk["reason"] if sk else None})
        d += timedelta(days=1)
    pains = [x["pain"] for x in days if x["pain"] is not None]
    skips = [{"date": date.fromisoformat(r["day"]), "reason": r["reason"], "note": r["note"]} for r in skipped.values()]
    return {"days": days, "done": sum(1 for x in days if x["n"]), "total": len(days), "max_pain": max(pains) if pains else None,
            "last_pain": pains[-1] if pains else None, "items": len(repo.items(db, prog["id"])), "skips": skips,
            "skip_pain": any(s["reason"] == "pain" for s in skips)}


def program_items(db: Database, patient_id: int) -> list[dict]:
    """Übungen des freigegebenen Programms (Name DE/FR, Dosierung inkl. Dauer in Minuten)."""
    prog = repo.active_program(db, patient_id)
    if prog is None:
        return []
    return [{"name_de": i["name_de"], "name_fr": i["name_fr"], "sets": i["sets"], "reps": i["reps"], "hold_sec": i["hold_sec"],
             "duration_min": i["duration_min"], "per_day": i["per_day"], "note": i["note"]} for i in repo.items(db, prog["id"])]
