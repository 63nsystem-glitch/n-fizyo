"""SQLite-Zugriff und Migrationen.

Eine Verbindung pro Prozess (die Praxis-Box hat wenige gleichzeitige Nutzer).
Jedes Modul bringt seine eigenen SQL-Migrationen mit (migrations/NNN_name.sql).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        self._tx_depth = 0

    def close(self) -> None:
        self.conn.close()

    # --- Abfragen ---------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def value(self, sql: str, params: Sequence[Any] | dict = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None else row[0]

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid if cur.lastrowid is not None else cur.rowcount

    def changes(self, sql: str, params: Sequence[Any] | dict = ()) -> int:
        """UPDATE/DELETE: Zahl der geänderten Zeilen. (execute() liefert bei UPDATE die letzte Einfüge-ID der Verbindung,
        nicht die Zahl der Treffer – für «nur wenn noch …»-Prüfungen immer changes() nehmen.)"""
        with self._lock:
            return self.conn.execute(sql, params).rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)

    @contextmanager
    def tx(self) -> Iterator["Database"]:
        """Transaktion (verschachtelbar; nur die äusserste schreibt)."""
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    # --- Migrationen ------------------------------------------------------
    def migrate(self, sources: Iterable[tuple[str, Path]]) -> list[str]:
        """Wendet alle noch fehlenden Migrationen an. sources = [(modulname, migrations_dir), ...]."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " module TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, applied_at TEXT NOT NULL,"
            " PRIMARY KEY (module, version))"
        )
        applied: list[str] = []
        for module, directory in sources:
            if not directory.exists():
                continue
            done = {r[0] for r in self.query("SELECT version FROM schema_migrations WHERE module = ?", (module,))}
            for file in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(file.name[:3])
                if version in done:
                    continue
                sql = file.read_text(encoding="utf-8")
                with self._lock:
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in _split_sql(sql):
                            self.conn.execute(statement)
                        self.conn.execute(
                            "INSERT INTO schema_migrations (module, version, name, applied_at) VALUES (?,?,?,?)",
                            (module, version, file.name, utcnow()),
                        )
                        self.conn.execute("COMMIT")
                    except BaseException:
                        self.conn.execute("ROLLBACK")
                        raise
                applied.append(f"{module}/{file.name}")
        return applied


def _split_sql(sql: str) -> list[str]:
    """Teilt ein SQL-Skript in Anweisungen; Trigger (BEGIN ... END;) bleiben zusammen."""
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper.endswith("END;"):
                statements.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf and "\n".join(buf).strip():
        statements.append("\n".join(buf))
    return statements
