"""Klinische Akte: Befunde am Körper und Messwerte. Stammdaten liegen im Kern (core/patients.py)."""
from __future__ import annotations

from datetime import date
from typing import Any

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...knowledge import anatomy
from ...core.timeutil import today as zurich_today

FINDING_KINDS = ("diagnosis", "surgery", "finding", "pain", "restriction", "clearance", "complication", "red_flag", "goal")


# --------------------------------------------------------------------------- Befunde am Körper
def findings(db: Database, patient_id: int, status: str | None = "active"):
    pid = current_practice_id()
    sql = "SELECT * FROM findings WHERE practice_id = ? AND patient_id = ?"
    params: list[Any] = [pid, patient_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += (" ORDER BY CASE kind WHEN 'red_flag' THEN 0 WHEN 'diagnosis' THEN 1 WHEN 'surgery' THEN 2"
            " WHEN 'complication' THEN 3 WHEN 'restriction' THEN 4 WHEN 'pain' THEN 5 ELSE 6 END, id")
    return db.query(sql, params)


def add_finding(db: Database, patient_id: int, *, kind: str, title: str, detail: str | None = None,
                structure_id: str | None = None, region: str | None = None, side: str | None = None,
                spine_levels: list[str] | str | None = None, severity: int | None = None, on_date: str | None = None,
                until: str | None = None, source: str = "manual", source_document_id: int | None = None,
                source_excerpt: str | None = None) -> int:
    if kind not in FINDING_KINDS:
        raise ValueError(kind)
    if structure_id and anatomy.get(structure_id) is None:
        structure_id = None
    if isinstance(spine_levels, list):
        spine_levels = ",".join(spine_levels) or None
    if region is None and structure_id:
        regions = anatomy.muscle_regions(anatomy.get(structure_id), side)
        region = regions[0] if regions else None
    now = utcnow()
    fid = db.execute(
        "INSERT INTO findings (practice_id, patient_id, kind, title, detail, structure_id, region, side, spine_levels,"
        " severity, on_date, until, source, source_document_id, source_excerpt, created_by, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, kind, title.strip()[:500], detail, structure_id, region, side, spine_levels,
         severity, on_date, until, source, source_document_id, source_excerpt, current_user_id(), now, now),
    )
    audit(db, "create", "finding", fid, patient_id, {"kind": kind, "source": source})
    return fid


def set_finding_status(db: Database, patient_id: int, finding_id: int, status: str) -> None:
    db.execute(
        "UPDATE findings SET status = ?, updated_at = ? WHERE id = ? AND patient_id = ? AND practice_id = ?",
        (status, utcnow(), finding_id, patient_id, current_practice_id()),
    )
    audit(db, "update", "finding", finding_id, patient_id, {"status": status})


def finding_marks(rows, today: date | None = None) -> dict:
    """Markierungen für Körperkarte und Skelett aus aktiven Befunden.

    Ergebnis: {"muscle": {region: klasse}, "skeleton": {id: klasse}, "markers": [...]} – Nummern
    gruppiert nach Körperstelle; Rot = Diagnose/OP/Warnzeichen, Orange = weitere Befunde.
    """
    today = today or zurich_today()
    muscle: dict[str, str] = {}
    skeleton: dict[str, str] = {}
    groups: dict[tuple, dict] = {}
    for f in rows:
        if f["kind"] in ("goal", "clearance"):
            continue
        if f["kind"] == "restriction" and f["until"] and f["until"] < today.isoformat():
            continue
        structure = anatomy.get(f["structure_id"])
        levels = [x for x in (f["spine_levels"] or "").split(",") if x]
        key = (structure.joint or structure.id) if structure else (f["region"] or f"f{f['id']}")
        key = (key, f["side"])
        strong = f["kind"] in ("diagnosis", "surgery", "red_flag", "complication")
        g = groups.setdefault(key, {"findings": [], "strong": False, "regions": set(), "bones": set(), "structure": structure})
        g["findings"].append(f)
        g["strong"] = g["strong"] or strong
        if structure:
            g["regions"].update(anatomy.muscle_regions(structure, f["side"]))
            g["bones"].update(anatomy.skeleton_ids(structure, f["side"], levels))
        elif f["region"]:
            g["regions"].add(f["region"])
        if levels:
            g["bones"].update(levels)
    ordered = sorted(groups.values(), key=lambda g: (not g["strong"], g["findings"][0]["id"]))
    markers = []
    for n, g in enumerate(ordered, start=1):
        cls = "hl-1" if g["strong"] else "hl-2"
        for r in g["regions"]:
            if muscle.get(r) != "hl-1":
                muscle[r] = cls
        for b in g["bones"]:
            if skeleton.get(b) != "hl-1":
                skeleton[b] = cls
        markers.append({
            "n": n, "cls": "c1" if g["strong"] else "c2", "structure": g["structure"], "findings": g["findings"],
            "side": g["findings"][0]["side"], "regions": sorted(g["regions"]), "bones": sorted(g["bones"]),
        })
    return {"muscle": muscle, "skeleton": skeleton, "markers": markers}


# --------------------------------------------------------------------------- Messwerte
def measurements(db: Database, patient_id: int, kind: str | None = None):
    pid = current_practice_id()
    if kind:
        return db.query(
            "SELECT * FROM measurements WHERE practice_id = ? AND patient_id = ? AND kind = ? ORDER BY measured_on, id",
            (pid, patient_id, kind),
        )
    return db.query(
        "SELECT * FROM measurements WHERE practice_id = ? AND patient_id = ? ORDER BY measured_on, id", (pid, patient_id)
    )


def add_measurement(db: Database, patient_id: int, kind: str, value: float, unit: str, *, joint: str | None = None,
                    side: str | None = None, measured_on: str | None = None, note_id: int | None = None,
                    goal: float | None = None) -> int:
    mid = db.execute(
        "INSERT INTO measurements (practice_id, patient_id, note_id, kind, joint, side, value, unit, goal, measured_on,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, note_id, kind, joint, side, value, unit, goal,
         measured_on or zurich_today().isoformat(), current_user_id(), utcnow()),
    )
    audit(db, "create", "measurement", mid, patient_id)
    return mid


def measurement_series(db: Database, patient_id: int) -> list[dict]:
    """Verlauf je Messart/Gelenk/Seite (für Mini-Diagramme)."""
    series: dict[tuple, dict] = {}
    for m in measurements(db, patient_id):
        key = (m["kind"], m["joint"], m["side"])
        s = series.setdefault(key, {"kind": m["kind"], "joint": m["joint"], "side": m["side"], "unit": m["unit"],
                                    "points": [], "goal": None})
        s["points"].append((m["measured_on"], m["value"]))
        if m["goal"] is not None:
            s["goal"] = m["goal"]
    return list(series.values())


# --------------------------------------------------------------------------- Messreihen in der Patienten-App
def shares(db: Database, patient_id: int) -> set[tuple[str, str, str]]:
    return {(r["kind"], r["joint"], r["side"]) for r in db.query(
        "SELECT kind, joint, side FROM measurement_shares WHERE practice_id = ? AND patient_id = ?", (current_practice_id(), patient_id))}


def set_share(db: Database, patient_id: int, kind: str, joint: str | None, side: str | None, on: bool) -> None:
    key = (current_practice_id(), patient_id, kind, joint or "", side or "")
    if on:
        db.execute("INSERT INTO measurement_shares (practice_id, patient_id, kind, joint, side, shared_by, shared_at)"
                   " VALUES (?,?,?,?,?,?,?) ON CONFLICT (practice_id, patient_id, kind, joint, side) DO NOTHING",
                   (*key, current_user_id(), utcnow()))
    else:
        db.execute("DELETE FROM measurement_shares WHERE practice_id = ? AND patient_id = ? AND kind = ? AND joint = ? AND side = ?", key)
    audit(db, "update", "measurement_share", None, patient_id, {"kind": kind, "joint": joint, "side": side, "shared": on})


def shared_series(db: Database, patient_id: int) -> list[dict]:
    keys = shares(db, patient_id)
    return [s for s in measurement_series(db, patient_id) if (s["kind"], s["joint"] or "", s["side"] or "") in keys]
