"""Daten für Kurzüberblick und Übergabe – nur Lesen, nur über die Schnittstellen der anderen Module, ohne KI."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from aiohttp import web

from ...core.keys import DB
from ...core.modules import enabled
from ...core.timeutil import TZ, from_iso, today
from ..agenda import api as agenda_api
from ..patients import api as patients_api

HP_WINDOW = 21       # höchstens 3 Wochen Übungstage im Kurzüberblick
HANDOVER_NOTES = 3   # Notizen auf der Karte (die ganze Übergabe zeigt mehr)


# --------------------------------------------------------------------------- Bausteine
def done_history(request: web.Request, patient_id: int) -> list:
    """Erledigte Termine bis jetzt (neueste zuerst), mit Therapeut/in."""
    now = datetime.now(TZ)
    return [a for a in agenda_api.for_patient(request.app[DB], patient_id, limit=200)
            if a["status"] == "done" and from_iso(a["starts_at"]) <= now]


def approved_notes(request: web.Request, patient_id: int, limit: int = 30) -> list:
    if not enabled(request, "documentation"):
        return []
    from ..documentation import api as doc_api

    return [n for n in doc_api.for_patient(request.app[DB], patient_id, limit=limit) if n["status"] == "approved"]


def trends(db, patient_id: int) -> list[dict]:
    out = []
    for s in patients_api.measurement_series(db, patient_id):
        pts = s["points"]
        if not pts:
            continue
        out.append({"kind": s["kind"], "joint": s["joint"], "side": s["side"], "unit": s["unit"], "goal": s["goal"],
                    "last": pts[-1][1], "last_on": pts[-1][0], "prev": pts[-2][1] if len(pts) > 1 else None, "first": pts[0][1],
                    "count": len(pts)})
    return out


def clinical(db, patient_id: int) -> dict:
    day = today().isoformat()
    fs = patients_api.findings(db, patient_id)
    return {
        "restrictions": [f for f in fs if f["kind"] == "restriction" and not (f["until"] and f["until"] < day)],
        "clearances": [f for f in fs if f["kind"] == "clearance"],
        "diagnoses": [f for f in fs if f["kind"] in ("diagnosis", "surgery")],
        "alerts": [f for f in fs if f["kind"] in ("red_flag", "complication")],
    }


def _text(request: web.Request, original: str | None, translation: str | None, lang: str | None) -> str:
    return (translation if translation and lang != request["lang"] else original) or ""


def messages_since(request: web.Request, patient_id: int, since: str | None) -> list[dict]:
    if not enabled(request, "messages"):
        return []
    from ..messages import api as msg_api

    out = []
    for m in msg_api.incoming_since(request.app[DB], patient_id, since):
        out.append({"id": m["id"], "problem": m["kind"] == "problem", "text": _text(request, m["body"], m["translation"], m["lang"]),
                    "original": m["body"] if m["translation"] and m["lang"] != request["lang"] else None, "lang": m["lang"],
                    "pain": m["pain"], "at": m["created_at"], "open": m["kind"] == "problem" and not m["handled_at"],
                    "unread": not m["read_at"]})
    return out


def open_messages(request: web.Request, patient_id: int) -> dict | None:
    if not enabled(request, "messages"):
        return None
    from ..messages import api as msg_api

    return msg_api.open_items(request.app[DB], patient_id)


def new_intakes(request: web.Request, patient_id: int) -> list[dict]:
    if not enabled(request, "intake"):
        return []
    from ..intake import api as intake_api

    return [{"id": i["id"], "pain": i["pain"], "at": i["created_at"], "text": _text(request, i["text_original"], i["text_translated"], i["lang"])}
            for i in intake_api.new_for_patient(request.app[DB], patient_id)]


def home_days(request: web.Request, patient_id: int, start: date, end: date) -> dict | None:
    if not enabled(request, "homeprogram"):
        return None
    from ..homeprogram import api as hp_api

    return hp_api.practice_days(request.app[DB], patient_id, start, end)


def mtt_days(request: web.Request, patient_id: int, start: date, end: date) -> dict | None:
    if not enabled(request, "mtt"):
        return None
    from ..mtt import api as mtt_api

    return mtt_api.trainings(request.app[DB], patient_id, start, end)


def home_items(request: web.Request, patient_id: int) -> list[dict]:
    if not enabled(request, "homeprogram"):
        return []
    from ..homeprogram import api as hp_api

    return hp_api.program_items(request.app[DB], patient_id)


def rx_state(request: web.Request, patient_id: int) -> dict | None:
    if not enabled(request, "rxwatch"):
        return None
    from ..rxwatch import api as rx_api

    state = rx_api.status(request, patient_id)
    if state is not None:
        urgent = [ln for ln in state["lines"] if ln["level"] == "act"] or [ln for ln in state["lines"] if ln["level"] == "soon"]
        state["top"] = urgent[0] if urgent else None
    return state


def goal_lang(patient) -> str:
    """Sprache des Ziels «in Worten der Patientin / des Patienten»: gesprochene Sprache, sonst Sprache der App."""
    from ...core import languages

    spoken = patient["spoken_lang"]
    return spoken if spoken in languages.BY_CODE and spoken != "gsw" else (patient["lang"] or "de")


def _same(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def goal(request: web.Request, patient) -> dict | None:
    """Ziel in der Sprache der Therapie (wie das Anliegen): Übersetzung aus dem Zwischenspeicher, Original sichtbar.
    Fehlt die Übersetzung, wird sie im Hintergrund erstellt (Aufgabe «translate_goal») – bis dahin das Original."""
    from ...ai.translate import cached
    from ...core.keys import AI, JOBS
    from ...core.tenant import current_practice_id

    text = (patient["goal"] or "").strip()
    if not text:
        return None
    src, target = goal_lang(patient), request["lang"]
    if src == target:
        return {"text": text, "original": None}
    db = request.app[DB]
    row = cached(db, current_practice_id(), text, src, target)
    if row is not None and row["text"] and not _same(row["text"], text):
        return {"text": row["text"], "original": text, "lang": src, "demo": row["engine"] == "demo"}
    if row is None and request.app[AI].can_translate() and not db.value(
            "SELECT 1 FROM jobs WHERE practice_id = ? AND kind = 'translate_goal' AND ref_id = ? AND created_at >= ?",
            (current_practice_id(), patient["id"], patient["updated_at"])):
        request.app[JOBS].enqueue(current_practice_id(), "translate_goal", patient["id"])
    return {"text": text, "original": None}


# --------------------------------------------------------------------------- Kurzüberblick vor dem Termin
def briefing(request: web.Request, patient, appt=None) -> dict:
    """Was man in 15 Sekunden vor dem Termin wissen muss."""
    db = request.app[DB]
    pid = patient["id"]
    day = today()
    hist = done_history(request, pid)
    notes = approved_notes(request, pid, limit=10)
    last = hist[0] if hist else None
    last_note = notes[0] if notes else None
    last_day = from_iso(last["starts_at"]).date() if last else None
    last_at = last["starts_at"] if last else None
    try:
        nd = date.fromisoformat(last_note["session_date"]) if last_note else None
    except ValueError:              # ungültiges Datum einer alten Notiz: wie ohne Notiz
        nd = None
    if nd is not None:
        if last_day is None or nd > last_day:
            last_day, last_at = nd, nd.isoformat()
    if last and (last_note is None or last_note["session_date"] <= last_day.isoformat()):
        last_by = (last["therapist_id"], last["therapist_name"])
    elif last_note:
        last_by = (last_note["author_id"], last_note["author_name"])
    else:
        last_by = (None, None)
    viewer = appt["therapist_id"] if appt else request["user"]["id"]
    start = (last_day + timedelta(days=1)) if last_day else day - timedelta(days=6)
    start = min(max(start, day - timedelta(days=HP_WINDOW - 1)), day)
    return {
        "patient": patient, "appt": appt, "session": agenda_api.session_number(db, appt) if appt else None,
        "last": last, "last_day": last_day, "last_note": last_note, "last_by_name": last_by[1],
        "takeover": last_by[0] is not None and last_by[0] != viewer,
        "hp": home_days(request, pid, start, day), "hp_since_last": last_day is not None, "mtt": mtt_days(request, pid, start, day),
        "messages": messages_since(request, pid, last_at or (day - timedelta(days=7)).isoformat()),
        "intakes": new_intakes(request, pid), "rx": rx_state(request, pid), "trends": trends(db, pid),
        "goal": goal(request, patient), **clinical(db, pid),
    }


# --------------------------------------------------------------------------- Übergabe
def handover(request: web.Request, patient, full: bool = False) -> dict | None:
    """Übernahme (noch nie selbst behandelt, zuletzt jemand anders) oder «während Ihrer Abwesenheit» (andere haben
    nach Ihrer letzten Sitzung behandelt). Sonst None."""
    db = request.app[DB]
    pid = patient["id"]
    me = request["user"]["id"]
    hist = done_history(request, pid)
    others = [a for a in hist if a["therapist_id"] != me]
    if not hist or not others or hist[0]["therapist_id"] == me:
        return None
    mine = [a for a in hist if a["therapist_id"] == me]
    if mine:
        mode, cutoff = "away", mine[0]["starts_at"]
        sessions = [a for a in others if a["starts_at"] > cutoff]
    else:
        mode, cutoff, sessions = "takeover", None, others
    names: list[str] = []
    for a in sessions:
        if a["therapist_name"] not in names:
            names.append(a["therapist_name"])
    notes = [n for n in approved_notes(request, pid, limit=40) if n["author_id"] != me]
    if cutoff:
        notes = [n for n in notes if n["session_date"] > from_iso(cutoff).date().isoformat()]
    notes = notes if full else notes[:HANDOVER_NOTES]
    day = today()
    upcoming = agenda_api.for_patient(db, pid, future_only=True, limit=6 if full else 3)
    since = from_iso(cutoff).isoformat() if cutoff else (day - timedelta(days=14)).isoformat()
    return {
        "patient": patient, "mode": mode, "names": names, "n": len(sessions),
        "first": from_iso(sessions[-1]["starts_at"]).date() if sessions else None,
        "last": from_iso(sessions[0]["starts_at"]).date() if sessions else None,
        "notes": notes, "rx": rx_state(request, pid), "trends": trends(db, pid), "goal": goal(request, patient),
        "hp": home_days(request, pid, day - timedelta(days=13), day), "hp_items": home_items(request, pid) if full else [],
        "messages": messages_since(request, pid, since)[: (10 if full else 2)], "open": open_messages(request, pid),
        "intakes": new_intakes(request, pid), "upcoming": upcoming, "full": full, **clinical(db, pid),
    }
