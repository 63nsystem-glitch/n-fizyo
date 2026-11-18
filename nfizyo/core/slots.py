"""Plätze in den Bildschirmen, an die Module etwas anhängen («Steckplätze»).

Ein Bildschirm legt fest, WO etwas erscheinen kann; Module legen fest, WAS dort erscheint.
So kann ein neues Modul eine Karte, einen Reiter oder einen Knopf hinzufügen, ohne fremden Code zu ändern.
Ist ein Modul für die Praxis ausgeschaltet, verschwinden seine Beiträge automatisch.

Bekannte Plätze:
  therapie.patient.left|center|right   Karten in der Akte (provider(request, patient) → ctx für template)
  therapie.patient.tab                 Reiter der Akte ({"key","label_key","path","count"})
  therapie.patient.fact                Kurzinfo in der Kopfzeile (Text)
  therapie.patient.action              Knopf in der Kopfzeile ({"label_key","icon","href","post","primary"})
  therapie.today.card                  Karten auf «Heute» (provider(request) → ctx)
  therapie.today.next                  Zusatz im Feld «Als Nächstes» auf «Heute» (provider(request, appointment) → ctx)
  therapie.today.todo                  Offene Punkte auf «Heute» ({"label_key","icon","href","n"})
  therapie.appointment.action          Knöpfe bei einem Termin (provider(request, appointment) → {"label_key","icon","href","primary"})
  therapie.dictation.review            Zusatzkarten beim Prüfen eines Diktats (provider(request, recording) → ctx)
  empfang.patient.card                 Karten im Patienten-Stammblatt des Empfangs
  empfang.patient.action               Knöpfe im Stammblatt des Empfangs
  empfang.appointment.action           Knöpfe bei einem Termin im Empfang (wie therapie.appointment.action)
  leitung.overview.tile                Kennzahl-Kacheln der Leitung (provider(request) → {"label_key","value","sub"})
  leitung.overview.card                Karten der Leitungsübersicht (provider(request) → ctx)
  leitung.overview.work                Offene Arbeit in der Übersicht ({"label_key","n","icon","href"})
  leitung.overview.usage               Nutzungszahlen in der Übersicht ({"label_key","n"})
  leitung.settings                     Bereiche der Einstellungen ({"key","label_key","path"})
  patient.home.card                    Karten auf der Startseite der Patienten-App (provider(request) → ctx)
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web

from .keys import DB, MODULES
from .modules import module_enabled

log = logging.getLogger(__name__)

SLOTS = web.AppKey("nfizyo_slots", dict)


@dataclass
class Contribution:
    module: str
    slot: str
    order: int
    provider: Callable[..., Any]
    template: str | None = None


def contribute(app: web.Application, contribution: Contribution) -> None:
    app.setdefault(SLOTS, {}).setdefault(contribution.slot, []).append(contribution)


def _practice_id(request: web.Request) -> int | None:
    if request.get("user") is not None:
        return request["user"]["practice_id"]
    if request.get("patient_session") is not None:
        return request["patient_session"]["practice_id"]
    return None


async def collect(request: web.Request, slot: str, *args: Any) -> list[dict]:
    """Alle Beiträge eines Platzes: [{"module", "template", "ctx"}] (leere/None-Beiträge fallen weg)."""
    app = request.app
    out = []
    for c in sorted(app.get(SLOTS, {}).get(slot, []), key=lambda c: c.order):
        if not module_enabled(app[DB], _practice_id(request), c.module, app[MODULES]):
            continue
        try:
            value = c.provider(request, *args)
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # eine kaputte Karte darf den Bildschirm nie blockieren
            log.exception("Platz %s: Beitrag von %s fehlgeschlagen", slot, c.module)
            value = None
        if value:
            out.append({"module": c.module, "template": c.template, "ctx": value})
    return out


async def patient_tabs(request: web.Request, patient, active: str) -> list[dict]:
    """Reiter der Akte im Therapie-Bildschirm."""
    from .screens import prefix

    base = f"{prefix(request)}/patienten/{patient['id']}"
    tabs = [{"key": "overview", "label_key": "patients.tab_overview", "href": base, "active": active == "overview", "count": 0}]
    for item in await collect(request, "therapie.patient.tab", patient):
        t = item["ctx"]
        tabs.append({"key": t["key"], "label_key": t["label_key"], "href": t.get("href") or f"{base}/{t['path']}",
                     "active": t["key"] == active, "count": t.get("count", 0)})
    return tabs
