"""Meldung an die NSystem-Übersicht (alle 5 Minuten, nur ausgehend) und Annahme signierter Befehle.

Ablauf pro Runde:
  1) Ergebnisse und Ereignisse des Runner-Dienstes einsammeln (runner/results/, runner/events/) → box_commands, box_events.
  2) Zustand sammeln (agent/status.py) + Ergebnisse noch nicht gemeldeter Befehle + Protokoll (nur Art/Zeit) + neue
     Support-Anfragen (vorher auf Patientendaten geprüft) → signiert senden.
  3) Antwort: neue Befehle → prüfen (Signatur, Box, Ablauf, nicht doppelt, erlaubte Aktion) → in runner/spool/ ablegen;
     Wartungshinweise und Antworten auf Support-Anfragen → Signatur von NSystem prüfen (Ed25519) → übernehmen.
Ohne NFIZYO_CENTRAL_URL / _BOX_ID / _BOX_KEY ist die Anbindung aus.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ..core import nsystem
from ..core.db import utcnow
from ..core.keys import DB, SETTINGS
from ..core.box import configured, get_state, set_state  # noqa: F401
from . import actions, signing, status

log = logging.getLogger(__name__)
INTERVAL = 300


def _dirs(settings) -> tuple[Path, Path]:
    base = status.runner_dir(settings)
    spool, results = base / "spool", base / "results"
    for d in (base, spool, results):
        d.mkdir(parents=True, exist_ok=True)
    return spool, results


def collect_results(app) -> int:
    """Ergebnisse des Runners übernehmen."""
    db = app[DB]
    _, results = _dirs(app[SETTINGS])
    n = 0
    for f in sorted(results.glob("*.json")):
        try:
            res = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cid = str(res.get("id") or "")
        if db.value("SELECT 1 FROM box_commands WHERE id = ?", (cid,)):
            st = res.get("status") if res.get("status") in ("running", "done", "failed", "rejected") else "failed"
            db.execute("UPDATE box_commands SET status = ?, result = ?, reason = COALESCE(?, reason), finished_at = ?, reported = 0"
                       " WHERE id = ?", (st, str(res.get("summary") or "")[:2000], res.get("reason"),
                                        None if st == "running" else utcnow(), cid))
            n += 1
        if res.get("status") != "running":
            f.unlink(missing_ok=True)
    return n


def accept_commands(app, commands: list[dict]) -> list[str]:
    """Befehle aus der Antwort prüfen und für den Runner ablegen. Gibt die angenommenen IDs zurück."""
    s = app[SETTINGS]
    db = app[DB]
    spool, _ = _dirs(s)
    accepted = []
    for cmd in commands[:20]:
        if not isinstance(cmd, dict):
            continue
        cid = str(cmd.get("id") or "")[:64]
        if not cid or db.value("SELECT 1 FROM box_commands WHERE id = ?", (cid,)):
            continue  # schon gesehen (kein zweites Mal)
        reason = signing.verify_command(s.central_command_pubkey, cmd, box_id=s.central_box_id)
        args = cmd.get("args") if isinstance(cmd.get("args"), dict) else {}
        if reason is None and not actions.valid_args(str(cmd.get("action")), args):
            reason = "not_allowed"
        db.execute("INSERT INTO box_commands (id, action, args, issued_by, issued_at, received_at, status, reason, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?)",
                   (cid, str(cmd.get("action"))[:40], json.dumps(args)[:2000], str(cmd.get("issued_by") or "?")[:120],
                    str(cmd.get("issued_at") or ""), utcnow(), "rejected" if reason else "queued", reason, utcnow() if reason else None))
        if reason:
            log.warning("NSystem-Befehl %s abgelehnt: %s", cid, reason)
            continue
        tmp = spool / f".{cid}.tmp"
        tmp.write_text(json.dumps(cmd))
        tmp.rename(spool / f"{cid}.json")
        accepted.append(cid)
    return accepted


def build_report(app) -> dict:
    db = app[DB]
    report = status.collect(app)
    report["commands"] = [dict(r) for r in db.query(
        "SELECT id, status, reason, result, finished_at FROM box_commands WHERE reported = 0 ORDER BY received_at LIMIT 50")]
    report["events"] = nsystem.events_for_report(db)
    report["support_requests"] = nsystem.outgoing_requests(db)
    report["support_known"] = nsystem.known_revs(db)
    return report


def accept_notices(app, notices) -> int:
    """Wartungshinweise: nur mit gültiger Signatur von NSystem für genau diese Box. Fehlt die Liste, bleibt alles wie es ist."""
    s = app[SETTINGS]
    good = []
    for n in notices[:50] if isinstance(notices, list) else []:
        if not isinstance(n, dict):
            continue
        data = {k: n.get(k) for k in ("id", "text_de", "text_fr", "starts_at", "ends_at")}
        if signing.verify_payload(s.central_command_pubkey, "notice", dict(data, box_id=s.central_box_id), str(n.get("signature") or "")):
            good.append(data)
        else:
            log.warning("Wartungshinweis mit ungültiger Signatur verworfen")
    return nsystem.sync_notices(app[DB], good)


def accept_replies(app, replies) -> int:
    """Antworten auf Support-Anfragen: nur mit gültiger Signatur von NSystem für genau diese Box."""
    s = app[SETTINGS]
    n = 0
    for r in replies[:50] if isinstance(replies, list) else []:
        if not isinstance(r, dict):
            continue
        data = {k: r.get(k) for k in ("uid", "rev", "status", "reply", "reply_by")}
        if signing.verify_payload(s.central_command_pubkey, "support_reply", dict(data, box_id=s.central_box_id), str(r.get("signature") or "")):
            n += nsystem.apply_reply(app[DB], data)
    return n


async def send_once(app) -> bool:
    """Eine Meldung senden. True = NSystem hat geantwortet."""
    import aiohttp

    s = app[SETTINGS]
    db = app[DB]
    await asyncio.to_thread(collect_results, app)
    await asyncio.to_thread(nsystem.ingest_runner_events, db, s)
    report = await asyncio.to_thread(build_report, app)
    body = json.dumps(report, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Box-Id": s.central_box_id, "X-Timestamp": ts,
               "X-Signature": signing.report_signature(s.central_box_key, ts, body)}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(f"{s.central_url}/api/v1/report", data=body, headers=headers) as resp:
                if resp.status != 200:
                    err = f"HTTP {resp.status}"
                    if resp.status == 403:
                        info = await resp.json(content_type=None) if resp.content_type == "application/json" else {}
                        if isinstance(info, dict) and info.get("error") == "clock":
                            err = f"Uhr der Box weicht ab ({int(time.time()) - int(info.get('server_time') or 0):+d} s)"
                            log.error("NSystem lehnt die Meldung ab: %s – Zeitabgleich (timedatectl) prüfen", err)
                    set_state(db, "last_error", err)
                    return False
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - Netz weg → nächste Runde
        set_state(db, "last_error", type(exc).__name__)
        return False
    ids = [c["id"] for c in report["commands"]]
    if ids:
        db.execute(f"UPDATE box_commands SET reported = 1 WHERE id IN ({','.join('?' for _ in ids)}) AND status IN ('done','failed','rejected')", ids)
    nsystem.mark_events_reported(db, [e["uid"] for e in report["events"]])
    nsystem.mark_requests_sent(db, [r["uid"] for r in report["support_requests"]])
    set_state(db, "last_report", utcnow())
    set_state(db, "last_error", None)
    data = data if isinstance(data, dict) else {}
    accept_commands(app, data.get("commands") or [])
    if "notices" in data:
        accept_notices(app, data["notices"])
    if "support_replies" in data:
        accept_replies(app, data["support_replies"])
    return True


async def loop(app) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await send_once(app)
        except Exception:  # pragma: no cover - darf die App nie stören
            log.exception("NSystem-Meldung")
        await asyncio.sleep(INTERVAL)


async def start(app) -> None:
    if configured(app[SETTINGS]):
        app[TASK] = asyncio.create_task(loop(app))


async def stop(app) -> None:
    if TASK in app:
        app[TASK].cancel()


def _task_key():
    from aiohttp import web

    return web.AppKey("nfizyo_agent_task", asyncio.Task)


TASK = _task_key()
