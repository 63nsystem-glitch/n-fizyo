"""MediData-Abfragen im Hintergrund – nach «Leistungsabrechnung Schnittstellendokumentation» V5.2.0 und «Best Practice Webservice».

Intervalle (Anhang BestPractice «Polling»): Teilnehmerverzeichnis alle 24 h; Status, Downloads, Benachrichtigungen alle 30 min.
«Die Intervalle sollen nicht unbedingt volle Stunden sein»: der erste Lauf beginnt mit einer Verzögerung je Box (aus der
Client-ID), danach gilt das Intervall ab dem letzten Lauf. Alle Aufrufe nacheinander (nie parallel), kurze Pause dazwischen;
bei 401/403/429/5xx oder ohne Verbindung endet die Runde sofort (kein Hämmern), nächster Versuch beim nächsten Intervall.
Protokoll (log) nur mit Anzahl und Fehlercode – nie Patientendaten, nie Zugangsdaten.

Der MediData-Zugang der Box gehört einer Praxis (NFIZYO_MEDIDATA_PRACTICE, sonst die einzige Praxis): alles wird dort gespeichert.

Abholen (3.9) ohne Verlust und ohne doppelten Import:
  1. Liste der pendenten Dokumente  2. Dokument holen  3. in EINER Transaktion speichern (medidata_downloads + Import in die
  bestehenden Antworten invoice_responses)  4. erst danach PUT …/status {"status": "CONFIRMED"}.
  Absturz zwischen 3 und 4: Das Dokument erscheint wieder in der Liste → schon gespeichert → nur noch bestätigen.
  Absturz vor 3: nichts gespeichert, nicht bestätigt → wird beim nächsten Mal wieder geholt.
  Nicht unterstützte Dokumente werden gespeichert (Leitung sieht sie) und ebenfalls bestätigt, wie die Dokumentation verlangt.
Benachrichtigungen (3.10): erst alle Seiten speichern, dann jede bestätigen (PUT {"notificationFetched": true}).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web

from ...core.db import Database, utcnow
from ...core.keys import DB, SETTINGS
from ...core.tenant import current_practice_id, practice_scope
from . import followup, medidata_cert, response
from .medidata import MdError, MediDataClient, accepts

log = logging.getLogger(__name__)

KINDS = ("participants", "notifications", "status", "downloads")      # Reihenfolge einer Runde
INTERVALS = {"participants": 24 * 3600, "status": 30 * 60, "downloads": 30 * 60, "notifications": 30 * 60}
LOOP_SECONDS = 300            # alle 5 min prüfen, was fällig ist
MANUAL_GAP = {"participants": 3600, "status": 120, "downloads": 120, "notifications": 120}   # «Jetzt abrufen» frühestens
STATUS_DAYS = 90              # Status nur für Sendungen der letzten 90 Tage nachführen
PAUSE = 0.2                   # s zwischen zwei Aufrufen
STOP_ROUND = (401, 403, 429)  # bei diesen Antworten die ganze Runde beenden


def owner_practice_id(db: Database, settings) -> int | None:
    """Praxis des MediData-Zugangs: NFIZYO_MEDIDATA_PRACTICE, sonst die einzige Praxis der Box."""
    if settings.medidata_practice:
        return db.value("SELECT id FROM practices WHERE slug = ?", (settings.medidata_practice,))
    ids = [r["id"] for r in db.query("SELECT id FROM practices ORDER BY id LIMIT 2")]
    return ids[0] if len(ids) == 1 else None


def active_here(db: Database, settings) -> bool:
    """MediData für die aktuelle Praxis nutzbar (eingerichtet und Zugang gehört dieser Praxis)."""
    return MediDataClient(settings).configured and owner_practice_id(db, settings) == current_practice_id()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def state(db: Database) -> dict:
    rows = db.query("SELECT * FROM medidata_sync WHERE practice_id = ?", (current_practice_id(),))
    return {r["kind"]: dict(r) for r in rows}


def _mark(db: Database, kind: str, *, ok: bool, error: str | None = None, count: int | None = None) -> None:
    now = utcnow()
    db.execute("INSERT INTO medidata_sync (practice_id, kind, last_run_at, last_ok_at, last_error, count) VALUES (?,?,?,?,?,?)"
               " ON CONFLICT (practice_id, kind) DO UPDATE SET last_run_at = excluded.last_run_at,"
               " last_ok_at = COALESCE(excluded.last_ok_at, medidata_sync.last_ok_at), last_error = excluded.last_error,"
               " count = COALESCE(excluded.count, medidata_sync.count)",
               (current_practice_id(), kind, now, now if ok else None, None if ok else (error or "")[:300], count))


def due(db: Database, now: datetime | None = None, *, manual: bool = False) -> list[str]:
    now = now or _now()
    st = state(db)
    out = []
    for kind in KINDS:
        last = (st.get(kind) or {}).get("last_run_at")
        gap = MANUAL_GAP[kind] if manual else INTERVALS[kind]
        if not last or datetime.fromisoformat(last) + timedelta(seconds=gap) <= now:
            out.append(kind)
    return out


LOCK = web.AppKey("nfizyo_medidata_lock", asyncio.Lock)


async def run(app: web.Application, *, kinds: list[str] | None = None, manual: bool = False) -> dict:
    """Eine Runde: fällige (oder angegebene) Abfragen nacheinander. Gibt {kind: Anzahl | "Fehler"} zurück."""
    settings, db = app[SETTINGS], app[DB]
    md = MediDataClient(settings)
    if not md.configured:
        return {"skipped": "not_configured"}
    pid = owner_practice_id(db, settings)
    if pid is None:
        return {"skipped": "no_practice"}
    lock = app.get(LOCK) or asyncio.Lock()           # in setup() angelegt; ohne (Tests) eine eigene
    if lock.locked():
        return {"skipped": "busy"}
    result: dict = {}
    async with lock:
        with practice_scope(pid):
            todo = [k for k in KINDS if k in (kinds if kinds is not None else due(db, manual=manual))]
            if not todo:
                return {"skipped": "not_due"}
            try:
                ctx = md._ctx()
            except medidata_cert.CertError as exc:
                for kind in todo:
                    _mark(db, kind, ok=False, error=f"Client-Zertifikat: {exc}")
                return {"error": "certificate"}
            async with md.session() as session:
                for i, kind in enumerate(todo):
                    try:
                        n = await FUNCS[kind](md, session, ctx, db)
                    except MdError as exc:
                        _mark(db, kind, ok=False, error=str(exc))
                        log.warning("MediData %s: %s", kind, exc)                  # nur Code/Status, keine Inhalte
                        result[kind] = "error"
                        if exc.status is None or exc.status in STOP_ROUND or exc.status >= 500:
                            for rest in todo[i + 1:]:                              # Runde beenden; Rest erst im nächsten Intervall
                                _mark(db, rest, ok=False, error="übersprungen (vorheriger Aufruf fehlgeschlagen)")
                                result[rest] = "skipped"
                            break
                        continue
                    except Exception as exc:  # noqa: BLE001 - eigener Fehler (z. B. beim Speichern): nichts bestätigt
                        _mark(db, kind, ok=False, error=f"intern: {type(exc).__name__}")
                        log.exception("MediData %s: interner Fehler", kind)
                        result[kind] = "error"
                        continue
                    _mark(db, kind, ok=True, count=n)
                    result[kind] = n
                    log.info("MediData %s: %s", kind, n)
    return result


# --------------------------------------------------------------------------- Teilnehmerverzeichnis (3.6)
async def sync_participants(md: MediDataClient, session, ctx, db: Database) -> int:
    items = await md.participants(session, ctx=ctx)
    now, pid = utcnow(), current_practice_id()
    with db.tx():                                    # ganz oder gar nicht: altes Verzeichnis bleibt bei Fehlern
        db.execute("DELETE FROM medidata_participants WHERE practice_id = ?", (pid,))
        db.executemany(
            "INSERT INTO medidata_participants (practice_id, gln_participant, gln_receiver, name, street, zip, town, law_types, bag_number,"
            " tg_tp_change, additional_costs, max_receive, not_supported, tg_allowed, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(pid, p["gln_participant"], p["gln_receiver"], p["name"], p["street"], p["zip"], p["town"], json.dumps(p["law_types"]),
              p["bag_number"], int(p["tg_tp_change"]), int(p["additional_costs"]), p["max_receive"], json.dumps(p["not_supported"]),
              int(p["tg_allowed"]), now) for p in items])
    return len(items)


def participant(db: Database, gln: str | None):
    """Eintrag zu einer GLN (als Teilnehmer oder als Empfänger-GLN)."""
    if not gln:
        return None
    return db.one("SELECT * FROM medidata_participants WHERE practice_id = ? AND (gln_participant = ? OR gln_receiver = ?)"
                  " ORDER BY gln_participant = ? DESC LIMIT 1", (current_practice_id(), gln, gln, gln))


def directory_info(db: Database) -> dict:
    row = db.one("SELECT COUNT(*) AS n, MAX(fetched_at) AS at FROM medidata_participants WHERE practice_id = ?", (current_practice_id(),))
    return {"count": row["n"], "fetched_at": row["at"]}


def reach(db: Database, gln: str | None) -> dict | None:
    """Erreichbarkeit eines Versicherers laut Verzeichnis. None = Verzeichnis noch leer (nicht prüfbar)."""
    if not directory_info(db)["count"]:
        return None
    p = participant(db, gln)
    if p is None:
        return {"listed": False}
    ns = json.loads(p["not_supported"] or "[]")
    return {"listed": True, "receiver": p["gln_receiver"], "name": p["name"], "v50": accepts(ns, "5.0"), "v45": accepts(ns, "4.5"),
            "tg_allowed": bool(p["tg_allowed"]), "additional_costs": bool(p["additional_costs"]), "max_receive": p["max_receive"],
            "laws": json.loads(p["law_types"] or "[]")}


def recipient_problems(db: Database, gln: str | None, *, tiers: str, size: int) -> tuple[list[str], dict | None]:
    """Vor dem Versand: Gründe, warum MediData die Sendung ablehnen würde (Schlüssel billing.md.chk_*).
    ([], None) = Verzeichnis noch leer – nicht prüfbar (die Seite zeigt das als Warnung)."""
    r = reach(db, gln)
    if r is None:
        return [], None
    if not r["listed"]:
        return ["chk_not_listed"], r                 # «Ein Versand ist nur an aufgelistete Teilnehmer möglich» (3.6)
    out = []
    if not r["v50"]:
        out.append("chk_version")                    # notSupported: «Es dürfen keine Dokumente dieses Types … gesendet werden»
    if tiers == "garant" and not r["tg_allowed"]:
        out.append("chk_tg")                         # Tiers garant an eine Organisation nur mit tgAllowed (3.6.2, 3.7.2.2)
    if r["max_receive"] and size > r["max_receive"]:
        out.append("chk_size")                       # maxReceive
    return out, r


# --------------------------------------------------------------------------- Status (3.8)
async def sync_status(md: MediDataClient, session, ctx, db: Database) -> int:
    cutoff = _iso(_now() - timedelta(days=STATUS_DAYS))
    rows = db.query("SELECT * FROM invoice_transmissions WHERE practice_id = ? AND channel = 'medidata' AND reference IS NOT NULL"
                    " AND (md_status IS NULL OR md_status = 'PROCESSING') AND created_at >= ? ORDER BY id LIMIT 200",
                    (current_practice_id(), cutoff))
    n = 0
    for t in rows:
        st = await md.upload_status(session, t["reference"], ctx=ctx)
        await asyncio.sleep(PAUSE)
        if st is None:
            db.execute("UPDATE invoice_transmissions SET md_status = 'NOT_FOUND', md_checked_at = ? WHERE id = ? AND practice_id = ?",
                       (utcnow(), t["id"], current_practice_id()))
            continue
        detail = None
        if st["status"] != "DONE":                   # Teilauslieferungen: wer hat schon, wer nicht (3.8.4)
            full = await md.upload_status(session, t["reference"], ctx=ctx, detail=True)
            await asyncio.sleep(PAUSE)
            if full and isinstance(full.get("receivers"), list):
                detail = [{k: str(r.get(k) or "")[:120] for k in ("status", "receiverType", "receiverDisplayName", "fetchedDate")}
                          for r in full["receivers"] if isinstance(r, dict)]
        set_md_status(db, t, st["status"], st.get("errorReason"), st.get("modified"), detail)
        n += 1
    return n


def set_md_status(db: Database, t, status: str, reason, modified, detail) -> None:
    db.execute("UPDATE invoice_transmissions SET md_status = ?, md_error_reason = ?, md_modified = ?, md_detail = ?, md_checked_at = ?"
               " WHERE id = ? AND practice_id = ?",
               (status, (str(reason) if reason else None), (str(modified)[:40] if modified else None),
                json.dumps(detail, ensure_ascii=False) if detail else t["md_detail"], utcnow(), t["id"], current_practice_id()))
    if t["status"] == "unknown":                     # MediData kennt die Sendung → sie ist angekommen
        resolve_unknown(db, t["id"], "status")


def resolve_unknown(db: Database, tid: int, how: str) -> None:
    """Unklare Sendung automatisch klären: sie ist bei MediData angekommen (Status, Benachrichtigung, Duplikat, Antwort)."""
    db.execute("UPDATE invoice_transmissions SET status = 'sent', resolution = ?, checked_at = ?, sent_at = COALESCE(sent_at, ?)"
               " WHERE id = ? AND practice_id = ? AND status = 'unknown'", (how, utcnow(), utcnow(), tid, current_practice_id()))


# --------------------------------------------------------------------------- Benachrichtigungen (3.10)
def notifications_since(db: Database) -> str | None:
    """Parameter «from» (3.10.1): ab 2 Tage vor der letzten erfolgreichen Abfrage (Überlappung schadet nicht – gespeicherte
    werden erkannt). Erste Abfrage: ohne «from». Nie bestätigte holt die Bestätigungsrunde unten aus der eigenen Tabelle."""
    last = (state(db).get("notifications") or {}).get("last_ok_at")
    if not last:
        return None
    return (datetime.fromisoformat(last) - timedelta(days=2)).date().isoformat()


async def sync_notifications(md: MediDataClient, session, ctx, db: Database) -> int:
    items = await md.notifications(session, ctx=ctx, since=notifications_since(db))
    new = sum(1 for n in items if store_notification(db, n))
    pid = current_practice_id()
    for row in db.query("SELECT id, notification_id FROM medidata_notifications WHERE practice_id = ? AND confirmed_at IS NULL"
                        " ORDER BY id LIMIT 500", (pid,)):
        if await md.confirm_notification(session, row["notification_id"], ctx=ctx):
            db.execute("UPDATE medidata_notifications SET confirmed_at = ? WHERE id = ? AND practice_id = ?", (utcnow(), row["id"], pid))
        await asyncio.sleep(PAUSE)
    return new


def _map(v) -> str | None:
    return json.dumps({k: str(x)[:1024] for k, x in v.items() if isinstance(k, str)}, ensure_ascii=False) if isinstance(v, dict) else None


def store_notification(db: Database, n: dict) -> bool:
    pid = current_practice_id()
    nid = int(n["id"])
    if db.value("SELECT 1 FROM medidata_notifications WHERE practice_id = ? AND notification_id = ?", (pid, nid)):
        return False
    params = n.get("notificationParameters") if isinstance(n.get("notificationParameters"), dict) else {}
    tref = str(n.get("transmissionReference") or params.get("transmissionReference") or "")[:64] or None
    dref = str(n.get("documentReference") or params.get("documentReference") or "")[:100] or None
    cref = str(n.get("correlationReference") or params.get("correlationReference") or "")[:100] or None
    code = str(n.get("errorCode") or params.get("code") or "")[:120] or None
    t = None
    if tref:
        t = db.one("SELECT * FROM invoice_transmissions WHERE practice_id = ? AND reference = ?", (pid, tref))
    if t is None and dref:
        t = db.one("SELECT * FROM invoice_transmissions WHERE practice_id = ? AND document_reference = ?", (pid, dref))
    invoice_id = t["invoice_id"] if t else None
    if invoice_id is None and cref:
        invoice_id = db.value("SELECT id FROM invoices WHERE practice_id = ? AND number = ?", (pid, cref))
    severity = str(n.get("severity") or n.get("type") or "")[:20] or None
    with db.tx():
        db.execute(
            "INSERT INTO medidata_notifications (practice_id, notification_id, severity, subject, message, potential_reasons,"
            " possible_solutions, technical_information, template, mode, error_code, transmission_reference, document_reference,"
            " correlation_reference, created, invoice_id, transmission_id, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, nid, severity, _map(n.get("subject")), _map(n.get("message")), _map(n.get("potentialReasons")),
             _map(n.get("possibleSolutions")), str(n.get("technicalInformation") or "")[:1000] or None,
             str(n.get("template") or "")[:80] or None, str(n.get("mode") or "")[:20] or None, code, tref, dref, cref,
             str(n.get("created") or "")[:40] or None, invoice_id, t["id"] if t else None, utcnow()))
        if t is not None:
            if tref and not t["reference"]:
                db.execute("UPDATE invoice_transmissions SET reference = ? WHERE id = ? AND practice_id = ?", (tref, t["id"], pid))
            if severity == "ERROR":
                db.execute("UPDATE invoice_transmissions SET md_status = 'ERROR', md_error_reason = COALESCE(?, md_error_reason),"
                           " md_checked_at = ? WHERE id = ? AND practice_id = ?", (code, utcnow(), t["id"], pid))
            if t["status"] == "unknown" and tref:
                resolve_unknown(db, t["id"], "notification")
    return True


# --------------------------------------------------------------------------- Dokumente abholen (3.9)
async def sync_downloads(md: MediDataClient, session, ctx, db: Database, *, rounds: int = 5, page: int = 50) -> int:
    pid = current_practice_id()
    stored = 0
    for _ in range(rounds):
        items, pending = await md.downloads(session, ctx=ctx, limit=page)
        progress = 0
        for item in items:
            ref = str(item["transmissionReference"])
            row = db.one("SELECT id, state, confirmed_at FROM medidata_downloads WHERE practice_id = ? AND transmission_reference = ?",
                         (pid, ref))
            if row is None:
                try:
                    data = await md.download(session, ref, ctx=ctx)
                except MdError as exc:
                    if exc.status is None or exc.status in STOP_ROUND or exc.status >= 500:
                        raise
                    store_download_error(db, item, str(exc))            # z. B. zu gross: bleibt bei MediData, Leitung sieht es
                    continue
                await asyncio.sleep(PAUSE)
                if data is None:                                         # 404: nicht mehr vorhanden
                    continue
                store_download(db, item, data)                           # eine Transaktion: speichern + Import
                stored += 1
            elif row["state"] == "error":
                continue                                                 # nicht gespeichert → nicht bestätigen
            if await md.confirm_download(session, ref, ctx=ctx):         # erst NACH dem Speichern
                db.execute("UPDATE medidata_downloads SET confirmed_at = ? WHERE practice_id = ? AND transmission_reference = ?",
                           (utcnow(), pid, ref))
                progress += 1
            await asyncio.sleep(PAUSE)
        if not items or not pending or progress == 0:
            break
    return stored


def _meta(item: dict) -> tuple:
    def s(k, n=120):
        v = item.get(k)
        return str(v)[:n] if v not in (None, "") else None

    size = item.get("fileSize")
    return (s("transmissionReference", 64), s("documentReference", 100), s("correlationReference", 100), s("senderGln", 20),
            s("docType"), size if isinstance(size, int) and not isinstance(size, bool) else None, s("modus", 20), s("created", 40))


def store_download(db: Database, item: dict, data: bytes) -> int:
    """Dokument speichern und – wenn es eine Rechnungsantwort ist – in die Antworten übernehmen. Alles in einer Transaktion."""
    pid = current_practice_id()
    sha = hashlib.sha256(data).hexdigest()
    meta = _meta(item)
    with db.tx():
        state_, response_id, invoice_id, error, content = "stored", None, None, None, data
        try:
            resp = response.parse(data)
        except response.ResponseError as exc:
            error = f"resp.err_{exc}"                                    # z. B. kein generalInvoiceResponse → aufbewahren
        else:
            rid, _matched = followup.store(db, resp, data, f"medidata-{meta[0]}.xml", "medidata")
            if rid is None:                                              # gleiche Datei schon (z. B. von Hand) hochgeladen
                rid = db.value("SELECT id FROM invoice_responses WHERE practice_id = ? AND sha256 = ?", (pid, sha))
            state_, response_id, content = "imported", rid, None
            invoice_id = db.value("SELECT invoice_id FROM invoice_responses WHERE id = ? AND practice_id = ?", (rid, pid))
            _resolve_by_response(db, invoice_id, meta[2], resp)
        did = db.execute(
            "INSERT INTO medidata_downloads (practice_id, transmission_reference, document_reference, correlation_reference, sender_gln,"
            " doc_type, file_size, modus, created, state, response_id, invoice_id, error, content, sha256, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, *meta, state_, response_id, invoice_id, error, content, sha, utcnow()))
    return did


def store_download_error(db: Database, item: dict, error: str) -> None:
    db.execute("INSERT OR IGNORE INTO medidata_downloads (practice_id, transmission_reference, document_reference, correlation_reference,"
               " sender_gln, doc_type, file_size, modus, created, state, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,'error',?,?)",
               (current_practice_id(), *_meta(item), error[:300], utcnow()))


def _resolve_by_response(db: Database, invoice_id: int | None, correlation: str | None, resp) -> None:
    """Eine Antwort des Versicherers beweist: die (unklare) Sendung derselben Rechnung ist angekommen."""
    if not invoice_id:
        return
    modus = "production" if (resp.modus or "").lower() == "production" else "test"
    for t in db.query("SELECT id, correlation_reference FROM invoice_transmissions WHERE practice_id = ? AND invoice_id = ?"
                      " AND channel = 'medidata' AND status = 'unknown' AND modus = ?", (current_practice_id(), invoice_id, modus)):
        if not correlation or not t["correlation_reference"] or t["correlation_reference"] == correlation:
            resolve_unknown(db, t["id"], "response")


FUNCS = {"participants": sync_participants, "status": sync_status, "downloads": sync_downloads, "notifications": sync_notifications}


# --------------------------------------------------------------------------- Anzeige
def notifications(db: Database, *, invoice_id: int | None = None, only_open: bool = False, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM medidata_notifications WHERE practice_id = ?"
    params: list = [current_practice_id()]
    if invoice_id is not None:
        sql += " AND invoice_id = ?"
        params.append(invoice_id)
    if only_open:
        sql += " AND seen_at IS NULL"
    rows = db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
    out = []
    for r in rows:
        d = dict(r)
        for k in ("subject", "message", "potential_reasons", "possible_solutions"):
            try:
                d[k] = json.loads(r[k]) if r[k] else {}
            except json.JSONDecodeError:
                d[k] = {}
        out.append(d)
    return out


def unhandled_downloads(db: Database, limit: int = 50):
    return db.query("SELECT id, transmission_reference, sender_gln, doc_type, file_size, modus, created, state, error, fetched_at,"
                    " confirmed_at, seen_at FROM medidata_downloads WHERE practice_id = ? AND state != 'imported' ORDER BY id DESC LIMIT ?",
                    (current_practice_id(), limit))


def open_count(db: Database) -> int:
    pid = current_practice_id()
    n = db.value("SELECT COUNT(*) FROM medidata_notifications WHERE practice_id = ? AND seen_at IS NULL"
                 " AND severity IN ('ERROR', 'WARNING')", (pid,), 0)
    return n + db.value("SELECT COUNT(*) FROM medidata_downloads WHERE practice_id = ? AND state != 'imported' AND seen_at IS NULL",
                        (pid,), 0)


def mark_seen(db: Database, table: str, row_id: int, user_id: int | None) -> bool:
    if table not in ("medidata_notifications", "medidata_downloads"):
        return False
    return bool(db.query(f"UPDATE {table} SET seen_at = ?, seen_by = ? WHERE id = ? AND practice_id = ? AND seen_at IS NULL"
                         " RETURNING id", (utcnow(), user_id, row_id, current_practice_id())))


def latest_states(db: Database, invoice_ids: list[int]) -> dict[int, dict]:
    """Letzte MediData-Sendung je Rechnung (für die Liste in Abrechnung)."""
    if not invoice_ids:
        return {}
    marks = ",".join("?" * len(invoice_ids))
    rows = db.query(f"SELECT invoice_id, status, md_status FROM invoice_transmissions WHERE practice_id = ? AND channel = 'medidata'"
                    f" AND invoice_id IN ({marks}) ORDER BY id", (current_practice_id(), *invoice_ids))
    return {r["invoice_id"]: {"status": r["status"], "md_status": r["md_status"]} for r in rows}


# --------------------------------------------------------------------------- Hintergrund
def first_delay(settings) -> int:
    """Verschiebung je Box (60–360 s), damit nicht alle Boxen zur vollen Stunde abfragen."""
    h = int(hashlib.sha256((settings.medidata_client_id or "box").encode()).hexdigest(), 16)
    return 60 + h % 300


async def loop(app: web.Application) -> None:
    await asyncio.sleep(first_delay(app[SETTINGS]))
    while True:
        try:
            await run(app)
        except Exception:  # pragma: no cover - die Schleife darf nie sterben
            log.exception("MediData-Abfrage fehlgeschlagen")
        await asyncio.sleep(LOOP_SECONDS)


TASK = web.AppKey("nfizyo_medidata_loop", asyncio.Task)


async def start(app: web.Application) -> None:
    if MediDataClient(app[SETTINGS]).configured:
        app[TASK] = asyncio.create_task(loop(app))


async def stop(app: web.Application) -> None:
    task = app.get(TASK)
    if task:
        task.cancel()
