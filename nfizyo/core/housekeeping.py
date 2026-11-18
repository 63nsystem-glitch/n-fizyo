"""Aufräumen im Betrieb: Tabellen, die sonst endlos wachsen (alle 6 Stunden, erster Lauf kurz nach dem Start).

Kern (eigene Tabellen):
  jobs      erledigte Hintergrundaufgaben nach 30 Tagen, fehlgeschlagene nach 90 Tagen
  outbox    versendete / protokollierte / endgültig fehlgeschlagene Nachrichten (SMS, E-Mail, HIN) nach 90 Tagen
  sessions  abgelaufene Anmeldungen (feste Höchstdauer) und Praxis-Anmeldungen, die länger als die Inaktivitätsgrenze
            (NFIZYO_STAFF_IDLE_HOURS) nicht benutzt wurden
Nie gelöscht: Offenes (jobs queued/running, outbox queued) – egal wie alt.
Module: jedes Modul räumt NUR eigene Tabellen auf (Bauregel) und meldet dafür eine Funktion an:
  ctx.housekeeping(fn)   fn(db, now: datetime) -> Anzahl gelöschter Zeilen   (z. B. notify › notify_queue)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from aiohttp import web

from .db import Database
from .keys import DB, SETTINGS

log = logging.getLogger(__name__)

JOBS_DONE_DAYS = 30
JOBS_FAILED_DAYS = 90
OUTBOX_DAYS = 90
INTERVAL = 6 * 3600
FIRST_RUN_AFTER = 120
CHUNK = 2000

Hook = Callable[[Database, datetime], int]
HOOKS = web.AppKey("nfizyo_housekeeping_hooks", list)
TASK = web.AppKey("nfizyo_housekeeping_task", asyncio.Task)


def iso(dt: datetime) -> str:
    """Gleiches Format wie db.utcnow() (UTC, Sekunden) – damit Vergleiche als Text stimmen."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def delete_chunked(db: Database, table: str, where: str, params: tuple) -> int:
    """DELETE in Portionen (die Datenbank bleibt dazwischen für die Praxis frei). Nur mit festen Tabellennamen aufrufen."""
    total = 0
    while True:
        with db.tx():
            db.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT {CHUNK})", params)  # noqa: S608
            n = db.value("SELECT changes()", (), 0)
        total += n
        if n < CHUNK:
            return total


def purge_core(db: Database, now: datetime, staff_idle_hours: float | None = None) -> dict[str, int]:
    out = {
        "jobs": delete_chunked(db, "jobs", "(status = 'done' AND COALESCE(finished_at, created_at) < ?)"
                               " OR (status = 'failed' AND COALESCE(finished_at, created_at) < ?)",
                               (iso(now - timedelta(days=JOBS_DONE_DAYS)), iso(now - timedelta(days=JOBS_FAILED_DAYS)))),
        "outbox": delete_chunked(db, "outbox", "status IN ('sent', 'logged', 'failed') AND created_at < ?",
                                 (iso(now - timedelta(days=OUTBOX_DAYS)),)),
        "sessions": delete_chunked(db, "sessions", "expires_at < ?", (iso(now),)),
    }
    if staff_idle_hours:
        out["sessions"] += delete_chunked(db, "sessions", "kind = 'staff' AND COALESCE(last_seen_at, created_at) < ?",
                                          (iso(now - timedelta(hours=staff_idle_hours)),))
    return out


def register(app: web.Application, owner: str, fn: Hook) -> None:
    app.setdefault(HOOKS, []).append((owner, fn))


def run_once(app: web.Application, now: datetime | None = None) -> dict[str, int]:
    """Ein Durchgang: Kern, dann jedes Modul. Ein Fehler in einem Modul hält die anderen nicht auf."""
    now = now or datetime.now(timezone.utc)
    db = app[DB]
    out = purge_core(db, now, getattr(app[SETTINGS], "staff_idle_hours", None))
    for owner, fn in app.get(HOOKS, []):
        try:
            out[owner] = out.get(owner, 0) + int(fn(db, now) or 0)
        except Exception:  # noqa: BLE001 - Aufräumen darf nie den Betrieb stören
            log.exception("Aufräumen im Modul %s fehlgeschlagen", owner)
    if any(out.values()):
        log.info("Aufgeräumt: %s", ", ".join(f"{k} {v}" for k, v in out.items() if v))
    return out


async def _loop(app: web.Application) -> None:
    await asyncio.sleep(FIRST_RUN_AFTER)
    while True:
        try:
            run_once(app)
        except Exception:  # noqa: BLE001
            log.exception("Aufräumen fehlgeschlagen")
        await asyncio.sleep(INTERVAL)


async def start(app: web.Application) -> None:
    app[TASK] = asyncio.create_task(_loop(app))


async def stop(app: web.Application) -> None:
    task = app.get(TASK)
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
