"""Zusammenbau der Anwendung: Kern + Module + lokale KI, alles in einem Prozess."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from aiohttp import web

from .ai.service import AIServices
from .core import views as core_views
from .core.auth import LoginThrottle
from .core.config import Settings
from .core.db import Database
from .core.i18n import Translations
from .core.jobs import JobQueue
from .core.hin import HinMailbox
from .core.keys import AI, BADGES, DB, HIN, I18N, JOBS, MAIL, MODULES, SETTINGS, SMS, STATIC_VERSION, THROTTLE
from .core.modules import ModuleContext
from .core.mail import MailService
from .core.sms import make_sms
from .core.web import context_middleware, error_middleware, setup_templates
from .modules import load_modules

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
CORE_DIR = PACKAGE_DIR / "core"
WEB_DIR = PACKAGE_DIR / "web"
MAX_UPLOAD = 60 * 1024 * 1024  # Diktate und Berichte


def _static_version(directory: Path) -> str:
    h = hashlib.sha1(usedforsecurity=False)  # nur Cache-Version der statischen Dateien
    for f in sorted(directory.rglob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(str(f.stat().st_mtime_ns).encode())
    return h.hexdigest()[:10]


def _record_versions(db: Database, modules) -> None:
    """Merkt sich die installierte Version jedes Moduls (für Updates und die NSystem-Übersicht)."""
    from .core.db import utcnow

    for m in modules:
        db.execute("INSERT INTO module_versions (module, version, updated_at) VALUES (?,?,?)"
                   " ON CONFLICT(module) DO UPDATE SET version = excluded.version,"
                   " updated_at = CASE WHEN module_versions.version != excluded.version THEN excluded.updated_at ELSE module_versions.updated_at END",
                   (m.name, m.version, utcnow()))


def create_app(settings: Settings, *, start_workers: bool = True, seed_demo: bool | None = None) -> web.Application:
    if settings.ai_mode == "cloud_demo":
        from .ai.cloud import ensure_demo_only

        ensure_demo_only(settings)  # externer KI-Dienst nur mit NFIZYO_DEMO=1 – sonst startet nichts
    for d in (settings.data_dir, settings.audio_dir, settings.documents_dir, settings.scan_dir):
        d.mkdir(parents=True, exist_ok=True)

    app = web.Application(middlewares=[error_middleware, context_middleware], client_max_size=MAX_UPLOAD)
    app[SETTINGS] = settings
    db = Database(settings.db_path)
    app[DB] = db

    modules = load_modules()
    app[MODULES] = {m.name: m for m in modules}
    applied = db.migrate([("core", CORE_DIR / "migrations")] + [(m.name, m.migrations_dir) for m in modules])
    if applied:
        log.info("Migrationen angewendet: %s", ", ".join(applied))

    i18n = Translations()
    i18n.load_dirs([CORE_DIR / "locales"] + [m.locales_dir for m in modules])
    app[I18N] = i18n

    app[STATIC_VERSION] = _static_version(WEB_DIR / "static")
    template_dirs = [WEB_DIR / "templates"] + [m.templates_dir for m in modules if m.templates_dir.exists()]
    setup_templates(app, template_dirs)

    if settings.ai_mode == "cloud_demo":
        from .ai.cloud import ensure_only_demo_practices

        ensure_only_demo_practices(db)  # externer KI-Dienst nur, wenn alle Praxen Demo-Praxen sind (fiktive Daten)
    app[AI] = AIServices(settings)
    if getattr(app[AI], "cloud", None) is not None:
        from .ai.cloud import ensure_only_demo_practices

        # Nicht nur beim Start: vor jeder Anfrage an den externen Dienst (eine echte Praxis könnte später angelegt werden)
        app[AI].cloud.guard = lambda: ensure_only_demo_practices(db)
    app[THROTTLE] = LoginThrottle()
    app[BADGES] = {}
    app[JOBS] = JobQueue(db)
    app[SMS] = make_sms(settings, db)
    app[MAIL] = MailService(settings, db)
    app[HIN] = HinMailbox(settings)

    core_views.setup(app)
    for module in modules:
        module.setup(ModuleContext(app, module))
    _record_versions(db, modules)
    app.router.add_static("/static", WEB_DIR / "static", name="static")

    if seed_demo if seed_demo is not None else settings.demo:
        from .demo.seed import seed_if_empty

        seed_if_empty(app)

    if start_workers:
        app.on_startup.append(app[JOBS].start)
        app.on_cleanup.append(app[JOBS].stop)
        app.on_startup.append(app[SMS].start)
        app.on_cleanup.append(app[SMS].stop)
        app.on_startup.append(app[MAIL].start)
        app.on_cleanup.append(app[MAIL].stop)
        from .agent import client as agent_client
        from .core import housekeeping

        app.on_startup.append(agent_client.start)
        app.on_cleanup.append(agent_client.stop)
        app.on_startup.append(housekeeping.start)     # alte erledigte Aufgaben, Nachrichten, Sitzungen (CODE-14)
        app.on_cleanup.append(housekeeping.stop)

        async def ai_first_check(app: web.Application) -> None:
            if app[AI].mode != "local":
                app[AI].status()   # KI-Server gleich beim Start im Hintergrund fragen (wartet nicht)

        app.on_startup.append(ai_first_check)

    async def close_db(app: web.Application) -> None:
        db.close()

    app.on_cleanup.append(close_db)
    return app
