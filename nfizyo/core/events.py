"""Nachrichten zwischen Modulen («Haber sistemi»).

Ein Modul meldet, was passiert ist; andere Module reagieren, ohne dass sie sich gegenseitig kennen müssen.
Beispiel: Die Dokumentation meldet «note.approved» – die Agenda setzt daraufhin den Termin auf «erledigt».

Bekannte Ereignisse (Name → Daten):
  patient.created        patient_id
  appointment.booked     appointment_id, patient_id, therapist_id
  appointment.status     appointment_id, patient_id, status
  note.approved          note_id, patient_id, appointment_id, author_id
  note.discarding        note_id, patient_id               (Entwurf wird gleich gelöscht: Verweise lösen)
  dictation.transcribed  recording_id, patient_id, user_id, transcript, language
  dictation.approved     recording_id, note_id, patient_id, appointment_id, form (Daten der Prüfseite)
  document.assigned      document_id, patient_id
  message.sent           message_id, patient_id        (Antwort der Praxis an die Patientin / den Patienten)
  patientinfo.released   info_id, patient_id           (Information für die Patienten-App freigegeben)
  patient.linked         patient_id, found (dict)      (Leitung › Doppelte Einträge: jedes Modul zählt seine Einträge zur Person
                                                        in found[Tabelle]; nur lesen)
  patient.merge          drop_id, keep_id              (Doppel-Eintrag zusammenführen: jedes Modul überführt die eigenen Tabellen;
                                                        läuft in einer Transaktion – Fehler brechen alles ab)
  Beide werden mit publish_strict() gesendet und von ModuleContext.patient_tables() angemeldet.

Fehler in einem Empfänger werden protokolliert und brechen die Aktion des Senders nicht ab.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from aiohttp import web

log = logging.getLogger(__name__)

EVENTS = web.AppKey("nfizyo_events", dict)

KNOWN = {
    "patient.created", "appointment.booked", "appointment.status", "note.approved", "note.discarding",
    "dictation.transcribed", "dictation.approved", "document.assigned", "message.sent", "patientinfo.released",
    "patient.linked", "patient.merge",
}


def subscribe(app: web.Application, name: str, handler: Callable[..., Any]) -> None:
    if name not in KNOWN:
        raise ValueError(f"Unbekanntes Ereignis {name!r} – bitte in core/events.py eintragen.")
    app.setdefault(EVENTS, {}).setdefault(name, []).append(handler)


async def publish(app: web.Application, name: str, **data: Any) -> None:
    if name not in KNOWN:
        raise ValueError(f"Unbekanntes Ereignis {name!r}")
    for handler in app.get(EVENTS, {}).get(name, []):
        try:
            result = handler(app, **data)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - ein Empfänger darf den Absender nie blockieren
            log.exception("Ereignis %s: Empfänger %s fehlgeschlagen", name, getattr(handler, "__qualname__", handler))


async def publish_strict(app: web.Application, name: str, **data: Any) -> None:
    """Wie publish(), aber ein Fehler eines Empfängers bricht ab (für Zusammenführen: alles oder nichts in einer Transaktion)."""
    if name not in KNOWN:
        raise ValueError(f"Unbekanntes Ereignis {name!r}")
    for handler in app.get(EVENTS, {}).get(name, []):
        result = handler(app, **data)
        if inspect.isawaitable(result):
            await result
