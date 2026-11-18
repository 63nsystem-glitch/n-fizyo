"""Benutzer, Rollen und Sitzungen."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow
from .security import dummy_verify, hash_password, needs_rehash, new_token, token_hash, verify_password

ROLES = ("owner", "therapist", "reception")  # Leitung, Therapie, Empfang
STAFF_SESSION_DAYS = 14   # feste Höchstdauer einer Anmeldung (dazu: Abmeldung nach Inaktivität, Settings.staff_idle_hours)
PATIENT_SESSION_DAYS = 60
MIN_PASSWORD = 10           # überall gleich: Konto-Seite, Team, Kommandozeile
PENDING_MINUTES = 10  # Zeit für den Code aus der App bzw. die Einrichtung
SEEN_EVERY_SECONDS = 60   # «zuletzt aktiv» höchstens einmal pro Minute schreiben


def initials_of(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def normalize_roles(roles) -> str:
    items = [roles] if isinstance(roles, str) else list(roles)
    out = []
    for r in ",".join(items).split(","):
        r = r.strip()
        if not r:
            continue
        if r not in ROLES:
            raise ValueError(f"Unbekannte Rolle: {r}")
        if r not in out:
            out.append(r)
    if not out:
        raise ValueError("Mindestens eine Rolle nötig.")
    return ",".join(out)


def create_user(
    db: Database,
    practice_id: int,
    email: str,
    name: str,
    roles,
    password: str,
    lang: str = "de",
) -> int:
    """roles: "therapist" oder ("owner", "therapist") – eine Person kann mehrere Bildschirme haben."""
    if practice_id is None:
        raise ValueError("Benutzer brauchen eine Praxis.")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Passwort zu kurz (mindestens {MIN_PASSWORD} Zeichen).")
    return db.execute(
        "INSERT INTO users (practice_id, email, name, initials, roles, lang, password_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (practice_id, email.strip().lower(), name.strip(), initials_of(name), normalize_roles(roles), lang,
         hash_password(password), utcnow()),
    )


def authenticate(db: Database, email: str, password: str):
    """Prüft E-Mail + Passwort. Unbekannte Adressen kosten gleich viel Zeit (keine Auskunft, welche Konten es gibt).
    Ein Hash mit älteren, schwächeren Werten wird nach dem erfolgreichen Anmelden still erneuert."""
    user = db.one("SELECT * FROM users WHERE email = ? AND active = 1", (email.strip().lower(),))
    if user is None:
        dummy_verify(password)
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    if needs_rehash(user["password_hash"]):
        db.execute("UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
                   (hash_password(password), user["id"], user["password_hash"]))
        user = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    return user


def create_session(
    db: Database,
    kind: str,
    *,
    user_id: int | None = None,
    patient_id: int | None = None,
    practice_id: int | None = None,
    days: int | None = None,
    stage: str = "ok",
) -> tuple[str, str]:
    """stage: ok = angemeldet | code / setup = nur Zwischenschritt der Zwei-Schritt-Anmeldung (10 Minuten gültig)."""
    token = new_token()
    csrf = new_token(16)
    if stage != "ok":
        delta = timedelta(minutes=PENDING_MINUTES)
    else:
        delta = timedelta(days=days or (STAFF_SESSION_DAYS if kind == "staff" else PATIENT_SESSION_DAYS))
    expires = (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()
    now = utcnow()
    db.execute(
        "INSERT INTO sessions (token_hash, kind, user_id, patient_id, practice_id, csrf, created_at, expires_at, stage, last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (token_hash(token), kind, user_id, patient_id, practice_id, csrf, now, expires, stage, now),
    )
    return token, csrf


def load_session(db: Database, token: str | None, *, idle_hours: float | None = None):
    """Gültige Sitzung oder None. idle_hours (Praxiskonten): ohne Klick so lange → abgemeldet (gleitend);
    die feste Höchstdauer (expires_at) gilt immer zusätzlich. Abgelaufene Sitzungen werden gelöscht."""
    if not token:
        return None
    th = token_hash(token)
    row = db.one("SELECT * FROM sessions WHERE token_hash = ?", (th,))
    if row is None:
        return None
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()
    if row["expires_at"] < now:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
        return None
    if idle_hours:
        seen = row["last_seen_at"] or row["created_at"]
        try:
            seen_dt = datetime.fromisoformat(seen)
        except (TypeError, ValueError):
            seen_dt = now_dt - timedelta(days=365)
        if seen_dt.tzinfo is None:
            seen_dt = seen_dt.replace(tzinfo=timezone.utc)
        if now_dt - seen_dt > timedelta(hours=idle_hours):
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            return None
        if (now_dt - seen_dt).total_seconds() >= SEEN_EVERY_SECONDS:
            db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, th))
    return row


# --------------------------------------------------------------------------- Team verwalten (Leitung)
def update_user(db: Database, user_id: int, *, roles, gln: str | None, zsr: str | None) -> None:
    from .tenant import current_practice_id

    db.execute("UPDATE users SET roles = ?, gln = ?, zsr = ? WHERE id = ? AND practice_id = ?",
               (normalize_roles(roles), gln, zsr, user_id, current_practice_id()))


def set_user_active(db: Database, user_id: int, active: bool) -> None:
    from .tenant import current_practice_id

    db.execute("UPDATE users SET active = ? WHERE id = ? AND practice_id = ?", (1 if active else 0, user_id, current_practice_id()))
    if not active:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_password(db: Database, user_id: int, password: str, *, keep_session_token: str | None = None) -> None:
    """Neues Passwort; alle anderen Anmeldungen dieser Person werden beendet."""
    from .security import hash_password

    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    if keep_session_token:
        db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, token_hash(keep_session_token)))
    else:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def end_patient_sessions(db: Database, practice_id: int, patient_id: int) -> None:
    """Alle Anmeldungen einer Patientin / eines Patienten beenden (z. B. App-Zugang widerrufen)."""
    db.execute("DELETE FROM sessions WHERE kind = 'patient' AND patient_id = ? AND practice_id = ?", (patient_id, practice_id))


def destroy_session(db: Database, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


class LoginThrottle:
    """Bremse gegen Raten und Massenversand (im Speicher dieses Prozesses).

    Schlüssel = Zweck + echte Adresse (core.web.client_ip), Konto oder Telefonnummer, z. B. «login-ip:203.0.113.5».
    - blocked(key, limit, window): so viele Einträge im Zeitfenster → gesperrt (Standard: 8 in 15 Minuten).
    - fail(key) / hit(key): einen Versuch zählen; reset(key): nach Erfolg vergessen.
    - delay(key): wachsende Pause für ein Konto (unabhängig von der Adresse), kurz genug, dass niemand ein fremdes
      Konto lange sperren kann.
    Alte Einträge werden regelmässig entfernt (spätestens nach einem Tag), damit der Speicher nicht wächst."""

    MAX_PER_KEY = 200          # mehr Zeitstempel pro Schlüssel braucht keine Regel
    SWEEP_SECONDS = 60
    MAX_KEYS = 50_000

    def __init__(self, limit: int = 8, window: int = 900, clock=time.time):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = {}
        self._keep: dict[str, float] = {}      # wie lange die Einträge eines Schlüssels gebraucht werden
        self._clock = clock
        self._last_sweep = clock()
        self._lock = threading.Lock()

    # -- intern
    def _recent(self, key: str, window: float) -> list[float]:
        now = self._clock()
        return [t for t in self.failures.get(key, ()) if now - t < window]

    def _remember(self, key: str, window: float) -> None:
        if window > self._keep.get(key, 0):
            self._keep[key] = min(window, 86400)

    def _maybe_sweep(self) -> None:
        now = self._clock()
        if now - self._last_sweep >= self.SWEEP_SECONDS or len(self.failures) > self.MAX_KEYS:
            self.cleanup()

    def cleanup(self) -> int:
        """Abgelaufene Einträge entfernen. Gibt die Zahl der gelöschten Schlüssel zurück."""
        with self._lock:
            now = self._clock()
            self._last_sweep = now
            removed = 0
            for key in list(self.failures):
                keep = self._keep.get(key, self.window)
                stamps = [t for t in self.failures[key] if now - t < keep]
                if stamps:
                    self.failures[key] = stamps
                else:
                    del self.failures[key]
                    self._keep.pop(key, None)
                    removed += 1
            return removed

    # -- öffentlich
    def count(self, key: str, window: float | None = None) -> int:
        return len(self._recent(key, window or self.window))

    def blocked(self, key: str, limit: int | None = None, window: float | None = None) -> bool:
        self._maybe_sweep()
        window = window or self.window
        with self._lock:
            self._remember(key, window)
            return len(self._recent(key, window)) >= (limit or self.limit)

    def fail(self, key: str, window: float | None = None) -> None:
        self._maybe_sweep()
        with self._lock:
            self._remember(key, window or self.window)
            stamps = self.failures.setdefault(key, [])
            stamps.append(self._clock())
            if len(stamps) > self.MAX_PER_KEY:
                del stamps[: len(stamps) - self.MAX_PER_KEY]

    hit = fail   # «zählen» (z. B. gesendete SMS) – gleiche Mechanik

    def reset(self, key: str) -> None:
        with self._lock:
            self.failures.pop(key, None)
            self._keep.pop(key, None)

    def delay(self, key: str, *, free: int = 5, base: float = 30, cap: float = 900, window: float = 3600) -> int:
        """Sekunden bis zum nächsten erlaubten Versuch (0 = sofort). Die ersten «free» Fehlversuche innerhalb von
        «window» sind frei; danach 30 s, 1, 2, 4, 8 Minuten …, höchstens «cap» (15 Minuten)."""
        with self._lock:
            self._remember(key, window)
            stamps = self._recent(key, window)
        if len(stamps) < free:
            return 0
        wait = min(cap, base * 2 ** (len(stamps) - free))
        return max(0, math.ceil(stamps[-1] + wait - self._clock()))
