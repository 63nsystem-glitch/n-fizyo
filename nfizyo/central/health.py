"""Bewertung der Meldungen: Warnungen (mit Push) und Checkliste für neue Boxen."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    kind: str
    message: str
    push: bool = True


CLOCK_TOLERANCE_S = 20   # Anmeldecodes (TOTP) gelten 30 s; darüber scheitern Anmeldungen


def clock_message(offset_s: int) -> str:
    return (f"Uhr der Box geht {offset_s:+d} s falsch – Anmeldung mit Code kann scheitern, Zeiten auf Rechnungen stimmen nicht"
            " (Zeitabgleich prüfen: timedatectl)")


def issues(report: dict) -> list[Issue]:
    out: list[Issue] = []
    mode = report.get("mode") or {}
    runner = report.get("runner") or {}
    disk = report.get("disk") or {}
    jobs = report.get("jobs") or {}
    outbox = report.get("outbox") or {}
    ai = report.get("ai") or {}
    if not runner or not runner.get("updated_at") or time.time() - float(runner.get("updated_at") or 0) > 600:
        out.append(Issue("runner", "Runner-Dienst meldet nicht (Sicherungs- und Gerätezustand unbekannt)"))
    else:
        age = runner.get("backup_age_h")
        last = runner.get("backup") or {}
        if (last.get("local") or {}).get("state") == "failed":
            out.append(Issue("backup", "Letzte Sicherung fehlgeschlagen"))
        elif age is None or age > 30:
            out.append(Issue("backup", f"Letzte Sicherung zu alt ({'keine' if age is None else f'{age:.0f} h'})"))
        off = last.get("offsite") or {}
        if off.get("state") == "failed":
            since = off.get("last_ok_at")
            hours = f", letzte gelungene vor {(time.time() - float(since)) / 3600:.0f} h" if since else ", noch nie gelungen"
            out.append(Issue("offsite", f"Externe Sicherung fehlgeschlagen ({off.get('error') or 'unbekannt'}{hours})"))
        services = runner.get("services") or {}
        if services.get("n-fizyo-backup.timer") not in (None, "active"):
            out.append(Issue("backup_timer", "Sicherungs-Zeitplan nicht aktiv"))
        g = runner.get("gpu") or {}
        if isinstance(g, dict) and (g.get("temp_c") or 0) >= 85:
            out.append(Issue("gpu_hot", f"Grafikkarte heiss ({g.get('temp_c')} °C)"))
    off = report.get("clock_offset_s")
    if isinstance(off, (int, float)) and abs(off) > CLOCK_TOLERANCE_S:
        out.append(Issue("clock", clock_message(int(off))))
    elif report.get("clock_synced") is False:
        out.append(Issue("clock_sync", "Zeitabgleich (NTP) der Box ist aus – die Uhr kann abweichen", push=False))
    if (disk.get("used_pct") or 0) >= 85:
        out.append(Issue("disk", f"Speicher fast voll ({disk.get('used_pct')} %)"))
    if not mode.get("demo"):
        missing = [k for k in ("stt", "llm", "translate") if ai.get(k) is False]
        if missing:
            out.append(Issue("ai", "KI nicht bereit: " + ", ".join(missing)))
    if (jobs.get("oldest_queued_min") or 0) > 30:
        out.append(Issue("jobs_stuck", f"Warteschlange steht ({jobs.get('oldest_queued_min')} min)"))
    if (jobs.get("failed_24h") or 0) > 0:
        out.append(Issue("jobs_failed", f"{jobs.get('failed_24h')} fehlgeschlagene Aufgaben (24 h)", push=False))
    if (outbox.get("sms_failed_24h") or 0) > 0:
        out.append(Issue("sms", f"{outbox.get('sms_failed_24h')} SMS nicht zugestellt (24 h)", push=False))
    if (outbox.get("hin_failed_24h") or 0) > 0:
        out.append(Issue("hin", f"{outbox.get('hin_failed_24h')} HIN-Sendungen fehlgeschlagen (24 h)", push=False))
    out += _medidata_cert(report.get("medidata_cert"))
    for c in report.get("commands") or []:
        if c.get("status") in ("failed", "rejected"):
            out.append(Issue("command", f"Befehl {c.get('status')}: {str(c.get('reason') or '')[:60]}"))
            break
    return out


def _medidata_cert(mc) -> list[Issue]:
    """MediData-Client-Zertifikat: Warnung ab 60 Tage vor Ablauf (dann ist die Erneuerung möglich), Fehler wenn abgelaufen/unbrauchbar."""
    if not isinstance(mc, dict):
        return []
    state, days = mc.get("state"), mc.get("days_left")
    days = days if isinstance(days, int) and not isinstance(days, bool) else None
    if state == "expired":
        return [Issue("medidata_cert_error", "MediData-Zertifikat abgelaufen – Senden an MediData nicht möglich")]
    if state == "error":
        return [Issue("medidata_cert_error", "MediData-Zertifikat unbrauchbar (Schlüssel passt nicht, Datei/Rechte) – check-connections")]
    if state == "warn" and days is not None and 0 <= days <= 60:
        return [Issue("medidata_cert", f"MediData-Zertifikat läuft in {days} Tagen ab – erneuern: python -m nfizyo medidata-renew")]
    return []


CHECKLIST = (
    ("first_report", "Erste Meldung empfangen", True),
    ("https", "HTTPS aktiv (Zertifikat)", True),
    ("demo_off", "Demo-Modus aus", True),
    ("require_2fa", "Zwei-Schritt-Anmeldung Pflicht", True),
    ("staff_2fa", "Alle Konten mit Zwei-Schritt-Anmeldung", True),
    ("ai_ready", "KI bereit (Sprache, Text, Übersetzung)", True),
    ("runner", "Runner-Dienst aktiv", True),
    ("backup", "Sicherung jünger als 26 h", True),
    ("disk", "Speicher unter 80 %", True),
    ("offsite", "Externe Sicherung (verschlüsselt) gelungen", False),
    ("practice_data", "Praxis: GLN, Adresse, IBAN erfasst", True),
    ("sms", "SMS-Versand eingerichtet", False),
    ("hin", "HIN eingerichtet", False),
    ("medidata", "MediData (Test oder produktiv)", False),
    ("support", "Supportzugang eingerichtet (bleibt zu, bis die Praxis ihn öffnet)", False),
    ("release_key", "Nur signierte Versionen (Release-Schlüssel auf der Box)", False),
)


def checklist(report: dict | None) -> list[dict]:
    r = report or {}
    mode = r.get("mode") or {}
    runner = r.get("runner") or {}
    practices = [p for p in r.get("practices") or [] if not p.get("demo")]
    ai = r.get("ai") or {}
    ok = {
        "first_report": bool(report),
        "https": bool(mode.get("https")),
        "demo_off": bool(report) and not mode.get("demo"),
        "require_2fa": bool(mode.get("require_2fa")),
        "staff_2fa": bool(practices) and all((p.get("staff_without_2fa") or 0) == 0 for p in practices),
        "ai_ready": bool(ai) and all(ai.get(k) for k in ("stt", "llm", "translate")),
        "runner": bool(runner.get("updated_at")) and time.time() - float(runner.get("updated_at") or 0) < 600,
        "backup": runner.get("backup_age_h") is not None and runner.get("backup_age_h") < 26
        and ((runner.get("backup") or {}).get("local") or {}).get("state") != "failed",
        "offsite": ((runner.get("backup") or {}).get("offsite") or {}).get("state") == "ok",
        "disk": (r.get("disk") or {}).get("used_pct") is not None and (r.get("disk") or {}).get("used_pct") < 80,
        "practice_data": bool(practices) and all(p.get("has_gln") and p.get("has_address") and p.get("has_iban") for p in practices),
        "sms": mode.get("sms") == "live" and bool(mode.get("sms_providers")),
        "hin": bool(mode.get("hin")),
        "medidata": mode.get("medidata") in ("test", "production"),
        "support": bool((runner.get("support") or {}).get("ready")),
        "release_key": bool(mode.get("release_signed")),
    }
    return [{"key": k, "label": label, "required": req, "ok": ok[k]} for k, label, req in CHECKLIST]
