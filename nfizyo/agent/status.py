"""Zustand der Box für die NSystem-Übersicht – ausschliesslich technische Angaben, KEINE Patientendaten.

Enthalten: Versionen, Speicher, Warteschlangen (Anzahl), Fehlerzähler, KI-Zustand, Einstellungen (ja/nein),
pro Praxis: Name, Anzahl Konten, Konten ohne Zwei-Schritt-Anmeldung, ob GLN/IBAN/Adresse erfasst sind,
MediData-Client-Zertifikat: nur Zustand (ok/warn/expired/error) und Tage bis zum Ablauf – keine Pfade, keine Client-ID.
0.9.0: Supportzugang (runner.support: eingerichtet/offen bis), ob nur signierte Versionen angenommen werden; dazu im Bericht
(agent/client.py) das Protokoll (nur Art/Zeit) und geprüfte Support-Anfragen.
Nicht enthalten: Namen von Patient/innen, Inhalte, Telefonnummern, E-Mail-Adressen, Protokolle.
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.db import utcnow
from ..core.keys import AI, DB, HIN, MODULES, SETTINGS, SMS

STARTED = time.time()


def runner_dir(settings) -> Path:
    return settings.data_dir / "runner"


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def collect(app) -> dict:
    from ..core.version import app_version

    s = app[SETTINGS]
    db = app[DB]
    usage = shutil.disk_usage(s.data_dir)
    ai = app[AI].status()
    oldest = db.value("SELECT MIN(created_at) FROM jobs WHERE status = 'queued'")
    oldest_min = None
    if oldest:
        oldest_min = int((datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds() // 60)
    practices = []
    for p in db.query("SELECT id, slug, name, gln, zip, city, is_demo FROM practices ORDER BY id"):
        users = db.query("SELECT totp_enabled_at FROM users WHERE practice_id = ? AND active = 1", (p["id"],))
        iban = db.value("SELECT iban FROM billing_settings WHERE practice_id = ?", (p["id"],)) if "billing" in app[MODULES] else None
        practices.append({"slug": p["slug"], "name": p["name"], "demo": bool(p["is_demo"]), "staff": len(users),
                          "staff_without_2fa": sum(1 for u in users if not u["totp_enabled_at"]),
                          "has_gln": bool(p["gln"]), "has_address": bool(p["zip"] and p["city"]), "has_iban": bool(iban)})
    runner = {}
    status_file = runner_dir(s) / "status.json"
    if status_file.exists():
        try:
            runner = json.loads(status_file.read_text())
        except (OSError, json.JSONDecodeError):
            runner = {"error": "unreadable"}
    return {
        "box_id": s.central_box_id,
        "time": utcnow(),
        "app_version": app_version(),
        "modules": {r["module"]: r["version"] for r in db.query("SELECT module, version FROM module_versions")},
        "uptime_s": int(time.time() - STARTED),
        "mode": {"demo": s.demo, "require_2fa": s.require_2fa, "https": s.base_url.startswith("https://"), "ai_mode": s.ai_mode,
                 "sms": s.sms_engine, "sms_providers": [p for p in app[SMS].available() if p != "log"],
                 "hin": app[HIN].configured, "hin_receive": app[HIN].can_receive, "medidata": s.medidata_mode,
                 "release_signed": bool(s.release_base and s.release_pubkey)},
        "ai": {k: bool(v.get("available")) for k, v in ai.items() if isinstance(v, dict) and "available" in v},
        "disk": {"total_gb": round(usage.total / 1e9, 1), "free_gb": round(usage.free / 1e9, 1),
                 "used_pct": round(100 * (usage.total - usage.free) / usage.total, 1) if usage.total else None},
        "db_mb": round(s.db_path.stat().st_size / 1e6, 1) if s.db_path.exists() else 0,
        "jobs": {"queued": db.value("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')", (), 0),
                 "failed_24h": db.value("SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND finished_at >= ?", (_since(24),), 0),
                 "oldest_queued_min": oldest_min},
        "outbox": {"sms_failed_24h": db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms' AND status = 'failed' AND created_at >= ?",
                                              (_since(24),), 0),
                   "sms_queued": db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms' AND status = 'queued'", (), 0),
                   "hin_failed_24h": db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'hin' AND status = 'failed' AND created_at >= ?",
                                              (_since(24),), 0)},
        "practices": practices,
        "runner": runner,
        "medidata_cert": _medidata_cert(s),
        "clock_synced": clock_synced(),
    }


def clock_synced() -> bool | None:
    """Läuft der Zeitabgleich (systemd-timesyncd/chrony)? None = unbekannt (kein systemd, z. B. in Tests)."""
    import shutil
    import subprocess

    if not shutil.which("timedatectl"):
        return None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip().lower()
    return {"yes": True, "no": False}.get(value)


def _medidata_cert(settings) -> dict | None:
    """Ablauf des MediData-Client-Zertifikats (feste Felder wie runner.backup_status). None = keines eingetragen."""
    try:
        from ..modules.billing import medidata_cert

        return medidata_cert.summary(settings)
    except Exception:  # noqa: BLE001 - Meldung an NSystem darf daran nie scheitern
        return {"state": "error", "days_left": None}
