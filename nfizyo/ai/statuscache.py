"""Zwischengespeicherter Status eines entfernten KI-Dienstes (KI-Server oder – nur Vorführung – Cloud-Dienst).

Der Webserver der Praxis wartet nie auf das Netz: ist der gespeicherte Wert veraltet, holt ihn ein Hintergrund-Thread,
und bis dahin gilt der letzte bekannte Wert. Sonst stünde bei hängendem KI-Server die ganze Praxis-Software
(Agenda, Kasse, Patienten-App) jedes Mal einige Sekunden still. In Hintergrundaufgaben (Thread) wird normal gewartet.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class CachedStatus:
    def __init__(self, fetch: Callable[[], dict], unknown: dict):
        self._fetch = fetch
        self._unknown = unknown
        self._value: tuple[float, dict] | None = None
        self._lock = threading.Lock()      # schützt nur _value (nie während einer Netzabfrage gehalten)
        self._busy = threading.Lock()      # höchstens eine Abfrage gleichzeitig

    def _fresh(self, max_age: float) -> dict | None:
        with self._lock:
            cur = self._value
        if cur and time.monotonic() - cur[0] < max_age:
            return cur[1]
        return None

    def _store(self, value: dict) -> dict:
        with self._lock:
            self._value = (time.monotonic(), value)
        return value

    def get(self, max_age: float) -> dict:
        hit = self._fresh(max_age)
        if hit is not None:
            return hit
        if on_event_loop():
            if self._busy.acquire(blocking=False):
                threading.Thread(target=self._refresh_background, name="nfizyo-ki-status", daemon=True).start()
            with self._lock:
                cur = self._value
            return cur[1] if cur else {**self._unknown, "checking": True}
        with self._busy:
            hit = self._fresh(max_age)   # ein anderer Thread hat inzwischen nachgesehen
            if hit is not None:
                return hit
            return self._store(self._fetch())

    def _refresh_background(self) -> None:
        try:
            self._store(self._fetch())
        finally:
            self._busy.release()

    def set(self, value: dict) -> None:
        self._store(value)

    def forget(self) -> None:
        with self._lock:
            self._value = None
