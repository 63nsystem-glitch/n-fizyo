"""Übersicht der Leitung (/leitung/uebersicht): Kennzahlen ohne medizinische Inhalte.

Eigene Zahlen: Sitzungen pro Woche, Nicht-erschienen-Quote, Auslastung (über die Agenda-Schnittstelle),
neue Patient/innen und Sprachen (Kern), KI-Leistung der Box (Hintergrundaufgaben).
Andere Module steuern Kacheln («leitung.overview.tile»), offene Arbeit («leitung.overview.work»)
und Nutzungszahlen («leitung.overview.usage») bei. Alles wird direkt aus der Datenbank der Praxis berechnet.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from aiohttp import web

from ...core import languages
from ...core import patients as core_patients
from ...core.keys import DB
from ...core.modules import ModuleContext, NavItem
from ...core.slots import collect
from ...core.tenant import current_practice_id
from ...core.timeutil import day_bounds_utc, from_iso, today, week_start
from ...core.web import render
from ..agenda import api as agenda_api

WEEKS = 8


def _week_rows(db) -> list[dict]:
    start = week_start(today()) - timedelta(weeks=WEEKS - 1)
    lo, _ = day_bounds_utc(start)
    _, hi = day_bounds_utc(today() + timedelta(days=7))
    counts: Counter = Counter()
    no_show: Counter = Counter()
    for a in agenda_api.between(db, lo, hi, patients_only=True):
        if a["status"] in ("done", "no_show"):
            wk = week_start(from_iso(a["starts_at"]).date())
            (counts if a["status"] == "done" else no_show)[wk] += 1
    return [{"week": start + timedelta(weeks=i), "done": counts[start + timedelta(weeks=i)],
             "no_show": no_show[start + timedelta(weeks=i)]} for i in range(WEEKS)]


def _bars(rows: list[dict]) -> dict:
    """Säulen (eine Reihe, eine Farbe) als SVG-Geometrie."""
    top = max([r["done"] for r in rows] + [4])
    step = 4 if top <= 16 else (5 if top <= 40 else 10)
    ymax = ((top + step - 1) // step) * step
    w, h, left, bottom = 560, 180, 28, 24
    plot_h = h - bottom - 8
    bw = (w - left) / len(rows)
    bars = []
    for i, r in enumerate(rows):
        bh = 0 if not r["done"] else max(2, r["done"] / ymax * plot_h)
        bars.append({"x": left + i * bw + 6, "w": bw - 12, "y": 8 + plot_h - bh, "h": bh, "r": r,
                     "label": f"{r['week'].day:02d}.{r['week'].month:02d}."})
    ticks = [{"v": v, "y": 8 + plot_h - v / ymax * plot_h} for v in range(0, ymax + 1, step)]
    return {"w": w, "h": h, "left": left, "base": 8 + plot_h, "bars": bars, "ticks": ticks}


def _utilisation(db) -> list[dict]:
    """Gebuchte Minuten vs. Arbeitszeit diese Woche, pro Person."""
    ws = week_start(today())
    lo, _ = day_bounds_utc(ws)
    _, hi = day_bounds_utc(ws + timedelta(days=6))
    out = []
    for u in core_patients.therapists(db):
        avail = agenda_api.available_minutes_per_week(db, u["id"])
        if not avail:
            continue
        booked = sum(int((from_iso(a["ends_at"]) - from_iso(a["starts_at"])).total_seconds() // 60)
                     for a in agenda_api.between(db, lo, hi, therapist_id=u["id"], patients_only=True)
                     if a["status"] in ("booked", "arrived", "done"))
        out.append({"name": u["name"], "booked_h": booked / 60, "avail_h": avail / 60, "pct": min(100, round(booked / avail * 100))})
    return out


async def overview(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    pid = current_practice_id()
    lang = request["lang"]
    weeks = _week_rows(db)
    month_lo, _ = day_bounds_utc(today().replace(day=1))
    langs = Counter(r["code"] or "de" for r in db.query(
        "SELECT COALESCE(spoken_lang, lang) AS code FROM patients WHERE practice_id = ? AND archived = 0", (pid,)))
    total_patients = sum(langs.values()) or 1
    week_lo, _ = day_bounds_utc(today() - timedelta(days=7))
    ai_perf = db.one(
        "SELECT COUNT(*) AS n,"
        " AVG((julianday(started_at) - julianday(created_at)) * 86400) AS wait_avg,"
        " MAX((julianday(started_at) - julianday(created_at)) * 86400) AS wait_max,"
        " AVG((julianday(finished_at) - julianday(started_at)) * 86400) AS work_avg,"
        " SUM((julianday(finished_at) - julianday(started_at)) * 86400) AS work_sum"
        " FROM jobs WHERE practice_id = ? AND status = 'done' AND created_at >= ? AND started_at IS NOT NULL AND finished_at IS NOT NULL",
        (pid, week_lo))
    no_show_4w = sum(r["no_show"] for r in weeks[-4:])
    done_4w = sum(r["done"] for r in weeks[-4:])
    own_tiles = [
        {"label_key": "stats.sessions_week", "value": weeks[-1]["done"], "sub_key": "stats.last_week", "sub_args": {"n": weeks[-2]["done"]}},
        {"label_key": "stats.new_patients", "value": db.value("SELECT COUNT(*) FROM patients WHERE practice_id = ? AND created_at >= ?",
                                                             (pid, month_lo), 0), "sub_key": "stats.this_month"},
        {"label_key": "stats.no_show", "value": f"{round(no_show_4w / (no_show_4w + done_4w) * 100) if (no_show_4w + done_4w) else 0} %",
         "sub_key": "stats.last_4_weeks"},
    ]
    return await render(request, "stats/overview.html", {
        "tiles": [c["ctx"] for c in await collect(request, "leitung.overview.tile")] + own_tiles,
        "work": [c["ctx"] for c in await collect(request, "leitung.overview.work")],
        "usage": [c["ctx"] for c in await collect(request, "leitung.overview.usage")],
        "cards": await collect(request, "leitung.overview.card"),
        "weeks": weeks, "chart": _bars(weeks), "util": _utilisation(db), "ai_perf": ai_perf,
        "lang_rows": [{"name": languages.name_for(c, lang), "n": n, "pct": round(n / total_patients * 100)} for c, n in langs.most_common()],
        "foreign": sum(n for c, n in langs.items() if c not in ("de", "fr", "gsw")), "total_patients": sum(langs.values()),
    })


def setup(ctx: ModuleContext) -> None:
    ctx.page("leitung", "/uebersicht", overview)
    ctx.nav("leitung", NavItem("nav.overview", "leaderboard", "/uebersicht", order=10, short_key="nav.overview_short"))
