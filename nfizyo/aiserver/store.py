"""Zugänge der Praxen und Nutzungszahlen (ohne Inhalte)."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    op TEXT NOT NULL,
    ok INTEGER NOT NULL,
    ms INTEGER NOT NULL,
    bytes_in INTEGER NOT NULL DEFAULT 0,
    audio_sec REAL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_client_at ON usage(client_id, at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def add_client(self, name: str) -> tuple[int, str]:
        token = "nfa_" + secrets.token_urlsafe(32)
        with self._lock:
            cur = self.conn.execute("INSERT INTO clients (name, token_hash, created_at) VALUES (?,?,?)", (name, token_hash(token), _now()))
        return int(cur.lastrowid), token

    def client_for(self, token: str):
        if not token:
            return None
        with self._lock:
            return self.conn.execute("SELECT * FROM clients WHERE token_hash = ? AND active = 1", (token_hash(token),)).fetchone()

    def set_active(self, client_id: int, active: bool) -> bool:
        with self._lock:
            cur = self.conn.execute("UPDATE clients SET active = ? WHERE id = ?", (1 if active else 0, client_id))
        return cur.rowcount > 0

    def clients(self):
        with self._lock:
            return self.conn.execute("SELECT id, name, active, created_at FROM clients ORDER BY id").fetchall()

    def record(self, client_id: int, op: str, ok: bool, ms: int, bytes_in: int = 0, audio_sec: float | None = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO usage (client_id, op, ok, ms, bytes_in, audio_sec, at) VALUES (?,?,?,?,?,?,?)",
                              (client_id, op, 1 if ok else 0, ms, bytes_in, audio_sec, _now()))

    def usage(self, since: str):
        with self._lock:
            return self.conn.execute(
                "SELECT c.name, u.op, COUNT(*) AS n, SUM(u.ms) AS ms, ROUND(SUM(COALESCE(u.audio_sec, 0)) / 60.0, 1) AS audio_min"
                " FROM usage u JOIN clients c ON c.id = u.client_id WHERE u.at >= ? GROUP BY c.name, u.op ORDER BY c.name, u.op",
                (since,)).fetchall()
