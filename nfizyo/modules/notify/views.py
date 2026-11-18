"""Leitung › Einstellungen › Benachrichtigungen; Hintergrund-Versand; Ereignisse der anderen Module."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiohttp import web

from ...core.keys import DB, MAIL, SMS
from ...core.modules import ModuleContext
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.tenant import current_practice_id, current_user_id
from ...core.web import public_url, redirect_with_flash
from . import service

log = logging.getLogger(__name__)
LOOP_SECONDS = 60


async def settings_view(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    pid = current_practice_id()
    back = f"{prefix(request)}/einstellungen/benachrichtigungen"
    if request.method == "POST":
        form = await request.post()
        mode = str(form.get("mode") or "")
        if mode not in service.MODES:
            raise redirect_with_flash(back, "settings.invalid")
        service.save_settings(db, pid, mode, form.get("appointments") == "1", current_user_id())
        raise redirect_with_flash(back, "common.saved")
    since = service._iso(service.now_utc() - timedelta(days=30))
    sent = db.query("SELECT channel, COUNT(DISTINCT outbox_id) AS n FROM notify_queue WHERE practice_id = ? AND status = 'sent'"
                    " AND sent_at >= ? GROUP BY channel", (pid, since))
    skipped = db.query("SELECT reason, COUNT(*) AS n FROM notify_queue WHERE practice_id = ? AND status = 'skipped'"
                       " AND created_at >= ? GROUP BY reason ORDER BY n DESC", (pid, since))
    pending = db.value("SELECT COUNT(*) FROM notify_queue WHERE practice_id = ? AND status = 'pending'", (pid,), 0)
    example = service.compose(request.app, request["lang"], db.value("SELECT name FROM practices WHERE id = ?", (pid,), ""),
                              {"message"}, base=public_url(request))
    return await settings_page(request, "benachrichtigungen", "notify/s_notify.html", {
        "st": service.settings(db, pid), "modes": service.MODES, "sent": {r["channel"]: r["n"] for r in sent},
        "skipped": skipped, "pending": pending, "example": example,
        "sms_live": request.app[SMS].provider_of(pid) != "log", "mail_live": request.app[MAIL].live})


async def _loop(app: web.Application) -> None:
    await asyncio.sleep(20)  # nach dem Start zuerst die anderen Dienste
    while True:
        try:
            service.process_due(app)
        except Exception:  # pragma: no cover - der Versand darf nie sterben
            log.exception("Benachrichtigungen fehlgeschlagen")
        await asyncio.sleep(LOOP_SECONDS)


TASK = web.AppKey("nfizyo_notify_loop", asyncio.Task)


async def start(app: web.Application) -> None:
    app[TASK] = asyncio.create_task(_loop(app))


async def stop(app: web.Application) -> None:
    task = app.get(TASK)
    if task:
        task.cancel()


def setup(ctx: ModuleContext) -> None:
    ctx.page("leitung", "/einstellungen/benachrichtigungen", settings_view, ("GET", "POST"))
    ctx.slot("leitung.settings", 64, lambda request: {"key": "benachrichtigungen", "label_key": "notify.sec", "path": "benachrichtigungen"})
    ctx.on("message.sent", service.on_message_sent)
    ctx.on("patientinfo.released", service.on_info_released)
    ctx.on("appointment.booked", service.on_appointment_booked)
    ctx.on_startup(start)
    ctx.on_cleanup(stop)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('notify_queue',), (
        "UPDATE notify_queue SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
