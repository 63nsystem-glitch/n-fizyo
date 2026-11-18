"""Öffentliche Schnittstelle der Patientenakte – nur das hier dürfen andere Module benutzen.

- require_patient(request): Akte laden; im Therapie-Bildschirm nur eigene Patient/innen oder Vertretung (sonst 404).
- file_page(...): Seite im Rahmen der Akte (Kopfzeile, Reiter) – für Reiter anderer Module.
- Befunde und Messwerte lesen/schreiben.
"""
from __future__ import annotations

from aiohttp import web

from ...core import access, languages
from ...core import patients as core_patients
from ...core.keys import DB
from ...core.charts import sparkline_svg
from ...core.slots import collect, patient_tabs
from ...core.web import int_param, render
from . import repo
from .repo import FINDING_KINDS, finding_marks  # noqa: F401 - Teil der Schnittstelle


def require_patient(request: web.Request, param: str = "pid", patient_id: int | None = None):
    """Akte laden (aus der Adresse oder patient_id). Im Therapie-Bildschirm nur mit Zugriff, sonst 404."""
    db = request.app[DB]
    patient = core_patients.get(db, patient_id if patient_id is not None else int_param(request, param))
    if patient is None:
        raise web.HTTPNotFound()
    screen = request.get("screen")
    if screen is not None and screen.medical:
        relation = access.relation(db, request["user"]["id"], patient["id"])
        if relation is None:
            raise web.HTTPNotFound()  # fremde Akte: so tun, als gäbe es sie nicht
        request["patient_relation"] = relation
    return patient


def can_open(request: web.Request, patient_id: int | None) -> bool:
    """Darf die angemeldete Person diese Akte öffnen (eigene Patient/in oder Vertretung)?"""
    if not patient_id:
        return False
    return access.relation(request.app[DB], request["user"]["id"], patient_id) is not None


def my_patients(request: web.Request, limit: int = 500):
    """Patient/innen, deren Akte die angemeldete Person öffnen darf (für Auswahllisten)."""
    from ...core.tenant import current_practice_id

    sql, params = access.patient_ids_sql(request["user"]["id"])
    return request.app[DB].query(
        f"SELECT id, number, first_name, last_name, birthdate, lang, spoken_lang FROM patients WHERE practice_id = ? AND archived = 0"
        f" AND id IN ({sql}) ORDER BY last_name, first_name LIMIT ?", (current_practice_id(), *params, limit))


async def header(request: web.Request, patient) -> dict:
    lang = request["lang"]
    spoken = patient["spoken_lang"]
    return {
        "patient": patient,
        "initials": core_patients.initials(patient),
        "facts": [c["ctx"] for c in await collect(request, "therapie.patient.fact", patient)],
        "actions": [c["ctx"] for c in await collect(request, "therapie.patient.action", patient)],
        "relation": request.get("patient_relation"),
        "spoken_name": languages.name_for(spoken, lang) if spoken else None,
        "foreign": bool(spoken and spoken not in ("de", "fr", "gsw")),
    }


async def file_page(request: web.Request, patient, active: str, template: str, ctx: dict) -> web.Response:
    """Seite innerhalb der Akte (Therapie): gleiche Kopfzeile und Reiter wie die Übersicht."""
    full = await header(request, patient)
    full["tabs"] = await patient_tabs(request, patient, active)
    full.update(ctx)
    return await render(request, template, full)


def findings(db, patient_id: int, status: str | None = "active"):
    return repo.findings(db, patient_id, status)


def add_finding(db, patient_id: int, **kw) -> int:
    return repo.add_finding(db, patient_id, **kw)


def measurements(db, patient_id: int, kind: str | None = None):
    return repo.measurements(db, patient_id, kind)


def measurement_series(db, patient_id: int) -> list[dict]:
    return repo.measurement_series(db, patient_id)


def measurement_charts(db, patient_id: int, w: int = 280, h: int = 64) -> list[dict]:
    """Messreihen mit kleinem Diagramm (svg), erstem und letztem Wert."""
    out = []
    for s in repo.measurement_series(db, patient_id):
        values = [v for _, v in s["points"]]
        out.append({**s, "svg": sparkline_svg(s["points"], s["goal"], w=w, h=h), "last": values[-1], "first": values[0],
                    "count": len(values)})
    return out


def add_measurement(db, patient_id: int, kind: str, value: float, unit: str, **kw) -> int:
    return repo.add_measurement(db, patient_id, kind, value, unit, **kw)
