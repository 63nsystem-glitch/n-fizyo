"""Leitung › Einstellungen › Export: Buchhaltung (Journal CSV, Excel), Kontenplan, vollständiger Datenexport (ZIP, 24 Stunden)."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from aiohttp import web

from ...core import export as core_export
from ...core import patients as core_patients
from ...core.audit import audit
from ...core.keys import DB, MODULES, SETTINGS
from ...core.modules import ModuleContext, module_enabled
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.security import verify_password
from ...core.tenant import current_practice_id
from ...core.timeutil import today
from ...core.web import redirect_with_flash
from ..billing import api as billing_api
from . import bexio, journal


def _period(request: web.Request) -> tuple[date, date]:
    t = today()
    first_this = t.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    try:
        d0 = date.fromisoformat(request.query.get("von", ""))
        d1 = date.fromisoformat(request.query.get("bis", ""))
    except ValueError:
        d0, d1 = last_prev.replace(day=1), last_prev
    if d1 < d0:
        d0, d1 = d1, d0
    return d0, min(d1, d0 + timedelta(days=400))


def _cash_enabled(request: web.Request) -> bool:
    return module_enabled(request.app[DB], current_practice_id(), "cashdesk", request.app[MODULES])


def _firms_enabled(request: web.Request) -> bool:
    return module_enabled(request.app[DB], current_practice_id(), "firms", request.app[MODULES])


def _data(request: web.Request, d0: date, d1: date) -> dict:
    db = request.app[DB]
    sales, corrections = [], []
    if _cash_enabled(request):
        from ..cashdesk import api as cash_api

        sales = cash_api.accounting_sales(db, d0.isoformat(), d1.isoformat())
        corrections = cash_api.accounting_corrections(db, d0.isoformat(), d1.isoformat())
    firms = None
    if _firms_enabled(request):
        from ..firms import api as firms_api

        lo, hi = journal.bounds(d0, d1)
        firms = firms_api.accounting(db, lo, hi, d0.isoformat(), d1.isoformat())
    return journal.build(db, d0, d1, cash_sales=sales, firms=firms, cash_corrections=corrections)


def open_items(request: web.Request) -> list[dict]:
    """Offene Posten der Abrechnung und (falls eingeschaltet) der Firmenkunden."""
    db = request.app[DB]
    items = billing_api.open_items(db)
    if _firms_enabled(request):
        from ..firms import api as firms_api

        items = items + firms_api.open_items(db)
    return sorted(items, key=lambda x: (x["due_on"] or "", x["number"] or ""))


def presets(t: date) -> list[dict]:
    first_this = t.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    q = (t.month - 1) // 3
    q_start = date(t.year, q * 3 + 1, 1)
    pq_end = q_start - timedelta(days=1)
    pq_start = date(pq_end.year, ((pq_end.month - 1) // 3) * 3 + 1, 1)
    return [{"key": "prev_month", "von": last_prev.replace(day=1), "bis": last_prev},
            {"key": "this_month", "von": first_this, "bis": t},
            {"key": "prev_quarter", "von": pq_start, "bis": pq_end},
            {"key": "this_year", "von": date(t.year, 1, 1), "bis": t},
            {"key": "prev_year", "von": date(t.year - 1, 1, 1), "bis": date(t.year - 1, 12, 31)}]


async def page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    settings = request.app[SETTINGS]
    back = f"{prefix(request)}/einstellungen/export"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "accounts":
            journal.save_settings(db, {k: "".join(ch for ch in str(form.get(k) or "") if ch.isalnum() or ch in ".-")[:12] for k in journal.ACCOUNTS})
            raise redirect_with_flash(back, "common.saved")
        if str(do or "").startswith("bexio_"):
            await _bexio_post(request, form, back)
        raise redirect_with_flash(back, "exports.invalid")
    core_export.cleanup(settings)
    d0, d1 = _period(request)
    data = _data(request, d0, d1)
    bx = {"mode": bexio.mode_here(db, settings), "connected": bexio.connected(db, settings)}
    if bx["connected"]:
        bx.update(plan=bexio.plan(db, data["entries"]), problems=bexio.problems(db))
    missing = request.query.get("bexio_fehlt")
    bx["missing"] = [m for m in (missing or "").split(",") if m][:20] if missing is not None else None
    return await settings_page(request, "export", "exports/s_export.html", {
        "von": d0.isoformat(), "bis": d1.isoformat(), "data": data, "acc": journal.settings(db), "accounts": journal.ACCOUNTS,
        "acc_stored": journal.stored_settings(db),
        "presets": presets(today()), "files": core_export.list_exports(settings, current_practice_id()),
        "cash": _cash_enabled(request), "qs": f"von={d0.isoformat()}&bis={d1.isoformat()}", "bexio": bx})


# --------------------------------------------------------------------------- bexio
async def _bexio_post(request: web.Request, form, back: str) -> None:
    db = request.app[DB]
    settings = request.app[SETTINGS]
    qs = f"?von={str(form.get('von') or '')[:10]}&bis={str(form.get('bis') or '')[:10]}"
    here = back + qs + "#bexio"
    do = form.get("do")
    if do == "bexio_disconnect":
        bexio.disconnect(db)
        raise redirect_with_flash(back + "#bexio", "exports.bexio.disconnected")
    if do == "bexio_mark" and str(form.get("booking_id", "")).isdigit():
        bexio.mark(db, int(form["booking_id"]), str(form.get("status") or ""))
        raise redirect_with_flash(here, "common.saved")
    if not bexio.connected(db, settings):
        raise redirect_with_flash(here, "exports.bexio.not_connected")
    try:
        d0 = date.fromisoformat(str(form.get("von") or ""))
        d1 = date.fromisoformat(str(form.get("bis") or ""))
    except ValueError:
        d0, d1 = _period(request)
    data = _data(request, min(d0, d1), max(d0, d1))
    try:
        if do == "bexio_check":
            needed = {n for e in data["entries"] for n in (e.debit, e.credit)} | set(journal.settings(db).values())
            res = await bexio.check(db, settings, needed)
            raise redirect_with_flash(back + qs + "&bexio_fehlt=" + ",".join(res["missing"]) + "#bexio",
                                      "exports.bexio.check_missing" if res["missing"] else "exports.bexio.check_ok")
        if do == "bexio_send":
            res = await bexio.send(db, settings, data["entries"])
            key = "exports.bexio.sent_ok" if not (res["failed"] or res["unknown"]) else "exports.bexio.sent_some"
            if not (res["sent"] or res["failed"] or res["unknown"]):
                key = "exports.bexio.nothing"
            raise redirect_with_flash(here, key)
    except bexio.BexioError:
        raise redirect_with_flash(here, "exports.bexio.error")
    except bexio.Unclear:
        raise redirect_with_flash(here, "exports.bexio.unclear")
    raise redirect_with_flash(here, "exports.invalid")


async def bexio_connect(request: web.Request) -> web.StreamResponse:
    """Weiter zur Anmeldung bei bexio (OAuth). Ein Link statt Formular: die Weiterleitung führt aus der Praxis-Seite heraus."""
    settings = request.app[SETTINGS]
    if bexio.mode_here(request.app[DB], settings) != "oauth":
        raise redirect_with_flash(f"{prefix(request)}/einstellungen/export#bexio", "exports.bexio.not_configured")
    raise web.HTTPFound(bexio.start_oauth(request.app[DB], settings))


async def bexio_return(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/export#bexio"
    if bexio.mode_here(db, request.app[SETTINGS]) != "oauth" or not bexio.check_state(db, request.query.get("state")):
        raise redirect_with_flash(back, "exports.bexio.state_bad")
    if not request.query.get("code"):
        raise redirect_with_flash(back, "exports.bexio.denied")
    try:
        await bexio.finish_oauth(db, request.app[SETTINGS], request.query["code"][:2000])
    except bexio.BexioError:
        raise redirect_with_flash(back, "exports.bexio.error")
    raise redirect_with_flash(back, "exports.bexio.connected")


async def journal_csv(request: web.Request) -> web.StreamResponse:
    d0, d1 = _period(request)
    data = _data(request, d0, d1)
    audit(request.app[DB], "export", "accounting", None, None, {"format": "csv", "from": d0.isoformat(), "to": d1.isoformat()})
    return web.Response(body=journal.journal_csv(data), content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="journal-{d0.isoformat()}-{d1.isoformat()}.csv"'})


async def workbook(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    d0, d1 = _period(request)
    data = _data(request, d0, d1)
    closings = []
    if _cash_enabled(request):
        from ..cashdesk import api as cash_api

        closings = cash_api.closings_between(db, d0.isoformat(), d1.isoformat())
    body = await asyncio.to_thread(journal.workbook, data, open_items(request), closings)
    audit(db, "export", "accounting", None, None, {"format": "xlsx", "from": d0.isoformat(), "to": d1.isoformat()})
    return web.Response(body=body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="buchhaltung-{d0.isoformat()}-{d1.isoformat()}.xlsx"'})


async def full_export(request: web.Request) -> web.StreamResponse:
    """Vollständiger Export – nur mit erneuter Eingabe des Passworts."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/export"
    form = await request.post()
    user = db.one("SELECT password_hash FROM users WHERE id = ?", (request["user"]["id"],))
    if user is None or not verify_password(str(form.get("password") or ""), user["password_hash"]):
        audit(db, "export_denied", "practice", current_practice_id(), None, {"reason": "password"})
        raise redirect_with_flash(back, "exports.bad_password")
    path = await asyncio.to_thread(core_export.build, db, request.app[SETTINGS], current_practice_id())
    audit(db, "export_full", "practice", current_practice_id(), None, {"file": path.name, "size": path.stat().st_size})
    raise redirect_with_flash(back + "#voll", "exports.full_ready")


async def patient_export(request: web.Request) -> web.StreamResponse:
    """Datenauskunft für EINE Person (Art. 25 DSG): ZIP direkt zum Herunterladen, nichts bleibt auf dem Server.
    Patientennummer (oder eindeutiger Name) + Passwort der Leitung; im Protokoll vermerkt."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/export#auskunft"
    form = await request.post()
    user = db.one("SELECT password_hash FROM users WHERE id = ?", (request["user"]["id"],))
    if user is None or not verify_password(str(form.get("password") or ""), user["password_hash"]):
        audit(db, "export_denied", "patient", None, None, {"reason": "password"})
        raise redirect_with_flash(back, "exports.bad_password")
    q = str(form.get("patient") or "").strip()[:80]
    hits = []
    if q:
        exact = db.one("SELECT id FROM patients WHERE practice_id = ? AND upper(number) = upper(?)", (current_practice_id(), q))
        hits = [exact] if exact else list(core_patients.search(db, q, limit=3)) + list(core_patients.search(db, q, limit=3, archived=True))
    if len(hits) != 1:
        raise redirect_with_flash(back, "exports.auskunft_none" if not hits else "exports.auskunft_many")
    pid = hits[0]["id"]
    data, counts = await asyncio.to_thread(core_export.build_patient, db, request.app[SETTINGS], current_practice_id(), pid)
    number = db.value("SELECT number FROM patients WHERE id = ?", (pid,)) or str(pid)
    audit(db, "export_patient", "patient", pid, pid, {"tables": len(counts), "bytes": len(data)})
    name = f"datenauskunft-{number}-{today().isoformat()}.zip"
    return web.Response(body=data, headers={"Content-Disposition": f'attachment; filename="{name}"', "Content-Type": "application/zip",
                                            "Cache-Control": "no-store"})


async def download(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    path = core_export.file_for(request.app[SETTINGS], current_practice_id(), request.match_info["name"])
    if path is None:
        raise web.HTTPNotFound()
    audit(db, "download", "practice_export", current_practice_id(), None, {"file": path.name})
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                                           "Content-Type": "application/zip", "Cache-Control": "no-store"})


async def delete_file(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    path = core_export.file_for(request.app[SETTINGS], current_practice_id(), request.match_info["name"])
    if path is not None:
        path.unlink(missing_ok=True)
        audit(db, "delete", "practice_export", current_practice_id(), None, {"file": request.match_info["name"]})
    raise redirect_with_flash(f"{prefix(request)}/einstellungen/export#voll", "exports.deleted")


async def _cleanup_loop(app: web.Application) -> None:
    while True:
        try:
            core_export.cleanup(app[SETTINGS])
        except Exception:  # pragma: no cover
            pass
        await asyncio.sleep(3600)


CLEANUP = web.AppKey("nfizyo_exports_cleanup", asyncio.Task)


async def start_cleanup(app: web.Application) -> None:
    app[CLEANUP] = asyncio.create_task(_cleanup_loop(app))


async def stop_cleanup(app: web.Application) -> None:
    task = app.get(CLEANUP)
    if task:
        task.cancel()


def setup(ctx: ModuleContext) -> None:
    ctx.page("leitung", "/einstellungen/export", page, ("GET", "POST"))
    ctx.slot("leitung.settings", 63, lambda request: {"key": "export", "label_key": "exports.title", "path": "export"})
    ctx.page("leitung", "/einstellungen/export/bexio/verbinden", bexio_connect)
    ctx.page("leitung", "/einstellungen/export/bexio/zurueck", bexio_return)
    ctx.page("leitung", "/export/journal.csv", journal_csv)
    ctx.page("leitung", "/export/buchhaltung.xlsx", workbook)
    ctx.page("leitung", "/export/voll", full_export, ("POST",))
    ctx.page("leitung", "/export/auskunft", patient_export, ("POST",))
    ctx.page("leitung", "/export/datei/{name:n-fizyo-export-[0-9a-z-]+\\.zip}", download)
    ctx.page("leitung", "/export/datei/{name:n-fizyo-export-[0-9a-z-]+\\.zip}/loeschen", delete_file, ("POST",))
    ctx.on_startup(start_cleanup)
    ctx.on_cleanup(stop_cleanup)
