"""Kern: NSystem-Anbindung der Box – feste Liste erlaubter Befehle, Zustand der Verbindung.

Feste Liste der Befehle, die NSystem auf der Box auslösen darf. Alles andere wird abgelehnt.

Bewusst NICHT auf der Liste: Passwort zurücksetzen, Daten lesen/exportieren, Protokolle mit Inhalten, beliebige Befehle –
und «Supportzugang öffnen»: das kann nur die Leitung der Praxis (Einstellungen › NSystem, core/nsystem.py). NSystem darf
einen offenen Supportzugang nur früher SCHLIESSEN.
"""
from __future__ import annotations

ACTIONS = {
    "diagnose": "Zustand prüfen (Dienste, Speicher, Grafikkarte, Versionen)",
    "backup_now": "Sicherung jetzt erstellen",
    "restart_app": "N-Fizyo neu starten",
    "restart_ai": "KI-Dienst (Ollama) neu starten",
    "update": "Neue Version einspielen (mit Sicherung und automatischer Rückkehr)",
    "rollback": "Zur vorherigen Version zurück",
    "support_close": "Supportzugang schliessen (öffnen kann nur die Leitung der Praxis)",
}
ACTIONS_FR = {
    "diagnose": "Contrôler l'état (services, mémoire, carte graphique, versions)",
    "backup_now": "Créer une sauvegarde maintenant",
    "restart_app": "Redémarrer N-Fizyo",
    "restart_ai": "Redémarrer le service IA (Ollama)",
    "update": "Installer une nouvelle version (avec sauvegarde et retour automatique)",
    "rollback": "Revenir à la version précédente",
    "support_close": "Fermer l'accès support (seule la direction du cabinet peut l'ouvrir)",
}


def valid_args(action: str, args: dict) -> bool:
    if action not in ACTIONS:
        return False
    if action == "update":
        import re

        name, sha = str(args.get("file") or ""), str(args.get("sha256") or "")
        if set(args) - {"file", "sha256", "version"}:
            return False
        if "version" in args and not re.fullmatch(r"[0-9][0-9A-Za-z.\-]{0,30}", str(args["version"])):
            return False
        return bool(re.fullmatch(r"n-fizyo-[0-9][0-9A-Za-z.\-]{0,30}\.tar\.gz", name) and re.fullmatch(r"[0-9a-f]{64}", sha))
    return not args


def configured(settings) -> bool:
    return bool(settings.central_url and settings.central_box_id and settings.central_box_key)


def set_state(db, key: str, value: str | None) -> None:
    from .db import utcnow

    db.execute("INSERT INTO box_state (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
               " updated_at = excluded.updated_at", (key, value, utcnow()))


def get_state(db, key: str):
    return db.one("SELECT value, updated_at FROM box_state WHERE key = ?", (key,))


def commands(db, limit: int = 50):
    return db.query("SELECT * FROM box_commands ORDER BY received_at DESC LIMIT ?", (limit,))
