"""«Heute» – im Therapie-Bildschirm der eigene Tag, im Empfang die Ankünfte aller."""
from __future__ import annotations

from datetime import datetime, timedelta

from aiohttp import web

from ...core import access
from ...core.keys import DB
from ...core.slots import collect
from ...core.timeutil import TZ, from_iso, now_local
from ...core.web import render
from ..patients import api as patients_api
from . import absences, repo
from .views import _int, _parse_day, off_labels


async def therapie_today(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    user = request["user"]
    d = _parse_day(request.query.get("d"))
    delegations = access.delegations_for(db, user["id"])
    who = _int(request.query.get("fuer"))
    if who not in [x["from_user"] for x in delegations]:
        who = user["id"]
    appts = repo.on_day(db, d, therapist_id=who)
    sessions = repo.session_numbers(db, appts)
    now = datetime.now(TZ)
    upcoming = [a for a in appts if a["patient_id"] and a["status"] in ("booked", "arrived") and from_iso(a["ends_at"]) >= now]
    nxt = upcoming[0] if upcoming else None
    next_ctx = None
    if nxt is not None:
        d0 = now_local().date()
        findings = patients_api.findings(db, nxt["patient_id"])
        today_iso = d0.isoformat()
        next_ctx = {
            "a": nxt, "session": sessions.get(nxt["id"]), "beyond": repo.beyond_prescription(sessions.get(nxt["id"])),
            "restrictions": [f for f in findings if f["kind"] == "restriction" and not (f["until"] and f["until"] < today_iso)],
            "diagnoses": [f for f in findings if f["kind"] in ("diagnosis", "surgery")][:2],
            "actions": [c["ctx"] for c in await collect(request, "therapie.appointment.action", nxt)],
            "extra": await collect(request, "therapie.today.next", nxt),
        }
    rows = []
    for a in appts:
        actions = [c["ctx"] for c in await collect(request, "therapie.appointment.action", a)] \
            if a["patient_id"] and a["status"] in ("booked", "arrived", "done") else []
        rows.append({"a": a, "session": sessions.get(a["id"]), "beyond": repo.beyond_prescription(sessions.get(a["id"])),
                     "start": from_iso(a["starts_at"]),
                     "end": from_iso(a["ends_at"]), "past": from_iso(a["ends_at"]) < now, "actions": actions})
    todo = [c["ctx"] for c in await collect(request, "therapie.today.todo")]
    cards = await collect(request, "therapie.today.card")
    return await render(request, "agenda/today.html", {
        "day": d, "rows": rows, "next": next_ctx, "todo": todo, "cards": cards, "delegations": delegations,
        "offs": off_labels(request, absences.periods(db, d, d, user_id=who)),
        "who": who, "who_name": next((x["from_name"] for x in delegations if x["from_user"] == who), None),
        "prev_day": (d - timedelta(days=1)).isoformat(), "next_day": (d + timedelta(days=1)).isoformat(),
        "is_today": d == now_local().date(),
    })


async def empfang_today(request: web.Request) -> web.StreamResponse:
    """Ankünfte des Tages: wer kommt wann zu wem, Anmelden mit einem Tipp. Keine medizinischen Inhalte."""
    db = request.app[DB]
    d = _parse_day(request.query.get("d"))
    locs = repo.locations(db)
    loc_id = _int(request.query.get("loc"))
    appts = [a for a in repo.on_day(db, d, location_id=loc_id) if a["patient_id"]]
    now = datetime.now(TZ)
    rows = [{"a": a, "start": from_iso(a["starts_at"]), "end": from_iso(a["ends_at"]),
             "late": a["status"] == "booked" and from_iso(a["starts_at"]) < now - timedelta(minutes=10)} for a in appts]
    counts = {st: sum(1 for r in rows if r["a"]["status"] == st) for st in ("booked", "arrived", "done", "cancelled", "no_show")}
    offs = absences.periods(db, d, d, everyone=True)
    return await render(request, "agenda/r_today.html", {
        "day": d, "rows": rows, "counts": counts, "locations": locs, "loc_id": loc_id,
        "notices": off_labels(request, [o for o in offs if o.user_id is None]),
        "absent": off_labels(request, [o for o in offs if o.user_id is not None]),
        "waiting": len(repo.waitlist(db)), "is_today": d == now_local().date(),
        "prev_day": (d - timedelta(days=1)).isoformat(), "next_day": (d + timedelta(days=1)).isoformat(),
    })
