"""Die Bildschirme von N-Fizyo – jeder ist eine eigene Anwendung mit eigener Adresse und eigenem Menü.

  /therapie   Therapie (jede Therapeutin / jeder Therapeut): eigene Patient/innen, eigener Tag, Diktat. Tablet + Telefon.
  /empfang    Empfang: Agenda aller, Patientenaufnahme, QR-Einladung, Posteingang zuordnen, Rechnungen vorbereiten. PC.
  /leitung    Leitung: Team, Vertretungen, Übersicht, Rechnungen kontrollieren, Einstellungen. PC. Ohne medizinische Inhalte.
  /p          Patient/in: eigenes Telefon, Zugang per QR-Code (eigene Sitzung, siehe patientapp).

Module hängen ihre Seiten mit ctx.page("therapie", "/heute", handler) an einen Bildschirm.
Der Bildschirm prüft Anmeldung, Rolle und ob das Modul für die Praxis eingeschaltet ist.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import quote

from aiohttp import web

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass(frozen=True)
class Screen:
    key: str
    prefix: str
    role: str
    title_key: str
    icon: str
    home: str
    medical: bool = False      # nur hier gibt es medizinische Inhalte
    fab: str | None = None     # grosser Knopf oben in der Seitenleiste (z. B. Diktat)


SCREENS: dict[str, Screen] = {
    "therapie": Screen("therapie", "/therapie", "therapist", "screen.therapie", "stethoscope", "/heute", medical=True, fab="/diktat"),
    "empfang": Screen("empfang", "/empfang", "reception", "screen.empfang", "event_available", "/heute"),
    "leitung": Screen("leitung", "/leitung", "owner", "screen.leitung", "leaderboard", "/uebersicht"),
}
ROLE_ORDER = ("therapist", "reception", "owner")


def roles_of(user) -> set[str]:
    if user is None:
        return set()
    raw = user["roles"] if "roles" in user.keys() else ""
    return {r.strip() for r in str(raw).split(",") if r.strip()}


def screens_for(user) -> list[Screen]:
    roles = roles_of(user)
    return [s for s in SCREENS.values() if s.role in roles]


@dataclass(frozen=True)
class NavItem:
    label_key: str
    icon: str
    path: str                 # relativ zum Bildschirm, z. B. "/heute"
    order: int = 100
    badge: str | None = None  # Name eines Zählers (app[BADGES])
    phone: bool = False       # in der unteren Leiste auf dem Telefon
    rail: bool = True         # in der Seitenleiste (Tablet/PC)
    short_key: str | None = None  # kürzere Beschriftung für die untere Leiste (Telefon), z. B. «Patienten»


@dataclass
class NavEntry:
    screen: str
    module: str
    item: NavItem


NAV = web.AppKey("nfizyo_screen_nav", list)


def screen_page(screen_key: str, module_name: str | None, handler: Handler) -> Handler:
    """Hülle um eine Seite: angemeldet, richtige Rolle, Modul eingeschaltet. Setzt request['screen']."""
    screen = SCREENS[screen_key]

    @functools.wraps(handler)
    async def wrapper(request: web.Request):
        from .keys import DB, MODULES
        from .modules import module_enabled

        user = request.get("user")
        if user is None:
            raise web.HTTPFound("/login?next=" + quote(request.path_qs, safe=""))
        if screen.role not in roles_of(user):
            raise web.HTTPForbidden()
        if module_name and not module_enabled(request.app[DB], user["practice_id"], module_name, request.app[MODULES]):
            raise web.HTTPNotFound()
        request["screen"] = screen
        return await handler(request)

    wrapper.csrf_exempt = getattr(handler, "csrf_exempt", False)  # type: ignore[attr-defined]
    return wrapper


def prefix(request: web.Request) -> str:
    """Adresse des aktuellen Bildschirms, z. B. '/therapie' (für Links und Weiterleitungen)."""
    screen = request.get("screen")
    return screen.prefix if screen else ""
