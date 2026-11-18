"""Module von N-Fizyo und ihre «Kimlik kartı» (Ausweis).

Jedes Modul ist ein eigener Ordner unter nfizyo/modules/<name>/:

    __init__.py      MODULE = Module(...)   – Ausweis: Name, Version, braucht/nutzt, eigene Tabellen
    api.py           Was andere Module benutzen dürfen (nur das!)
    views.py         Seiten; werden in setup(ctx) an Bildschirme gehängt
    repo.py          Datenzugriff auf die EIGENEN Tabellen (immer mit practice_id)
    migrations/      SQL-Dateien NNN_name.sql
    locales/         de.json, fr.json (+ Patiententexte in allen App-Sprachen)
    templates/<name> Jinja-Vorlagen

Regeln (werden in tests/test_architecture.py geprüft):
  1. Ein Modul importiert nur den Kern, ai/, knowledge/, sich selbst und die api.py von Modulen,
     die im Ausweis unter requires (Pflicht) oder uses (wenn eingeschaltet) stehen.
  2. Ein Modul schreibt nur in eigene Tabellen und liest nur eigene Tabellen und Kern-Tabellen.
     Alles andere geht über api.py oder Ereignisse (core/events.py).
  3. Jedes Modul hat eine Version. Ein Update führt die neuen Migrationen aus und merkt sich die Version.

Module können pro Praxis ein- und ausgeschaltet werden (Tabelle practice_modules). Ist ein Pflicht-Modul
ausgeschaltet, ist auch das abhängige Modul aus.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .screens import NavItem  # noqa: F401 - für Module: from ...core.modules import NavItem

if TYPE_CHECKING:
    from aiohttp import web

    from .db import Database

CORE_TABLES = frozenset({
    "practices", "practice_modules", "users", "sessions", "audit_log", "outbox", "jobs", "translations",
    "insurers", "referrers", "patients", "prescriptions", "patient_team", "delegations", "module_versions", "schema_migrations",
    "box_commands", "box_state", "patient_merges",
    "box_support_sessions", "box_events", "box_support_requests", "box_notices", "box_notice_dismissals",
})


@dataclass
class Module:
    name: str
    package_dir: Path
    setup: Callable[["ModuleContext"], None]
    title_key: str
    version: str = "1.0.0"
    requires: tuple[str, ...] = ()      # Pflicht: ohne diese Module läuft es nicht
    uses: tuple[str, ...] = ()          # Optional: wird genutzt, wenn eingeschaltet
    tables: tuple[str, ...] = ()        # eigene Tabellen
    optional: bool = True               # pro Praxis ausschaltbar
    description_key: str | None = None

    @property
    def migrations_dir(self) -> Path:
        return self.package_dir / "migrations"

    @property
    def locales_dir(self) -> Path:
        return self.package_dir / "locales"

    @property
    def templates_dir(self) -> Path:
        return self.package_dir / "templates"


def module_enabled(db: "Database", practice_id: int | None, name: str, modules: dict[str, Module], _seen: frozenset = frozenset()) -> bool:
    module = modules.get(name)
    if module is None:
        return False
    if name in _seen:
        return True
    if module.optional and practice_id is not None:
        row = db.one("SELECT enabled FROM practice_modules WHERE practice_id = ? AND module = ?", (practice_id, name))
        if row is not None and not row["enabled"]:
            return False
    return all(module_enabled(db, practice_id, dep, modules, _seen | {name}) for dep in module.requires)


def enabled(request, name: str) -> bool:
    """Ist ein (genutztes) Modul für die Praxis der angemeldeten Person eingeschaltet? Für «uses»-Abhängigkeiten."""
    from .keys import DB, MODULES

    user = request.get("user")
    practice_id = user["practice_id"] if user is not None else (
        request["patient_session"]["practice_id"] if request.get("patient_session") is not None else None)
    return module_enabled(request.app[DB], practice_id, name, request.app[MODULES])


def set_module_enabled(db: "Database", practice_id: int, name: str, enabled: bool) -> None:
    db.execute(
        "INSERT INTO practice_modules (practice_id, module, enabled) VALUES (?,?,?)"
        " ON CONFLICT(practice_id, module) DO UPDATE SET enabled = excluded.enabled",
        (practice_id, name, 1 if enabled else 0),
    )


class ModuleContext:
    """Was ein Modul in setup(ctx) tun darf: Seiten an Bildschirme hängen, Menüpunkte, Steckplätze,
    Ereignisse, Hintergrundaufgaben, Zähler. So bleibt jedes Modul in seinem Ordner."""

    def __init__(self, app: "web.Application", module: Module):
        self.app = app
        self.module = module

    # --- Seiten
    def page(self, screen: str, path: str, handler, methods: tuple[str, ...] = ("GET",)) -> None:
        from .screens import SCREENS, screen_page

        wrapped = screen_page(screen, self.module.name, handler)
        for method in methods:
            self.app.router.add_route(method, SCREENS[screen].prefix + path, wrapped)

    def patient_page(self, path: str, handler, methods: tuple[str, ...] = ("GET",)) -> None:
        """Seite der Patienten-App (/p/...): nur mit Patientensitzung und wenn das Modul für die Praxis eingeschaltet ist."""
        import functools

        from aiohttp import web as _web

        from .keys import DB, MODULES

        name = self.module.name

        @functools.wraps(handler)
        async def wrapper(request):
            if request.get("patient") is None:
                raise _web.HTTPFound("/p/anmelden")
            if not module_enabled(request.app[DB], request["patient_session"]["practice_id"], name, request.app[MODULES]):
                raise _web.HTTPNotFound()
            return await handler(request)

        wrapper.csrf_exempt = getattr(handler, "csrf_exempt", False)  # type: ignore[attr-defined]
        for method in methods:
            self.app.router.add_route(method, "/p" + path, wrapper)

    def public(self, path: str, handler, methods: tuple[str, ...] = ("GET",)) -> None:
        """Ohne Anmeldung (z. B. Einladungslink, Service Worker). Sparsam verwenden."""
        for method in methods:
            self.app.router.add_route(method, path, handler)

    def nav(self, screen: str, item: NavItem) -> None:
        """Menüpunkt in einem Bildschirm ("therapie", "empfang", "leitung") oder in der Patienten-App ("patient")."""
        from .screens import NAV, NavEntry

        self.app.setdefault(NAV, []).append(NavEntry(screen, self.module.name, item))

    # --- Steckplätze, Ereignisse, Aufgaben
    def slot(self, slot: str, order: int, provider, template: str | None = None) -> None:
        from .slots import Contribution, contribute

        contribute(self.app, Contribution(self.module.name, slot, order, provider, template))

    def on(self, event: str, handler) -> None:
        from .events import subscribe

        subscribe(self.app, event, handler)

    def patient_tables(self, tables: tuple[str, ...], merge_sql: tuple[str, ...]) -> None:
        """Eigene Tabellen mit Patientenbezug anmelden (Leitung › Doppelte Einträge):
        tables     Tabellen mit Spalte patient_id – werden für «Daten vorhanden?» gezählt (nur lesen).
        merge_sql  Anweisungen des Moduls für die EIGENEN Tabellen (Platzhalter :keep, :drop, :pid), die beim Zusammenführen
                   alles vom Doppel-Eintrag (drop) zum behaltenen Eintrag (keep) bringen – auch bei UNIQUE-Konflikten.
        Die Anweisungen stehen im Modul selbst (Bauregel: ein Modul schreibt nur in eigene Tabellen)."""
        from .keys import DB
        from .tenant import current_practice_id

        def linked(app, patient_id: int, found: dict) -> None:
            db = app[DB]
            for table in tables:
                n = db.value(f"SELECT COUNT(*) FROM {table} WHERE practice_id = ? AND patient_id = ?",  # noqa: S608 - Namen aus dem Modul
                             (current_practice_id(), patient_id), 0)
                if n:
                    found[table] = found.get(table, 0) + n

        def merge(app, drop_id: int, keep_id: int) -> None:
            db = app[DB]
            for sql in merge_sql:
                db.execute(sql, {"keep": keep_id, "drop": drop_id, "pid": current_practice_id()})

        self.on("patient.linked", linked)
        self.on("patient.merge", merge)

    def job(self, kind: str, handler, background: bool = False) -> None:
        """Hintergrundaufgabe anmelden. background=True: lange Arbeit ohne wartende Person – läuft nach den anderen."""
        from .keys import JOBS

        self.app[JOBS].register(kind, handler, background)

    def badge(self, name: str, fn: Callable[..., int]) -> None:
        from .keys import BADGES

        self.app[BADGES][name] = fn

    def housekeeping(self, fn) -> None:
        """Regelmässiges Aufräumen der EIGENEN Tabellen (core/housekeeping.py): fn(db, now) -> Anzahl gelöschter Zeilen.
        Nie Offenes löschen (z. B. noch nicht Versendetes)."""
        from .housekeeping import register

        register(self.app, self.module.name, fn)

    def on_startup(self, fn) -> None:
        self.app.on_startup.append(fn)

    def on_cleanup(self, fn) -> None:
        self.app.on_cleanup.append(fn)

    def set(self, key: Any, value: Any) -> None:
        self.app[key] = value
