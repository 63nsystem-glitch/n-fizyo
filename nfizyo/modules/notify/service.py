"""Benachrichtigungen planen, bündeln und versenden.

Regeln (Entscheid der Praxisinhaberin / des Praxisinhabers, Okt. 2026):
- Die Praxis wählt: aus | E-Mail (nur mit Einverständnis, patients.email_ok_at) | SMS | E-Mail mit Einverständnis, sonst SMS (Standard).
- Inhalt allgemein, ohne Gesundheitsdaten, in der Sprache der App der Patientin / des Patienten, mit Link in die App.
- Nur an Personen mit verbundener App (sonst führt der Link ins Leere).
- Höchstens eine Benachrichtigung pro Person in 15 Minuten: alles, was in dieser Zeit dazukommt, geht gebündelt mit.
- Kurz warten (2 Minuten): Wer gerade in der App liest, bekommt keine SMS; mehrere Antworten hintereinander = eine Benachrichtigung.
- Keine SMS zwischen 20:00 und 08:00 (Zeit Zürich) – die SMS wartet bis 08:00. E-Mails gehen jederzeit.
- Schon in der App gelesen (oder Termin abgesagt)? Dann keine Benachrichtigung.
- Neue Termine nur, wenn die Praxis das einschaltet – und nicht, wenn die Termin-Erinnerung der Agenda (24 Stunden vorher)
  ohnehin bald kommt (sonst zwei SMS für denselben Termin).
Versand über den Postausgang des Kerns (core/sms.py, core/mail.py). Der App-Link enthält kein Token.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

from aiohttp import web

from ...core import patients as core_patients
from ...core.audit import audit
from ...core.db import Database
from ...core.i18n import PATIENT_LANGS
from ...core.keys import DB, I18N, MAIL, MODULES, SMS
from ...core.mail import valid_email
from ...core.modules import module_enabled
from ...core.sms import normalize_phone
from ...core.tenant import practice_scope
from ...core.timeutil import TZ
from ...core.web import background_public_url
from ..patientapp import api as patientapp_api

log = logging.getLogger(__name__)

MODES = ("email_or_sms", "email", "sms", "off")
DEFAULT_MODE = "email_or_sms"
KINDS = ("message", "info", "appointment")
BUNDLE = timedelta(minutes=15)
DELAY = timedelta(minutes=2)
QUIET_START, QUIET_END = time(20, 0), time(8, 0)
REMINDER_WINDOW = timedelta(hours=26)   # Agenda erinnert 24 Stunden vorher (+ Reserve, die Erinnerung läuft alle 10 Minuten)
PATHS = {"message": "/p/nachrichten", "info": "/p/info", "appointment": "/p/termine"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- Einstellungen der Praxis
def settings(db: Database, practice_id: int) -> dict:
    row = db.one("SELECT mode, appointments, updated_at FROM notify_settings WHERE practice_id = ?", (practice_id,))
    if row is None:
        return {"mode": DEFAULT_MODE, "appointments": False, "updated_at": None}
    return {"mode": row["mode"], "appointments": bool(row["appointments"]), "updated_at": row["updated_at"]}


def save_settings(db: Database, practice_id: int, mode: str, appointments: bool, user_id: int | None) -> None:
    if mode not in MODES:
        raise ValueError(mode)
    db.execute("INSERT INTO notify_settings (practice_id, mode, appointments, updated_by, updated_at) VALUES (?,?,?,?,?)"
               " ON CONFLICT(practice_id) DO UPDATE SET mode = excluded.mode, appointments = excluded.appointments,"
               " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
               (practice_id, mode, 1 if appointments else 0, user_id, _iso(now_utc())))
    audit(db, "update", "notify_settings", practice_id, None, {"mode": mode, "appointments": bool(appointments)})


# --------------------------------------------------------------------------- Weg und Zeitpunkt
def channel_for(mode: str, patient) -> tuple[str | None, str | None]:
    """(Kanal, Grund ohne Kanal). E-Mail nur mit Einverständnis und gültiger Adresse."""
    email_ok = core_patients.email_allowed(patient) and valid_email(patient["email"])
    phone_ok = normalize_phone(patient["phone"]) is not None
    if mode == "off":
        return None, "off"
    if mode == "email":
        return ("email", None) if email_ok else (None, "no_consent")
    if mode == "sms":
        return ("sms", None) if phone_ok else (None, "no_phone")
    if email_ok:
        return "email", None
    return ("sms", None) if phone_ok else (None, "no_contact")


def quiet(dt: datetime) -> bool:
    """SMS-Ruhezeit (20:00–08:00 Zürich)?"""
    local = dt.astimezone(TZ).time()
    return local >= QUIET_START or local < QUIET_END


def next_morning(dt: datetime) -> datetime:
    """Nächster Zeitpunkt 08:00 (Zürich) ab dt, falls dt in der Ruhezeit liegt; sonst dt."""
    if not quiet(dt):
        return dt
    local = dt.astimezone(TZ)
    day = local.date() + (timedelta(days=1) if local.time() >= QUIET_START else timedelta(0))
    return datetime.combine(day, QUIET_END, tzinfo=TZ).astimezone(timezone.utc)


def _last_sent(db: Database, practice_id: int, patient_id: int) -> datetime | None:
    v = db.value("SELECT MAX(sent_at) FROM notify_queue WHERE practice_id = ? AND patient_id = ? AND status = 'sent'",
                 (practice_id, patient_id))
    return _dt(v) if v else None


def _plan(db: Database, practice_id: int, patient_id: int, channel: str, now: datetime) -> datetime:
    pending = db.value("SELECT MIN(due_at) FROM notify_queue WHERE practice_id = ? AND patient_id = ? AND status = 'pending'",
                       (practice_id, patient_id))
    if pending:
        return _dt(pending)                      # kommt in die schon geplante Benachrichtigung
    due = now + DELAY
    last = _last_sent(db, practice_id, patient_id)
    if last and last + BUNDLE > due:
        due = last + BUNDLE
    return next_morning(due) if channel == "sms" else due


# --------------------------------------------------------------------------- Anlass eintragen
def enqueue(app: web.Application, practice_id: int, patient_id: int, kind: str, ref_id: int | None, *,
            now: datetime | None = None) -> int | None:
    """Anlass für eine Benachrichtigung vormerken. Ohne Versand (oder mit Grund «nicht benachrichtigt») – sichtbar in der Praxis."""
    db = app[DB]
    if kind not in KINDS or not module_enabled(db, practice_id, "notify", app[MODULES]):
        return None
    st = settings(db, practice_id)
    if kind == "appointment" and (not st["appointments"] or st["mode"] == "off"):
        return None
    now = now or now_utc()
    with practice_scope(practice_id):
        patient = core_patients.get(db, patient_id)
        if patient is None:
            return None
        status, reason, channel = "pending", None, None
        if not patientapp_api.has_app(db, patient_id):
            status, reason = "skipped", "no_app"
        else:
            channel, reason = channel_for(st["mode"], patient)
            if channel is None:
                status = "skipped"
        due = _plan(db, practice_id, patient_id, channel, now) if status == "pending" else now
        return db.execute(
            "INSERT INTO notify_queue (practice_id, patient_id, kind, ref_id, status, reason, channel, due_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (practice_id, patient_id, kind, ref_id, status, reason, channel, _iso(due), _iso(now)))


# --------------------------------------------------------------------------- Ereignisse der anderen Module
def on_message_sent(app: web.Application, *, message_id: int, patient_id: int, practice_id: int | None = None, **_) -> None:
    from ...core.tenant import current_practice_id

    enqueue(app, practice_id or current_practice_id(), patient_id, "message", message_id)


def on_info_released(app: web.Application, *, info_id: int, patient_id: int, practice_id: int | None = None, **_) -> None:
    from ...core.tenant import current_practice_id

    enqueue(app, practice_id or current_practice_id(), patient_id, "info", info_id)


def on_appointment_booked(app: web.Application, *, appointment_id: int, patient_id: int | None = None, **_) -> None:
    """Neuer Termin – nur wenn eingeschaltet und nicht schon die Erinnerung (24 h vorher) kommt."""
    from ...core.tenant import current_practice_id

    if not patient_id:
        return
    practice_id = current_practice_id()
    db = app[DB]
    if not settings(db, practice_id)["appointments"]:
        return
    a = _appointment(app, practice_id, appointment_id)
    if a is None or a["status"] != "booked":
        return
    start = _dt(a["starts_at"])
    if start - now_utc() <= REMINDER_WINDOW:
        return                                        # die Erinnerung der Agenda kommt ohnehin – nicht zweimal
    with practice_scope(practice_id):
        p = core_patients.get(db, patient_id)
        if p is None or p["reminder_channel"] == "none":
            return                                    # möchte keine Termin-Nachrichten
    enqueue(app, practice_id, patient_id, "appointment", appointment_id)


def _appointment(app: web.Application, practice_id: int, appointment_id: int):
    if not module_enabled(app[DB], practice_id, "agenda", app[MODULES]):
        return None
    from ..agenda import api as agenda_api

    with practice_scope(practice_id):
        return agenda_api.get(app[DB], appointment_id)


# --------------------------------------------------------------------------- Versand
def _still_relevant(app: web.Application, practice_id: int, items: list) -> tuple[list, list]:
    """(relevant, erledigt): Was die Person in der App schon gelesen hat (oder abgesagte Termine), braucht keine Benachrichtigung."""
    db = app[DB]
    mods = app[MODULES]
    by_kind: dict[str, list] = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)
    unread: set[tuple[str, int]] = set()
    if by_kind.get("message"):
        if module_enabled(db, practice_id, "messages", mods):
            from ..messages import api as messages_api

            unread |= {("message", i) for i in messages_api.unread_out(db, [it["ref_id"] for it in by_kind["message"]])}
    if by_kind.get("info"):
        if module_enabled(db, practice_id, "patientinfo", mods):
            from ..patientinfo import api as info_api

            unread |= {("info", i) for i in info_api.unread(db, [it["ref_id"] for it in by_kind["info"]])}
    for it in by_kind.get("appointment", []):
        a = _appointment(app, practice_id, it["ref_id"])
        if a is not None and a["status"] == "booked" and _dt(a["starts_at"]) > now_utc():
            unread.add(("appointment", it["ref_id"]))
    keep = [it for it in items if (it["kind"], it["ref_id"]) in unread]
    return keep, [it for it in items if (it["kind"], it["ref_id"]) not in unread]


def _skip(db: Database, items: list, reason: str) -> None:
    for it in items:
        db.execute("UPDATE notify_queue SET status = 'skipped', reason = ? WHERE id = ? AND practice_id = ? AND status = 'pending'",
                   (reason, it["id"], it["practice_id"]))


def _postpone(db: Database, items: list, due: datetime, channel: str | None) -> None:
    for it in items:
        db.execute("UPDATE notify_queue SET due_at = ?, channel = ? WHERE id = ? AND practice_id = ? AND status = 'pending'",
                   (_iso(due), channel, it["id"], it["practice_id"]))


def compose(app: web.Application, lang: str, practice_name: str, kinds: set[str], base: str | None = None,
            practice_id: int | None = None) -> dict:
    """Text der Benachrichtigung (ohne Gesundheitsdaten): {"text", "subject", "body", "url"}. base: öffentliche Adresse
    (die Vorschau in den Einstellungen gibt core.web.public_url mit). Ohne base gilt dieselbe Regel im Hintergrund
    (core.web.background_public_url): eingestellte Adresse, sonst die Adresse, unter der die Praxis den Server benutzt."""
    t = app[I18N].t
    lang = lang if lang in PATIENT_LANGS else "de"
    kind = next(iter(kinds)) if len(kinds) == 1 else "several"
    url = (base or background_public_url(app, practice_id)) + PATHS.get(kind, "/p/heute")
    text = t(lang, f"p.notify.{kind}", practice=practice_name, url=url)
    body = "\n\n".join((t(lang, "p.notify.mail_hello"), text, t(lang, "p.notify.mail_privacy"),
                        t(lang, "p.notify.mail_bye", practice=practice_name)))
    return {"text": text, "subject": t(lang, "p.notify.mail_subject", practice=practice_name), "body": body, "url": url}


def _process_patient(app: web.Application, practice_id: int, patient_id: int, now: datetime) -> int:
    db = app[DB]
    items = db.query("SELECT * FROM notify_queue WHERE practice_id = ? AND patient_id = ? AND status = 'pending' ORDER BY id",
                     (practice_id, patient_id))
    if not items:
        return 0
    st = settings(db, practice_id)
    if not module_enabled(db, practice_id, "notify", app[MODULES]) or st["mode"] == "off":
        _skip(db, items, "off")
        return 0
    patient = core_patients.get(db, patient_id)
    if patient is None or patient["archived"] or not patientapp_api.has_app(db, patient_id):
        _skip(db, items, "no_app")
        return 0
    items, done = _still_relevant(app, practice_id, items)
    _skip(db, done, "read")
    if not items:
        return 0
    channel, reason = channel_for(st["mode"], patient)
    if channel is None:
        _skip(db, items, reason or "no_contact")
        return 0
    last = _last_sent(db, practice_id, patient_id)
    if last and now < last + BUNDLE:
        _postpone(db, items, next_morning(last + BUNDLE) if channel == "sms" else last + BUNDLE, channel)
        return 0
    if channel == "sms" and quiet(now):
        _postpone(db, items, next_morning(now), channel)
        return 0
    practice = db.one("SELECT name FROM practices WHERE id = ?", (practice_id,))
    msg = compose(app, patient["lang"], practice["name"], {it["kind"] for it in items}, practice_id=practice_id)
    if channel == "sms":
        outbox_id = app[SMS].send(practice_id, "sms", patient["phone"], msg["text"])
    else:
        outbox_id = app[MAIL].send(practice_id, patient["email"], msg["subject"], msg["body"])
    sent = _iso(now)
    for it in items:
        db.execute("UPDATE notify_queue SET status = 'sent', channel = ?, sent_at = ?, outbox_id = ?, reason = NULL"
                   " WHERE id = ? AND practice_id = ? AND status = 'pending'", (channel, sent, outbox_id, it["id"], practice_id))
    audit(db, "notify", "patient", patient_id, patient_id, {"channel": channel, "kinds": sorted({it["kind"] for it in items}),
                                                            "n": len(items)})
    return 1


def process_due(app: web.Application, now: datetime | None = None) -> int:
    """Fällige Benachrichtigungen senden (eine pro Person, gebündelt). Gibt die Zahl der versendeten Benachrichtigungen zurück."""
    db = app[DB]
    now = now or now_utc()
    sent = 0
    for row in db.query("SELECT DISTINCT practice_id, patient_id FROM notify_queue WHERE status = 'pending' AND due_at <= ?",
                        (_iso(now),)):
        try:
            with practice_scope(row["practice_id"]):
                sent += _process_patient(app, row["practice_id"], row["patient_id"], now)
        except Exception:  # noqa: BLE001 - eine Person darf die anderen nicht blockieren
            log.exception("Benachrichtigung für Patient/in %s fehlgeschlagen", row["patient_id"])
    return sent
