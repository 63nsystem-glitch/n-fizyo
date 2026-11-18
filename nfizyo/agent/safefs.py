"""Dateien in Ordnern, die dem App-Benutzer gehören – sicher für den Runner (root).

Der Ordner /var/lib/n-fizyo/runner/ (spool, local, results, events, status.json) gehört dem App-Benutzer: die App legt dort
Aufträge ab und löscht Ergebnisse/Ereignisse nach dem Einlesen. Der Runner läuft als root und muss annehmen, dass die App
kompromittiert sein kann. Darum gilt hier:
- Jeder Pfadteil unterhalb des Datenordners wird mit O_NOFOLLOW geöffnet (über Ordner-Deskriptoren, kein Pfad-Text):
  ein Symlink (z. B. runner → /etc) führt zu einem Fehler statt zu einer fremden Datei.
- Schreiben nur in eine NEUE Datei (O_CREAT|O_EXCL|O_NOFOLLOW, zufälliger Name), chown nur über den eigenen Deskriptor
  (fchown), danach rename im selben Ordner – ein Symlink am Ziel wird ersetzt, nie verfolgt.
- Lesen nur regulärer Dateien (fstat), mit Grössenlimit, ebenfalls mit O_NOFOLLOW.
- Dateinamen aus Aufträgen (Befehls-IDs) nur nach SAFE_ID (keine «/», kein «..»).
"""
from __future__ import annotations

import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}")
MAX_READ = 256 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(OSError):
    """Pfad zeigt auf etwas anderes als einen echten Ordner / eine echte Datei (z. B. Symlink)."""


def valid_id(value: str) -> bool:
    return bool(SAFE_ID.fullmatch(value or ""))


def _owner(user: str | None) -> tuple[int, int] | None:
    if not user:
        return None
    try:
        import pwd

        pw = pwd.getpwnam(user)
        return pw.pw_uid, pw.pw_gid
    except (KeyError, ImportError):
        return None


def _open_base(base: Path, create: bool) -> int:
    """Datenordner selbst: Pfad kommt aus der .env (root), sein Elternordner gehört root – darf normal geöffnet werden."""
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


@contextmanager
def open_dir(base: Path, *parts: str, create: bool = False, owner: str | None = None) -> Iterator[int]:
    """Ordner-Deskriptor für base/part1/part2… – jeder Teil ohne Symlinks. create: fehlende Teile anlegen (0750, Besitzer
    owner, damit die App darin weiterhin löschen kann). UnsafePath, wenn ein Teil kein echter Ordner ist."""
    fd = _open_base(base, create)
    try:
        for part in parts:
            if not SAFE_NAME.fullmatch(part):
                raise UnsafePath(f"ungültiger Name: {part!r}")
            if create:
                try:
                    os.mkdir(part, 0o750, dir_fd=fd)
                    created = True
                except FileExistsError:
                    created = False
            else:
                created = False
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise
                raise UnsafePath(f"{part}: kein echter Ordner") from exc
            if created:
                ids = _owner(owner)
                if ids:
                    try:
                        os.fchown(nfd, *ids)
                    except OSError:
                        pass
            os.close(fd)
            fd = nfd
        yield fd
    finally:
        os.close(fd)


def write_json_atomic(base: Path, parts: tuple[str, ...], name: str, text: str, owner: str | None = None) -> None:
    """Neue Datei im Ordner base/parts… schreiben und atomar nach «name» umbenennen. Nie einem Symlink folgen."""
    if not SAFE_NAME.fullmatch(name):
        raise UnsafePath(f"ungültiger Name: {name!r}")
    data = text.encode("utf-8")
    with open_dir(base, *parts, create=True, owner=owner) as dfd:
        tmp = f".{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
        try:
            try:
                ids = _owner(owner)
                if ids:
                    try:
                        os.fchown(fd, *ids)
                    except OSError:
                        pass
                os.fchmod(fd, 0o644)            # wie bisher lesbar für die App (Ordner selbst ist 0750)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise


def list_json(base: Path, *parts: str) -> list[str]:
    """Namen der *.json-Dateien im Ordner (ohne Symlink-Ordner). Fehlt der Ordner → []."""
    try:
        with open_dir(base, *parts) as dfd:
            return sorted(n for n in os.listdir(dfd) if n.endswith(".json") and not n.startswith(".") and SAFE_NAME.fullmatch(n))
    except FileNotFoundError:
        return []


def read_and_remove(base: Path, parts: tuple[str, ...], name: str, limit: int = MAX_READ) -> bytes | None:
    """Reguläre Datei lesen (nie über einen Symlink, höchstens limit Bytes) und danach löschen.
    None = kein lesbarer Auftrag (Symlink, Ordner, zu gross …) – der Eintrag wird trotzdem entfernt."""
    if not SAFE_NAME.fullmatch(name):
        return None
    with open_dir(base, *parts) as dfd:
        data = None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        except OSError:
            fd = None
        if fd is not None:
            try:
                st = os.fstat(fd)
                if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= limit:   # keine Hardlinks auf fremde Dateien
                    chunks, total = [], 0
                    while total <= limit:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    data = b"".join(chunks) if total <= limit else None
            finally:
                os.close(fd)
        try:
            os.unlink(name, dir_fd=dfd)          # entfernt einen Symlink selbst, nie sein Ziel
        except IsADirectoryError:
            pass
        except OSError:
            pass
        return data
