"""Verfügbare Versionen für die NSystem-Übersicht: index.json aus der Release-Ablage, Signatur geprüft (agent/release.py).

Quelle: NFIZYO_CENTRAL_RELEASE_DIR (Ordner auf diesem Server) oder NFIZYO_CENTRAL_RELEASE_BASE (https). Ohne gültige Signatur
(NFIZYO_CENTRAL_RELEASE_PUBKEY) wird keine Version angeboten. Ergebnis 5 Minuten zwischengespeichert.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from ..agent import release

CACHE_SECONDS = 300
_cache: dict[str, tuple[float, list[dict], str | None]] = {}


def _read(settings) -> dict:
    if settings.release_dir:
        return json.loads((Path(settings.release_dir) / "index.json").read_text(encoding="utf-8"))
    if not settings.release_base.startswith("https://"):
        raise release.ReleaseError("keine Release-Ablage eingerichtet")
    req = urllib.request.Request(f"{settings.release_base}/index.json", headers={"User-Agent": "nsystem-uebersicht"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - nur https (oben geprüft)
        return json.loads(resp.read(2_000_000))


def available(settings, *, fresh: bool = False) -> tuple[list[dict], str | None]:
    """(Versionen neueste zuerst, Fehlertext). Jede Version ist ein geprüftes Manifest."""
    if not settings.release_pubkey:
        return [], "NFIZYO_CENTRAL_RELEASE_PUBKEY fehlt" if (settings.release_dir or settings.release_base) else None
    if not (settings.release_dir or settings.release_base):
        return [], None
    key = f"{settings.release_dir}|{settings.release_base}|{settings.release_pubkey}"
    hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    try:
        items, err = release.verify_index(settings.release_pubkey, _read(settings)), None
    except (release.ReleaseError, OSError, ValueError) as exc:
        items, err = [], f"Release-Index: {exc}"
    _cache[key] = (time.time(), items, err)
    return items, err


def find(settings, version: str) -> dict | None:
    items, _ = available(settings)
    return next((m for m in items if m["version"] == version), None)


def clear_cache() -> None:
    _cache.clear()
