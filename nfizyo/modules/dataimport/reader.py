"""Tabellen lesen: CSV (Trennzeichen und Zeichensatz automatisch) und Excel (.xlsx). Nur Daten – nichts wird ausgeführt.

Rückgabe: Kopfzeile (Spaltennamen) und Zeilen als Text. Excel-Datumswerte werden zu JJJJ-MM-TT, Uhrzeiten zu HH:MM.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
import zlib
from datetime import date, datetime, time
from pathlib import Path

MAX_ROWS = 50_000
MAX_COLS = 80
# Grenzen für Excel-Dateien (xlsx = ZIP mit XML), geprüft BEVOR openpyxl liest – gegen «ZIP-Bomben» (wenige KB, entpackt GB):
MAX_UNZIPPED = 100 * 1024 * 1024       # alle Teile zusammen entpackt
MAX_MEMBERS = 500                      # Teile im ZIP
MAX_RATIO = 100                        # Verdichtung grösserer Teile (echte Tabellen: 5–20×)
MAX_CELL_CHARS = 32_767                # längster Zellinhalt (Grenze von Excel selbst)
MAX_CELLS = MAX_ROWS * MAX_COLS        # Zellen pro Tabelle


class ReadError(ValueError):
    pass


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):   # Excel «Unicode-Text» (.txt) ist UTF-16 mit Tabulatoren
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise ReadError("encoding") from exc
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ReadError("encoding")


def read_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = _decode(data)
    hint = re.match(r"\ufeff?sep=(.)[ \t]*\r?\n", text)   # Excel schreibt manchmal «sep=;» als erste Zeile
    if hint:
        delimiter, text = hint.group(1), text[hint.end():]
        return _split(list(csv.reader(io.StringIO(text), delimiter=delimiter)))
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return _split(rows)


def check_xlsx(data: bytes) -> None:
    """Excel-Datei vor dem Lesen prüfen – ohne sie ganz zu entpacken (Stück für Stück, wenig Speicher):
    Anzahl und entpackte Grösse der Teile, Verdichtung, längster Text zwischen zwei XML-Zeichen und Anzahl Zellen.
    Zu gross / verdächtig → ReadError("too_big"); kein gültiges ZIP → ReadError("xlsx")."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ReadError("xlsx") from exc
    if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise ReadError("too_big")
    for info in infos:
        if info.file_size > 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise ReadError("too_big")
        name = info.filename.lower()
        if name.endswith(".xml") and (name.startswith("xl/worksheets/") or name == "xl/sharedstrings.xml"):
            _scan_xml(zf, info)


_DELIM = re.compile(rb"[<>]")


def _scan_xml(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    run = 0          # Länge des Texts seit dem letzten < oder > (über Stückgrenzen hinweg)
    cells = 0
    tail = b""
    try:
        with zf.open(info) as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                cells += (tail + chunk[:2]).count(b"<c ") + chunk.count(b"<c ") + chunk.count(b"<c>")
                tail = chunk[-2:]
                pieces = _DELIM.split(chunk)
                if len(pieces) == 1:
                    run += len(chunk)
                else:
                    if run + len(pieces[0]) > MAX_CELL_CHARS or max(map(len, pieces[1:-1]), default=0) > MAX_CELL_CHARS:
                        raise ReadError("too_big")
                    run = len(pieces[-1])
                if run > MAX_CELL_CHARS or cells > MAX_CELLS:
                    raise ReadError("too_big")
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as exc:
        raise ReadError("xlsx") from exc


def read_xlsx(data: bytes) -> tuple[list[str], list[list[str]]]:
    from openpyxl import load_workbook

    check_xlsx(data)
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - beschädigte Datei
        raise ReadError("xlsx") from exc
    try:
        ws = wb.worksheets[0]
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i > MAX_ROWS + 1:
                break
            rows.append([_cell(v)[:MAX_CELL_CHARS] for v in row[:MAX_COLS]])
    except ReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - beschädigte Datei
        raise ReadError("xlsx") from exc
    finally:
        wb.close()
    return _split(rows)


def _split(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    rows = [[str(c).strip() for c in r[:MAX_COLS]] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ReadError("empty")
    header = rows[0]
    width = max(len(r) for r in rows)
    header = [h or f"Spalte {i + 1}" for i, h in enumerate(header + [""] * (width - len(header)))]
    body = [r + [""] * (width - len(r)) for r in rows[1:]]
    if len(body) > MAX_ROWS:
        raise ReadError("too_many")
    return header, body


def read(path: Path) -> tuple[list[str], list[list[str]]]:
    data = path.read_bytes()
    if path.suffix.lower() == ".xlsx":
        return read_xlsx(data)
    return read_csv(data)
