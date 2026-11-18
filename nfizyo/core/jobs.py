"""Hintergrundaufgaben.

Module melden Aufgaben-Arten an (register), legen Aufgaben in die Tabelle jobs
(enqueue) und ein einzelner Arbeiter arbeitet sie nacheinander ab. Rechenintensive
KI-Schritte laufen in einem Thread, damit die Oberfläche flüssig bleibt.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from aiohttp import web

from .db import Database, utcnow
from .tenant import practice_scope

log = logging.getLogger(__name__)

JobHandler = Callable[[web.Application, int, int], Awaitable[None]]  # (app, practice_id, ref_id)
MAX_ATTEMPTS = 3


class JobQueue:
    def __init__(self, db: Database):
        self.db = db
        self.handlers: dict[str, JobHandler] = {}
        self.background: set[str] = set()   # lange Arbeiten (z. B. Scans lesen) – kommen nach Diktaten und Übersetzungen
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def register(self, kind: str, handler: JobHandler, background: bool = False) -> None:
        self.handlers[kind] = handler
        if background:
            self.background.add(kind)

    def enqueue(self, practice_id: int, kind: str, ref_id: int) -> int:
        if kind not in self.handlers:
            raise KeyError(f"Unbekannte Aufgabe: {kind}")
        existing = self.db.value(
            "SELECT id FROM jobs WHERE practice_id = ? AND kind = ? AND ref_id = ? AND status IN ('queued','running')",
            (practice_id, kind, ref_id),
        )
        if existing:
            return existing
        job_id = self.db.execute(
            "INSERT INTO jobs (practice_id, kind, ref_id, created_at) VALUES (?,?,?,?)",
            (practice_id, kind, ref_id, utcnow()),
        )
        self._wake.set()
        return job_id

    def pending(self, practice_id: int, kind: str, ref_id: int) -> bool:
        return bool(
            self.db.value(
                "SELECT 1 FROM jobs WHERE practice_id = ? AND kind = ? AND ref_id = ? AND status IN ('queued','running')",
                (practice_id, kind, ref_id),
            )
        )

    async def run_once(self, app: web.Application, lane: str | None = None) -> bool:
        """Eine Aufgabe ausführen. True, wenn etwas zu tun war (für Tests nützlich).
        lane: "fg" = Arbeiten, auf die jemand wartet (Diktat, Übersetzung), "bg" = lange Hintergrundarbeiten (Scans lesen,
        Videos umwandeln) – zwei Arbeiter nebeneinander, damit ein 2-Minuten-Video nie ein Diktat aufhält. None = alle."""
        bg = sorted(self.background)
        marks = ",".join("?" * len(bg))
        if lane == "fg" and bg:
            sql, args = f"SELECT * FROM jobs WHERE status = 'queued' AND kind NOT IN ({marks}) ORDER BY id LIMIT 1", tuple(bg)
        elif lane == "bg":
            if not bg:
                return False
            sql, args = f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN ({marks}) ORDER BY id LIMIT 1", tuple(bg)
        else:
            # Wer wartet (Diktat, Nachricht übersetzen), kommt vor langen Hintergrundarbeiten (20 gescannte Seiten am Morgen)
            order = f"kind IN ({marks}), id" if bg else "id"
            sql, args = f"SELECT * FROM jobs WHERE status = 'queued' ORDER BY {order} LIMIT 1", tuple(bg)
        row = self.db.one(sql, args)
        if row is None:
            return False
        self.db.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
            (utcnow(), row["id"]),
        )
        handler = self.handlers.get(row["kind"])
        try:
            if handler is None:
                raise KeyError(row["kind"])
            with practice_scope(row["practice_id"]):
                await handler(app, row["practice_id"], row["ref_id"])
        except Exception as exc:  # noqa: BLE001 - Fehler wird gespeichert und angezeigt
            log.exception("Aufgabe %s #%s fehlgeschlagen", row["kind"], row["ref_id"])
            retry = row["attempts"] + 1 < MAX_ATTEMPTS
            self.db.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("queued" if retry else "failed", str(exc)[:500], utcnow(), row["id"]),
            )
        else:
            self.db.execute("UPDATE jobs SET status = 'done', finished_at = ? WHERE id = ?", (utcnow(), row["id"]))
        return True

    async def run_all(self, app: web.Application) -> int:
        n = 0
        while await self.run_once(app):
            n += 1
        return n

    async def _loop(self, app: web.Application, lane: str) -> None:
        wake = self._wakes[lane]
        while True:
            try:
                worked = await self.run_once(app, lane)
            except Exception:  # pragma: no cover - der Arbeiter darf nie sterben
                log.exception("Arbeiter-Fehler")
                worked = False
            if not worked:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

    async def start(self, app: web.Application) -> None:
        # Nach einem Neustart: abgebrochene Aufgaben wieder einreihen
        self.db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        self._wakes = {"fg": asyncio.Event(), "bg": asyncio.Event()}
        self._wake = _AnyWake(self._wakes)
        self._tasks = [asyncio.create_task(self._loop(app, lane)) for lane in ("fg", "bg")]
        self._task = self._tasks[0]

    async def stop(self, app: web.Application) -> None:
        for task in getattr(self, "_tasks", None) or ([self._task] if self._task else []):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


class _AnyWake:
    """enqueue() weckt beide Arbeiter (jeder nimmt nur seine Art von Aufgaben)."""

    def __init__(self, events: dict):
        self.events = events

    def set(self) -> None:
        for e in self.events.values():
            e.set()
