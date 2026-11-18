"""Kommandozeile: python -m nfizyo <befehl>

  run               Server starten (Standard)
  init-db           Datenbank anlegen / Migrationen anwenden
  create-practice   Praxis anlegen:      --name "Physio Muster" --slug muster [--lang de] (nicht mit cloud_demo)
  create-user       Benutzer anlegen:    --email … --name … --roles owner,therapist --practice slug
                                         (Passwort wird abgefragt)
  seed-demo         Demo-Praxis mit fiktiven Daten anlegen (nur wenn leer)
  reset-demo        Demo-Datenbank löschen und neu anlegen (nur mit NFIZYO_DEMO=1 und nur ohne echte Praxis)
  check-ai          Zeigt, welche KI-Bausteine laufen (cloud_demo: mit kleiner Testanfrage, Schlüssel maskiert)
  run-jobs          Offene Hintergrundaufgaben einmal abarbeiten
  check-connections SMS, E-Mail, HIN, MediData, Video, bexio prüfen (ohne Zusatz wird nichts gesendet)
                    [--sms NUMMER] [--mail ADRESSE] [--hin ADRESSE] [--praxis KURZNAME] – siehe docs/VERBINDUNGSTEST.md
  medidata-csr      Schlüssel (RSA 2048, 0600) + CSR für das MediData-Client-Zertifikat: --org FIRMA --city ORT
                    [--client-id ID] [--key PFAD] [--csr PFAD] [--new-key]
  medidata-renew    Client-Zertifikat erneuern (ab 60 Tage vor Ablauf) [--csr PFAD] [--json] [--force]
  medidata-client-id  neue X-Client-ID (Wechsel MediData-Box/VA → Webservice): --instance-id CLOUDCLIENTINSTANCEID
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sqlite3
import sys

from aiohttp import web

from .core.config import load_settings


def _app(settings, **kw):
    from .app import create_app

    return create_app(settings, **kw)


def cmd_run(args) -> None:
    settings = load_settings(args.env)
    app = _app(settings)
    logging.getLogger(__name__).info("N-Fizyo läuft auf http://%s:%s", settings.host, settings.port)
    web.run_app(app, host=settings.host, port=settings.port, access_log=None, print=None)


def cmd_init_db(args) -> None:
    settings = load_settings(args.env)
    _app(settings, start_workers=False, seed_demo=False)
    print(f"Datenbank bereit: {settings.db_path}")


SLUG_RX = r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?"


def cmd_create_practice(args) -> None:
    import re
    import sqlite3

    from .core.db import utcnow
    from .core.keys import DB

    settings = load_settings(args.env)
    if settings.ai_mode == "cloud_demo":
        # Externer KI-Dienst nur mit Demo-Praxen (fiktive Daten) – eine echte Praxis darf hier nicht entstehen
        sys.exit("create-practice ist mit NFIZYO_AI_MODE=cloud_demo nicht erlaubt (nur Demo-Praxen mit fiktiven Daten). "
                 "Für eine echte Praxis: NFIZYO_AI_MODE=local oder remote und ein eigener Datenordner.")
    name = (args.name or "").strip()
    if not name:
        sys.exit("Name der Praxis fehlt (--name).")
    if not re.fullmatch(SLUG_RX, args.slug or ""):
        sys.exit(f"Kurzname '{args.slug}' ungültig: nur Kleinbuchstaben a–z, Ziffern und «-» (z. B. muster oder physio-biel).")
    app = _app(settings, start_workers=False, seed_demo=False)
    db = app[DB]
    if db.value("SELECT 1 FROM practices WHERE slug = ?", (args.slug,)):
        sys.exit(f"Praxis mit dem Kurznamen '{args.slug}' gibt es schon – nichts geändert.")
    try:
        pid = db.execute(
            "INSERT INTO practices (name, slug, default_lang, created_at) VALUES (?,?,?,?)",
            (name, args.slug, args.lang, utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        sys.exit(f"Praxis nicht angelegt: {exc}")
    (settings.scan_dir / args.slug).mkdir(parents=True, exist_ok=True)
    print(f"Praxis angelegt: #{pid} {name} ({args.slug})")


def cmd_create_user(args) -> None:
    from .core.auth import MIN_PASSWORD, create_user
    from .core.keys import DB

    settings = load_settings(args.env)
    app = _app(settings, start_workers=False, seed_demo=False)
    db = app[DB]
    row = db.one("SELECT id FROM practices WHERE slug = ?", (args.practice,))
    if row is None:
        sys.exit(f"Praxis '{args.practice}' nicht gefunden.")
    practice_id = row["id"]
    password = getpass.getpass(f"Passwort (mind. {MIN_PASSWORD} Zeichen): ")
    if password != getpass.getpass("Passwort wiederholen: "):
        sys.exit("Passwörter stimmen nicht überein.")
    try:
        uid = create_user(db, practice_id, args.email, args.name, args.roles, password, args.lang)
    except (ValueError, sqlite3.IntegrityError) as exc:
        sys.exit(f"Konto nicht angelegt: {exc}")
    print(f"Benutzer angelegt: #{uid} {args.email} ({args.roles})")


def cmd_seed_demo(args) -> None:
    settings = load_settings(args.env)
    _app(settings, start_workers=False, seed_demo=True)
    print("Demo-Praxis bereit.")


def _real_practices(db_path) -> int | None:
    """Anzahl Praxen, die keine Demo-Praxis sind (Datenbank nur lesend geöffnet). None = keine Datenbank."""
    import sqlite3

    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(practices)")}
        if not cols:
            return 0
        if "is_demo" not in cols:          # sehr alte Datenbank ohne Kennzeichen: jede Praxis gilt als echt
            return conn.execute("SELECT COUNT(*) FROM practices").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM practices WHERE is_demo = 0").fetchone()[0]
    finally:
        conn.close()


def cmd_reset_demo(args) -> None:
    import sqlite3

    settings = load_settings(args.env)
    if not settings.demo:
        sys.exit("reset-demo ist nur im Demo-Modus erlaubt (NFIZYO_DEMO=1).")
    try:
        real = _real_practices(settings.db_path)
    except sqlite3.Error as exc:
        sys.exit(f"reset-demo abgebrochen: Datenbank nicht lesbar ({exc}) – nichts gelöscht.")
    if real:
        sys.exit(f"reset-demo abgebrochen: In der Datenbank gibt es {real} echte Praxis/Praxen (keine Demo-Praxis) – nichts gelöscht. "
                 "reset-demo löscht nur reine Demo-Datenbanken.")
    for suffix in ("", "-wal", "-shm"):
        p = settings.db_path.with_name(settings.db_path.name + suffix)
        if p.exists():
            p.unlink()
    import shutil

    for d in (settings.audio_dir, settings.documents_dir):
        if d.exists():
            shutil.rmtree(d)
    _app(settings, start_workers=False, seed_demo=True)
    print("Demo-Datenbank neu angelegt.")


def cmd_check_ai(args) -> None:
    from .core.keys import AI

    settings = load_settings(args.env)
    app = _app(settings, start_workers=False, seed_demo=False)
    ai = app[AI]
    out = ai.status()
    if ai.mode == "cloud_demo":  # nur Vorführung: kleine echte Testanfragen ohne Patientendaten, Schlüssel nur maskiert
        out["cloud_test"] = ai.self_test()
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_run_jobs(args) -> None:
    from .core.keys import JOBS

    settings = load_settings(args.env)
    app = _app(settings, start_workers=False, seed_demo=False)
    n = asyncio.run(app[JOBS].run_all(app))
    print(f"{n} Aufgaben erledigt.")


def cmd_check_connections(args) -> None:
    from .connections import FAIL, render, run_all

    settings = load_settings(args.env)
    app = _app(settings, start_workers=False, seed_demo=False)
    checks = asyncio.run(run_all(app, sms_to=args.sms, mail_to=args.mail, hin_to=args.hin, practice=args.praxis, ai_test=args.ki))
    print(render(checks))
    sys.exit(1 if any(c.status == FAIL for c in checks) else 0)


def _medidata_cli(args, run) -> None:
    from . import medidata_cli

    settings = load_settings(args.env)
    out: list[str] = []
    code = run(medidata_cli, settings, out)
    if asyncio.iscoroutine(code):
        code = asyncio.run(code)
    print("\n".join(out))
    sys.exit(code)


def cmd_medidata_csr(args) -> None:
    _medidata_cli(args, lambda m, s, out: m.csr(s, org=args.org, city=args.city, client_id=args.client_id, key=args.key,
                                                csr_path=args.csr, new_key=args.new_key, out=out))


def cmd_medidata_renew(args) -> None:
    _medidata_cli(args, lambda m, s, out: m.renew(s, csr_path=args.csr, accept="json" if args.json else "pem", force=args.force,
                                                  out=out))


def cmd_medidata_client_id(args) -> None:
    _medidata_cli(args, lambda m, s, out: m.client_id(s, instance_id=args.instance_id, env_file=args.env, out=out))


def cmd_versions(args) -> None:
    """Programmversion und Version jedes Moduls (für deploy/update.sh und die NSystem-Übersicht)."""
    import json

    from . import __version__
    from .modules import load_modules

    data = {"app": __version__, "modules": {m.name: m.version for m in load_modules()}}
    if args.json:
        print(json.dumps(data, sort_keys=True))
    else:
        print(f"N-Fizyo {data['app']}")
        for name, version in data["modules"].items():
            print(f"  {name:14} {version}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="nfizyo", description="N-Fizyo – Praxis-Software für Physiotherapie")
    parser.add_argument("--env", default=None, help="Pfad zur .env-Datei (Standard: ./.env)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Server starten")
    sub.add_parser("init-db", help="Datenbank anlegen oder auf den Stand dieser Version bringen")
    sub.add_parser("migrate", help="Datenbank auf den Stand dieser Version bringen (wie init-db)")
    p = sub.add_parser("versions", help="Version der Software und der Module anzeigen")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("create-practice", help="Neue Praxis anlegen (Name, Kurzname, Sprache)")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--lang", default="de", choices=("de", "fr"))
    p = sub.add_parser("create-user", help="Konto anlegen; das Passwort wird verdeckt abgefragt")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--roles", required=True, help="owner (Leitung), therapist (Therapie), reception (Empfang); mehrere mit Komma")
    p.add_argument("--practice", required=True, help="Kurzname der Praxis")
    p.add_argument("--lang", default="de", choices=("de", "fr"))
    sub.add_parser("seed-demo", help="Demo-Praxis mit fiktiven Daten anlegen (nur mit NFIZYO_DEMO=1)")
    sub.add_parser("reset-demo", help="Demo-Daten neu aufsetzen (verweigert, sobald eine echte Praxis existiert)")
    sub.add_parser("check-ai", help="KI-Dienste prüfen (Spracherkennung, Entwürfe, Übersetzung)")
    sub.add_parser("run-jobs", help="Wartende Hintergrundaufgaben einmal abarbeiten")
    p = sub.add_parser("check-connections", help="Verbindungen prüfen (siehe docs/VERBINDUNGSTEST.md)")
    p.add_argument("--sms", help="echte Test-SMS an diese Nummer")
    p.add_argument("--mail", help="echte Test-E-Mail an diese Adresse")
    p.add_argument("--hin", help="echte Test-Nachricht über HIN an diese Adresse")
    p.add_argument("--praxis", help="Kurzname der Praxis (für bexio mit OAuth)")
    p.add_argument("--ki", action="store_true", help="kleine echte KI-Aufgaben ohne Patientendaten (Stille erkennen, «Guten Tag» übersetzen)")
    p = sub.add_parser("medidata-csr", help="Schlüssel und CSR für das MediData-Client-Zertifikat (docs/VERBINDUNGSTEST.md)")
    p.add_argument("--org", required=True, help="O = Firma der Praxis, z. B. \"Physio Muster AG\"")
    p.add_argument("--city", required=True, help="L = Ort, z. B. Biel")
    p.add_argument("--client-id", help="CN = X-Client-ID (Standard: NFIZYO_MEDIDATA_CLIENT_ID)")
    p.add_argument("--key", help="privater Schlüssel (Standard: NFIZYO_MEDIDATA_KEY); vorhandener wird weiterverwendet")
    p.add_argument("--csr", help="Ziel für den CSR (Standard: neben dem Schlüssel, Endung .csr)")
    p.add_argument("--new-key", action="store_true", help="neuen Schlüssel erzeugen (<schlüssel>.new.key) – wird bei medidata-renew übernommen")
    p = sub.add_parser("medidata-renew", help="MediData-Client-Zertifikat erneuern (ab 60 Tage vor Ablauf)")
    p.add_argument("--csr", help="CSR-Datei (Standard: <schlüssel>.new.csr, sonst <schlüssel>.csr)")
    p.add_argument("--json", action="store_true", help="Antwort als JSON anfordern (serialNumber, validFrom, validTo, pkcs7)")
    p.add_argument("--force", action="store_true", help="auch mehr als 60 Tage vor Ablauf senden")
    p = sub.add_parser("medidata-client-id", help="neue X-Client-ID beim Wechsel MediData-Box/VA → Webservice")
    p.add_argument("--instance-id", required=True, help="cloudclientinstanceid (gibt MediData bei der Migration bekannt)")
    args = parser.parse_args(argv)
    handlers = {
        None: cmd_run, "run": cmd_run, "init-db": cmd_init_db, "create-practice": cmd_create_practice,
        "create-user": cmd_create_user, "seed-demo": cmd_seed_demo, "reset-demo": cmd_reset_demo,
        "check-ai": cmd_check_ai, "run-jobs": cmd_run_jobs, "migrate": cmd_init_db, "versions": cmd_versions,
        "check-connections": cmd_check_connections, "medidata-csr": cmd_medidata_csr, "medidata-renew": cmd_medidata_renew,
        "medidata-client-id": cmd_medidata_client_id,
    }
    from .ai.cloud import CloudDemoRefused

    try:
        handlers[args.cmd](args)
    except CloudDemoRefused as exc:
        sys.exit(f"N-Fizyo startet nicht: {exc}")


if __name__ == "__main__":
    main()
