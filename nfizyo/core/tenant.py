"""Praxis-Trennung (Mandantenfähigkeit).

Jede Anfrage legt zuerst fest, zu welcher Praxis sie gehört. Ohne Praxis wirft
jeder Datenzugriff einen Fehler – es wird nie stillschweigend eine andere Praxis
verwendet. Die Tests in tests/test_tenant_isolation.py dürfen nie gelöscht werden.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_practice: ContextVar[int | None] = ContextVar("nfizyo_practice", default=None)
_user: ContextVar[int | None] = ContextVar("nfizyo_user", default=None)
_patient_account: ContextVar[int | None] = ContextVar("nfizyo_patient_account", default=None)


class NoPracticeError(RuntimeError):
    """Datenzugriff ohne festgelegte Praxis."""


def current_practice_id() -> int:
    value = _practice.get()
    if value is None:
        raise NoPracticeError("Keine Praxis festgelegt – Datenzugriff verweigert.")
    return value


def maybe_practice_id() -> int | None:
    return _practice.get()


def current_user_id() -> int | None:
    return _user.get()


def current_patient_account() -> int | None:
    """Patienten-ID, wenn die Anfrage aus der Patienten-App kommt."""
    return _patient_account.get()


@contextmanager
def practice_scope(practice_id: int | None, user_id: int | None = None, patient_id: int | None = None) -> Iterator[None]:
    t1 = _practice.set(practice_id)
    t2 = _user.set(user_id)
    t3 = _patient_account.set(patient_id)
    try:
        yield
    finally:
        _patient_account.reset(t3)
        _user.reset(t2)
        _practice.reset(t1)
