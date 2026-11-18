"""Öffentliche Schnittstelle der Verlaufsdokumentation – nur das hier dürfen andere Module benutzen.

Notizen haben vier Teile (SECTIONS). Ein Entwurf wird von der Therapeutin / dem Therapeuten freigegeben
und ist danach gesperrt (Änderungen nur als Nachtrag mit Grund). Freigeben meldet «note.approved».
"""
from __future__ import annotations

from aiohttp import web

from ...core.db import Database
from ...core.events import publish
from ...core.keys import DB
from . import repo
from .repo import SECTIONS, NoteLocked  # noqa: F401 - Teil der Schnittstelle


def get(db: Database, note_id: int):
    return repo.get(db, note_id)


def for_patient(db: Database, patient_id: int, limit: int = 200):
    return repo.for_patient(db, patient_id, limit)


def for_appointment(db: Database, appointment_id: int):
    return repo.for_appointment(db, appointment_id)


def drafts_for(db: Database, author_id: int | None = None):
    return repo.drafts_for(db, author_id)


def draft_count(db: Database, author_id: int | None = None) -> int:
    return repo.draft_count(db, author_id)


def appointment_ids_with_note(db: Database, since: str | None = None) -> set[int]:
    return repo.appointment_ids_with_note(db, since)


def assessments(db: Database, patient_id: int, since: str | None = None, limit: int = 5):
    return repo.assessments(db, patient_id, since, limit)


def create_draft(db: Database, patient_id: int, sections: dict, *, appointment_id: int | None = None, source: str = "typed",
                 recording_id: int | None = None, session_date: str | None = None, author_id: int | None = None) -> int:
    return repo.create(db, patient_id, sections, appointment_id=appointment_id, source=source, recording_id=recording_id,
                       session_date=session_date, author_id=author_id)


def save(db: Database, note_id: int, sections: dict, reason: str | None = None) -> int:
    return repo.save(db, note_id, sections, reason)


async def approve(app: web.Application, note_id: int) -> None:
    """Freigeben und melden (Agenda setzt den Termin auf «erledigt», weitere Module können reagieren)."""
    db = app[DB]
    note = repo.get(db, note_id)
    if note is None or note["status"] == "approved":
        return
    repo.approve(db, note_id)
    await publish(app, "note.approved", note_id=note_id, patient_id=note["patient_id"], appointment_id=note["appointment_id"],
                  author_id=note["author_id"])

