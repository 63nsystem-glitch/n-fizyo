"""Patienten-App (PWA, läuft vom Praxis-Server): Einladung per QR-Code, Anmeldung, Startseite, Profil.

Die Inhalte (Termine, Übungen, Infos, Nachrichten, Anliegen) bringen die jeweiligen Module selbst mit
(Seiten unter /p/…, Menüpunkte «patient», Karten im Steckplatz «patient.home.card»)."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="patientapp",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="p.app_name",
    description_key="pstaff.description",
    version="2.1.1",
    requires=("patients",),
    tables=("patient_invites", "patient_consents", "patient_login_codes"),
)
