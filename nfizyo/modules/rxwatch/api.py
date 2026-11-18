"""Öffentliche Schnittstelle von «Verordnungen im Blick» – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from datetime import date

from aiohttp import web

from ...core.keys import DB, I18N
from ...core.modules import enabled
from ...core.timeutil import fmt_date, today
from . import repo


def status(request: web.Request, patient_id: int) -> dict | None:
    """Stand der aktuellen Verordnung: {"level", "flags", "total", "used", "left", "booked", "first_uncovered", …} oder None."""
    db = request.app[DB]
    rows = repo.active(db, patient_id=patient_id)
    if not rows:
        return None
    renewal = None
    if enabled(request, "reports"):
        from ..reports import api as reports_api

        renewal = reports_api.renewal_since(db, patient_id, rows[0]["created_at"])
    state = repo.evaluate(db, rows[0], renewal=renewal, approval=approval(request, patient_id))
    state["lines"] = lines(request, state)
    return state


def approval(request: web.Request, patient_id: int):
    """Kostengutsprache aus der Abrechnung (falls eingeschaltet)."""
    if not enabled(request, "billing"):
        return None
    from ..billing import api as billing_api

    return billing_api.cost_approval(request.app[DB], patient_id, today().isoformat())


ICON = {"act": "error", "soon": "warning", "info": "info", "ok": "check_circle"}


def lines(request: web.Request, state: dict) -> list[dict]:
    """Meldungen als Text in der Sprache der angemeldeten Person."""
    lang = request["lang"]

    def t(key: str, **kw) -> str:
        return request.app[I18N].t(lang, key, **kw)

    out = []
    for f in state["flags"]:
        p = dict(f.params)
        for k in ("date", "deadline"):
            if isinstance(p.get(k), date):
                p[k] = fmt_date(p[k], lang, weekday=True, year=False)
        if f.code == "incomplete":
            p["fields"] = ", ".join(t(f"rx.field.{x}") for x in p["fields"])
        if "via" in p:
            p["via"] = t(f"rx.via.{p['via']}")
        key = "rx.flag.kogu_nodate" if f.code == "kogu" and not p.get("date") else f"rx.flag.{f.code}"
        text = t(key, **{k: v for k, v in p.items() if k != "nudge"})
        if p.get("nudge"):
            text += " " + t("rx.nudge", days=p["days"])
        out.append({"code": f.code, "level": f.level, "icon": ICON[f.level], "text": text, "request_kind": f.request_kind,
                    "draft_id": p.get("id") if f.code == "renewal_draft" else None})
    return out
