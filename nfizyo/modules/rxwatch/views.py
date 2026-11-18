"""Verordnungen im Blick – Liste (Therapie: eigene Patient/innen; Empfang: alle), Karte in Akte und Stammblatt, «Heute»."""
from __future__ import annotations

from datetime import date

from aiohttp import web

from ...core import access
from ...core.keys import DB
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.screens import prefix
from ...core.web import int_param, redirect_with_flash, render
from ..patients import api as patients_api
from . import api, repo

ORDER = {"act": 0, "soon": 1, "info": 2, "ok": 3}


def _therapie(request: web.Request) -> bool:
    return request["screen"].key == "therapie"


def _renewal(request: web.Request, row):
    if not enabled(request, "reports"):
        return None
    from ..reports import api as reports_api

    return reports_api.renewal_since(request.app[DB], row["patient_id"], row["created_at"])


def _states(request: web.Request, rows, *, with_lines: bool = True) -> list[dict]:
    db = request.app[DB]
    out = repo.evaluate_many(db, rows, renewal_of=lambda r: _renewal(request, r),
                             approval_of=lambda r: api.approval(request, r["patient_id"]))
    if with_lines:
        for s in out:
            s["lines"] = api.lines(request, s)
    return out


GROUP_LIMIT = 50   # je Gruppe zuerst 50 zeigen: grosse Praxen hätten sonst eine Seite mit Tausenden Einträgen (mehrere MB)


def _only(request: web.Request):
    return access.patient_ids_sql(request["user"]["id"]) if _therapie(request) else None


def _safe_back(request: web.Request, value, default: str) -> str:
    value = str(value or "")
    return value if value.startswith(prefix(request) + "/") and "//" not in value[1:] else default


# --------------------------------------------------------------------------- Liste
async def overview(request: web.Request) -> web.StreamResponse:
    states = _states(request, repo.active(request.app[DB], only=_only(request)), with_lines=False)
    groups = {lvl: [s for s in states if s["level"] == lvl] for lvl in ("act", "soon", "info")}
    show_all = request.query.get("alle", "")
    totals = {}
    for lvl, g in groups.items():
        g.sort(key=lambda s: s["first_uncovered"] or s["start_deadline"] or date.max)
        totals[lvl] = len(g)
        if show_all != lvl:
            del g[GROUP_LIMIT:]
        for s in g:                                   # Textzeilen nur für das, was die Seite zeigt
            s["lines"] = api.lines(request, s)
    return await render(request, "rxwatch/list.html", {
        "groups": groups, "totals": totals, "ok_count": sum(1 for s in states if s["level"] == "ok"), "vias": repo.VIAS,
        "medical": _therapie(request),
    })


def _load_prescription(request: web.Request):
    pr = repo.get_prescription(request.app[DB], int_param(request, "prid"))
    if pr is None or (_therapie(request) and not patients_api.can_open(request, pr["patient_id"])):
        raise web.HTTPNotFound()
    return pr


async def request_add(request: web.Request) -> web.StreamResponse:
    pr = _load_prescription(request)
    form = await request.post()
    repo.add_request(request.app[DB], pr["patient_id"], pr["id"], str(form.get("kind") or "renewal"), str(form.get("via") or "phone"),
                     str(form.get("note") or ""))
    raise redirect_with_flash(_safe_back(request, form.get("back"), f"{prefix(request)}/verordnungen"), "rx.requested")


async def request_close(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    q = repo.get_request(db, int_param(request, "qid"))
    if q is None or (_therapie(request) and not patients_api.can_open(request, q["patient_id"])):
        raise web.HTTPNotFound()
    form = await request.post()
    repo.close_request(db, q["id"], "withdrawn" if form.get("status") == "withdrawn" else "done")
    raise redirect_with_flash(_safe_back(request, form.get("back"), f"{prefix(request)}/verordnungen"), "rx.closed")


# --------------------------------------------------------------------------- Steckplätze
async def card_patient(request: web.Request, patient) -> dict | None:
    rows = repo.active(request.app[DB], patient_id=patient["id"])
    if not rows:
        return None
    return {"s": _states(request, rows)[0], "patient": patient, "vias": repo.VIAS, "medical": _therapie(request),
            "back": request.path}


def todo(request: web.Request) -> dict | None:
    states = _states(request, repo.active(request.app[DB], only=_only(request)))
    n = sum(1 for s in states if s["level"] in ("act", "soon"))
    return {"label_key": "rx.todo", "icon": "assignment_late", "href": f"{prefix(request)}/verordnungen", "n": n} if n else None


def badge_act(db, practice_id: int, user) -> int:
    """Empfang: Verordnungen, bei denen jetzt gehandelt werden muss (wenige Abfragen für alle Verordnungen der Praxis)."""
    return sum(1 for s in repo.evaluate_many(db, repo.active(db)) if s["level"] == "act")


def setup(ctx: ModuleContext) -> None:
    for screen in ("therapie", "empfang"):
        ctx.page(screen, "/verordnungen", overview)
        ctx.page(screen, "/verordnungen/{prid:\\d+}/anfrage", request_add, ("POST",))
        ctx.page(screen, "/verordnungen/anfrage/{qid:\\d+}", request_close, ("POST",))
    ctx.nav("empfang", NavItem("rx.nav", "assignment_late", "/verordnungen", order=16, badge="rx_act", short_key="rx.nav_short"))
    ctx.badge("rx_act", badge_act)
    ctx.slot("therapie.patient.right", 11, card_patient, "rxwatch/card.html")
    ctx.slot("empfang.patient.card", 15, card_patient, "rxwatch/card.html")
    ctx.slot("therapie.today.todo", 35, todo)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('rx_requests',), (
        "UPDATE rx_requests SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
