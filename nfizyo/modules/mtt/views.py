"""MTT-Fläche – Therapie: Geräte, Trainingsplan in der Akte, Live-Ansicht, Vorschläge. Patienten-App: Training mit QR-Code."""
from __future__ import annotations

from datetime import timedelta

from aiohttp import web

from ...core import access
from ...core.charts import sparkline_svg
from ...core.keys import DB
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.screens import prefix
from ...core.timeutil import from_iso, today
from ...core.web import int_param, public_url, redirect_with_flash, render
from ...knowledge.regions import FACES
from ..patients import api as patients_api
from . import repo


def _num(v, cast=int, limit: float = 1000):
    """Zahl aus dem Formular: leer, unsinnig (nan, inf, 1e300, 30 Stellen) oder ausserhalb 0…limit → None.
    Sehr grosse Ganzzahlen würden sonst beim Speichern abstürzen, «inf» würde die Laststeigerung verderben."""
    s = str(v or "").strip().replace(",", ".")[:12]
    try:
        x = cast(s)
    except (ValueError, OverflowError):
        return None
    return x if 0 <= x <= limit else None


def _scale(v) -> int | None:
    """0–10 (Anstrengung, Schmerz) oder None."""
    s = str(v or "").strip()
    return min(10, int(s)) if s.isdigit() else None


def _only(request: web.Request):
    return access.patient_ids_sql(request["user"]["id"])


def _item_view(db, it) -> dict:
    hist = repo.history(db, it["id"], days=8)
    loads = [(d["day"], max((s["load"] or 0) for s in d["sets"])) for d in hist if any(s["load"] for s in d["sets"])]
    last = hist[-1] if hist else None
    return {"it": it, "last": last, "svg": sparkline_svg(loads, None, w=160, h=36) if len(loads) > 1 else None,
            "last_effort": max((s["effort"] for s in last["sets"] if s["effort"] is not None), default=None) if last else None,
            "last_pain": max((s["pain"] for s in last["sets"] if s["pain"] is not None), default=None) if last else None}


# --------------------------------------------------------------------------- Therapie: Plan in der Akte
async def plan_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    if request.method == "POST":
        form = await request.post()
        back = f"{prefix(request)}/patienten/{patient['id']}/mtt"
        if form.get("action") == "approve":
            plan = repo.current_plan(db, patient["id"])
            if plan is None or not repo.items(db, plan["id"]):
                raise redirect_with_flash(back, "mtt.plan_empty")
            repo.approve(db, plan, str(form.get("goal") or "").strip()[:200] or None, request["lang"])
            raise redirect_with_flash(back, "mtt.approved")
        m = repo.machine(db, _num(form.get("machine_id"), limit=10**12) or 0)
        if m is None or not m["active"]:
            raise redirect_with_flash(back, "mtt.choose_machine")
        repo.add_item(db, repo.current_plan(db, patient["id"], create=True), m)
        raise redirect_with_flash(back, "mtt.added")
    plan = repo.current_plan(db, patient["id"])
    its = [_item_view(db, it) for it in repo.items(db, plan["id"])] if plan else []
    used = {x["it"]["machine_id"] for x in its}
    return await patients_api.file_page(request, patient, "mtt", "mtt/plan.html", {
        "plan": plan, "rows": its, "machines": [m for m in repo.machines(db) if m["id"] not in used],
        "suggestions": repo.open_suggestions(db, patient_id=patient["id"]), "visits": repo.visits(db, patient["id"]),
        "has_machines": bool(repo.machines(db)),
    })


def _own_item(request: web.Request):
    it = repo.item(request.app[DB], int_param(request, "iid"))
    if it is None or not patients_api.can_open(request, it["patient_id"]):
        raise web.HTTPNotFound()
    return it


async def item_post(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    it = _own_item(request)
    form = await request.post()
    if form.get("remove") == "1":
        repo.remove_item(db, it["id"])
    else:
        repo.update_item(db, it["id"], sets=_num(form.get("sets"), limit=20), reps=_num(form.get("reps"), limit=500),
                         load=_num(form.get("load"), float, 500), minutes=_num(form.get("minutes"), limit=300), setting=str(form.get("setting") or "").strip()[:80] or None,
                         note=str(form.get("note") or "").strip()[:200] or None, note_lang=request["lang"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{it['patient_id']}/mtt", "common.saved")


async def suggestion_post(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    g = repo.suggestion(db, int_param(request, "sid"))
    if g is None or not patients_api.can_open(request, g["patient_id"]):
        raise web.HTTPNotFound()
    form = await request.post()
    accept = form.get("decision") == "accept"
    repo.decide(db, g["id"], accept)
    back = str(form.get("back") or "")
    if not back.startswith(prefix(request) + "/") or "//" in back[1:]:
        back = f"{prefix(request)}/patienten/{g['patient_id']}/mtt"
    raise redirect_with_flash(back, "mtt.sugg_accepted" if accept else "mtt.sugg_dismissed")


# --------------------------------------------------------------------------- Therapie: Live-Ansicht der Fläche
def _live_rows(request: web.Request) -> dict:
    db = request.app[DB]
    rows = []
    for v in repo.live(db, only=_only(request)):
        plan_items = repo.items(db, v["plan_id"]) if v["plan_id"] else []
        sets = repo.sets_of_visit(db, v["id"])
        by_item: dict[int, list] = {}
        for s in sets:
            by_item.setdefault(s["item_id"], []).append(s)
        names = {i["id"]: i["machine_name"] for i in plan_items}
        last = sets[-1] if sets else None
        pains = [s["pain"] for s in sets if s["pain"] is not None]
        efforts = [s["effort"] for s in sets if s["effort"] is not None]
        rows.append({"v": v, "started": from_iso(v["started_at"]), "done_items": sum(1 for i in plan_items if by_item.get(i["id"])),
                     "n_items": len(plan_items), "n_sets": len(sets), "last": last, "last_machine": names.get(last["item_id"]) if last else None,
                     "max_pain": max(pains) if pains else None, "max_effort": max(efforts) if efforts else None,
                     "items": [{"name": i["machine_name"], "n": len(by_item.get(i["id"], [])), "sets": i["sets"]} for i in plan_items]})
    total = repo.live_count(db)
    return {"rows": rows, "others": max(0, total - len(rows)),
            "suggestions": repo.open_suggestions(db, only=_only(request))}


async def live_page(request: web.Request) -> web.StreamResponse:
    return await render(request, "mtt/live.html", _live_rows(request))


async def live_fragment(request: web.Request) -> web.StreamResponse:
    return await render(request, "mtt/_live.html", _live_rows(request))


# --------------------------------------------------------------------------- Therapie: Geräte und QR-Codes
async def machines_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    if request.method == "POST":
        form = await request.post()
        back = f"{prefix(request)}/mtt/geraete"
        if str(form.get("toggle", "")).isdigit():
            m = repo.machine(db, int(form["toggle"]))
            if m is None:
                raise web.HTTPNotFound()
            repo.set_machine_active(db, m["id"], not m["active"])
            raise redirect_with_flash(back, "common.saved")
        name = str(form.get("name") or "").strip()
        if not name:
            raise redirect_with_flash(back, "mtt.machine_need_name")
        loc = _num(form.get("location_id"), limit=10**12)
        locs = {loc_row["id"] for loc_row in _locations(request)}
        repo.add_machine(db, name, str(form.get("kind") or "strength"), _num(form.get("step"), float, 50) or 2.5,
                         loc if loc in locs else None)
        raise redirect_with_flash(back, "mtt.machine_added")
    return await render(request, "mtt/machines.html", {"machines": repo.machines(db, include_inactive=True),
                                                       "locations": _locations(request), "kinds": repo.KINDS})


def _locations(request: web.Request):
    if not enabled(request, "agenda"):
        return []
    from ..agenda import api as agenda_api

    return agenda_api.locations(request.app[DB])


def _qr_svg(data: str) -> str:
    import segno

    return segno.make(data, error="m").svg_inline(scale=4, dark="#191C20", light="#FFFFFF", border=2)


async def qr_sheet(request: web.Request) -> web.StreamResponse:
    base = public_url(request)
    cards = [{"m": m, "svg": _qr_svg(f"{base}/p/mtt/{m['qr_token']}")} for m in repo.machines(request.app[DB])]
    return await render(request, "mtt/qr.html", {"cards": cards})


# --------------------------------------------------------------------------- Steckplätze (Therapie)
def tab(request: web.Request, patient) -> dict:
    n = len(repo.open_suggestions(request.app[DB], patient_id=patient["id"]))
    return {"key": "mtt", "label_key": "mtt.tab", "path": "mtt", "count": n}


async def card(request: web.Request, patient) -> dict | None:
    db = request.app[DB]
    plan = repo.current_plan(db, patient["id"])
    if plan is None:
        return None
    vs = repo.visits(db, patient["id"], limit=30)
    since = (today() - timedelta(days=27)).isoformat()
    recent = [v for v in vs if v["day"] >= since]
    return {"plan": plan, "n_items": len(repo.items(db, plan["id"])), "visits_4w": len(recent), "last": vs[0] if vs else None,
            "n_sugg": len(repo.open_suggestions(db, patient_id=patient["id"])), "patient": patient}


def todo(request: web.Request) -> dict | None:
    n = len(repo.open_suggestions(request.app[DB], only=_only(request)))
    return {"label_key": "mtt.todo", "icon": "trending_up", "href": f"{prefix(request)}/mtt", "n": n} if n else None


def badge_live(db, practice_id: int, user) -> int:
    return len(repo.live(db, only=access.patient_ids_sql(user["id"]))) if user is not None else 0


# --------------------------------------------------------------------------- Patienten-App
async def _localized(request: web.Request, text: str | None, src: str) -> dict | None:
    """Text der Therapeutin / des Therapeuten in der Sprache der Patientin – maschinell übersetzt (Original abrufbar)."""
    import asyncio

    from ...core.keys import AI
    from ...core.tenant import current_practice_id

    if not text:
        return None
    lang = request["lang"]
    if lang == src:
        return {"text": text, "machine": False, "lang": src}
    res = await asyncio.to_thread(request.app[AI].translate, request.app[DB], current_practice_id(), text, src, lang)
    if res.text and res.engine not in ("identity", "none"):
        return {"text": res.text, "machine": True, "original": text, "lang": lang}
    return {"text": text, "machine": False, "lang": src}


def _subscription(request: web.Request, check_in: bool) -> dict | None:
    if not enabled(request, "cashdesk"):
        return None
    from ..cashdesk import api as cash_api

    db, pid, day = request.app[DB], request["patient"]["id"], today().isoformat()
    return cash_api.checkin_visit(db, pid, day) if check_in else cash_api.active_subscription(db, pid, day)


def _start(request: web.Request, plan, source: str) -> int:
    vid, new = repo.start_visit(request.app[DB], request["patient"]["id"], plan["id"], today().isoformat(), source)
    if new:
        _subscription(request, check_in=True)
    return vid


async def p_training(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    plan = repo.active_plan(db, p["id"])
    if request.method == "POST":
        form = await request.post()
        if plan is None:
            raise web.HTTPNotFound()
        if form.get("action") == "end":
            v = repo.today_visit(db, p["id"], today().isoformat())
            if v:
                repo.end_visit(db, v["id"])
            raise redirect_with_flash("/p/training", "p.mtt.ended")
        _start(request, plan, "app")
        raise redirect_with_flash("/p/training", "p.mtt.started")
    visit = repo.today_visit(db, p["id"], today().isoformat())
    sets = repo.sets_of_visit(db, visit["id"]) if visit else []
    done: dict[int, int] = {}
    for s in sets:
        done[s["item_id"]] = done.get(s["item_id"], 0) + 1
    its = repo.items(db, plan["id"]) if plan else []
    return await render(request, "mtt/p_training.html", {
        "plan": plan, "items": its, "visit": visit, "done": done, "sub": _subscription(request, check_in=False),
        "goal": await _localized(request, plan["goal"], plan["goal_lang"]) if plan else None,
        "all_done": bool(its) and all(done.get(i["id"], 0) >= (i["sets"] or 1) for i in its),
    })


def _patient_item(request: web.Request):
    it = repo.item(request.app[DB], int_param(request, "iid"))
    if it is None or it["patient_id"] != request["patient"]["id"] or it["plan_status"] != "active":
        raise web.HTTPNotFound()
    return it


async def p_item(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    it = _patient_item(request)
    plan = repo.active_plan(db, it["patient_id"])
    if request.method == "POST":
        form = await request.post()
        vid = _start(request, plan, "app")
        repo.log_set(db, vid, it, reps=_num(form.get("reps"), limit=500), load=_num(form.get("load"), float, 500), minutes=_num(form.get("minutes"), limit=300),
                     effort=_scale(form.get("effort")), pain=_scale(form.get("pain")))
        repo.refresh_suggestion(db, it)
        raise redirect_with_flash(f"/p/training/{it['id']}", "p.mtt.saved")
    visit = repo.today_visit(db, it["patient_id"], today().isoformat())
    sets = [s for s in repo.sets_of_visit(db, visit["id"]) if s["item_id"] == it["id"]] if visit else []
    hist = [d for d in repo.history(db, it["id"], days=4) if d["day"] != today().isoformat()]
    return await render(request, "mtt/p_item.html", {"it": it, "sets": sets, "last": hist[-1] if hist else None, "faces": FACES,
                                                    "next_no": len(sets) + 1, "note": await _localized(request, it["note"], it["note_lang"])})


async def p_qr(request: web.Request) -> web.StreamResponse:
    """QR-Code am Gerät: Training starten (zählt als Eintritt) und direkt zur Übung an diesem Gerät."""
    db = request.app[DB]
    m = repo.machine_by_token(db, request.match_info["token"])
    plan = repo.active_plan(db, request["patient"]["id"])
    if m is None or plan is None:
        return await render(request, "mtt/p_qr.html", {"machine": m, "plan": plan}, status=404 if m is None else 200)
    it = next((i for i in repo.items(db, plan["id"]) if i["machine_id"] == m["id"]), None)
    _start(request, plan, "qr")
    if it is None:
        return await render(request, "mtt/p_qr.html", {"machine": m, "plan": plan})
    raise web.HTTPFound(f"/p/training/{it['id']}")


def p_card(request: web.Request) -> dict | None:
    db = request.app[DB]
    plan = repo.active_plan(db, request["patient"]["id"])
    if plan is None:
        return None
    visit = repo.today_visit(db, request["patient"]["id"], today().isoformat())
    return {"plan": plan, "visit": visit, "n_items": len(repo.items(db, plan["id"])), "sub": _subscription(request, check_in=False)}


def setup(ctx: ModuleContext) -> None:
    P = "/patienten/{pid:\\d+}/mtt"
    ctx.page("therapie", P, plan_page, ("GET", "POST"))
    ctx.page("therapie", "/mtt/plan/{iid:\\d+}", item_post, ("POST",))
    ctx.page("therapie", "/mtt/vorschlag/{sid:\\d+}", suggestion_post, ("POST",))
    ctx.page("therapie", "/mtt", live_page)
    ctx.page("therapie", "/mtt/live", live_fragment)
    ctx.page("therapie", "/mtt/geraete", machines_page, ("GET", "POST"))
    ctx.page("therapie", "/mtt/geraete/qr", qr_sheet)
    ctx.nav("therapie", NavItem("mtt.nav", "speed", "/mtt", order=82, badge="mtt_live"))
    ctx.badge("mtt_live", badge_live)
    ctx.slot("therapie.patient.tab", 47, tab)
    ctx.slot("therapie.patient.right", 32, card, "mtt/card.html")
    ctx.slot("therapie.today.todo", 40, todo)
    ctx.patient_page("/training", p_training, ("GET", "POST"))
    ctx.patient_page("/training/{iid:\\d+}", p_item, ("GET", "POST"))
    ctx.patient_page("/mtt/{token:[A-Za-z0-9_-]{8,40}}", p_qr)
    ctx.slot("patient.home.card", 22, p_card, "mtt/p_card.html")
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('mtt_plans', 'mtt_visits', 'mtt_sets', 'mtt_suggestions'), (
        "UPDATE mtt_plans SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE mtt_suggestions SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE mtt_sets SET visit_id = (SELECT k.id FROM mtt_visits k JOIN mtt_visits d ON d.id = "
        "mtt_sets.visit_id WHERE k.practice_id = :pid AND k.patient_id = :keep AND k.day = d.day) WHERE "
        "practice_id = :pid AND visit_id IN (SELECT d.id FROM mtt_visits d WHERE d.practice_id = :pid AND "
        "d.patient_id = :drop AND EXISTS (SELECT 1 FROM mtt_visits k WHERE k.practice_id = :pid AND k.patient_id = "
        ":keep AND k.day = d.day))",
        "DELETE FROM mtt_visits WHERE practice_id = :pid AND patient_id = :drop AND EXISTS (SELECT 1 FROM "
        "mtt_visits k WHERE k.practice_id = :pid AND k.patient_id = :keep AND k.day = mtt_visits.day)",
        "UPDATE mtt_visits SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE mtt_sets SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
