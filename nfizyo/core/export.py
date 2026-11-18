"""Vollständiger Datenexport einer Praxis (Leitung): alle Tabellen der eigenen Praxis als CSV + Dokumente, in einer ZIP-Datei.

- Nur Zeilen der eigenen Praxis (practice_id), nie fremde Daten.
- Ohne Geheimnisse: Passwort-Hashes, Zwei-Schritt-Schlüssel, Wiederherstellungscodes, Code-/Token-Hashes, E-Mail-Anhänge.
- Ohne Tonaufnahmen (Diktat) – die sind nach der Freigabe ohnehin gelöscht oder bleiben nur auf Wunsch der Praxis.
- Die Datei liegt höchstens 24 Stunden auf der Box (data/exports/<praxis>/) und wird dann gelöscht.
Gedacht für: Wechsel der Software, Herausgabe an die Praxis (Datenportabilität), Archiv.
"""
from __future__ import annotations

import csv
import io
import secrets
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .db import Database
from .spreadsheet import safe_cell

SKIP_TABLES = {"sessions", "schema_migrations", "module_versions", "box_commands", "box_state", "patient_login_codes"}
SKIP_COLUMNS = {"password_hash", "totp_secret", "totp_pending", "totp_last_step", "recovery_codes", "token_hash", "code_hash",
                "attachment", "box_key", "secret", "refresh_token_enc", "pending_state",
                "body_secret"}   # SMS-Text mit Code/Link vor dem Versand (outbox.body ist schon maskiert)
KEEP_HOURS = 24


def export_dir(settings, practice_id: int) -> Path:
    return settings.data_dir / "exports" / str(int(practice_id))


def _tables(db: Database) -> list[str]:
    out = []
    for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = r["name"]
        if name in SKIP_TABLES:
            continue
        cols = {c["name"] for c in db.query(f"PRAGMA table_info({name})")}
        if "practice_id" in cols or name == "practices":
            out.append(name)
    return out


def _csv(db: Database, table: str, practice_id: int) -> tuple[bytes, int]:
    cols = [c["name"] for c in db.query(f"PRAGMA table_info({table})") if c["name"] not in SKIP_COLUMNS]
    where = "id = ?" if table == "practices" else "practice_id = ?"
    rows = db.query(f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY rowid", (practice_id,))  # noqa: S608 - Namen aus sqlite_master
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(cols)
    for row in rows:
        # Text mit = + - @ am Anfang bekommt ein ' davor (sonst Formel in Excel); Zahlen bleiben Zahlen
        w.writerow(["" if v is None else (v.hex() if isinstance(v, (bytes, bytearray)) else safe_cell(v)) for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8"), len(rows)


def build(db: Database, settings, practice_id: int) -> Path:
    """ZIP erzeugen. Gibt den Pfad zurück (Name mit Zufallsteil)."""
    target_dir = export_dir(settings, practice_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"n-fizyo-export-{stamp}-{secrets.token_hex(6)}.zip"
    practice = db.one("SELECT name, slug FROM practices WHERE id = ?", (practice_id,))
    docs_root = settings.documents_dir.resolve()
    counts: dict[str, int] = {}
    missing_docs = 0
    tmp = path.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for table in _tables(db):
            data, n = _csv(db, table, practice_id)
            counts[table] = n
            zf.writestr(f"tabellen/{table}.csv", data)
        for d in db.query("SELECT id, filename, path FROM documents WHERE practice_id = ?", (practice_id,)) if "documents" in counts else []:
            try:
                p = Path(d["path"]).resolve()
                if docs_root not in p.parents or not p.is_file():
                    raise FileNotFoundError
                zf.write(p, f"dokumente/{d['id']}_{Path(d['filename']).name}")
            except (OSError, FileNotFoundError):
                missing_docs += 1
        videos = settings.data_dir / "videos" / str(int(practice_id))   # eigene Übungsvideos (nur diese Praxis)
        n_videos = 0
        if videos.is_dir():
            for f in sorted(videos.iterdir()):
                if f.is_file() and not f.is_symlink():
                    zf.write(f, f"videos/{f.name}")
                    n_videos += 1
        readme = [f"N-Fizyo – Datenexport der Praxis «{practice['name']}» ({practice['slug']})",
                  f"Erstellt: {datetime.now(timezone.utc).isoformat(timespec='seconds')} (UTC)", "",
                  "tabellen/*.csv  – eine Datei pro Tabelle (Trennzeichen ;, UTF-8). Spalte practice_id = diese Praxis.",
                  "dokumente/      – Dateien aus dem Posteingang (Berichte, Scans), Name = <Dokument-Nr>_<Dateiname>.",
                  f"videos/         – eigene Übungsvideos der Patient/innen ({n_videos}); Zuordnung: Tabelle program_items, Spalte video_path.",
                  "Nicht enthalten: Passwörter und Schlüssel (nur als Hash gespeichert), Sitzungen, Tonaufnahmen,",
                  "  SMS-Codes und Zugangslinks (im Postausgang unkenntlich: ••••••).",
                  "Text, der mit = + - @ beginnt, steht mit einem ' davor (Schutz vor Formeln in Excel).",
                  f"Fehlende Dokumentdateien: {missing_docs}", "", "Anzahl Zeilen:"] + [f"  {t}: {n}" for t, n in sorted(counts.items())]
        zf.writestr("LIESMICH.txt", "\r\n".join(readme))
    tmp.rename(path)
    return path


# --------------------------------------------------------------------------- Datenauskunft für eine Person (Art. 25 DSG)
PATIENT_SKIP_TABLES = {"audit", "audit_log"}     # internes Sicherheitsprotokoll (wer hat wann geöffnet) – keine Daten der Person selbst


def _cols(db: Database, table: str) -> list[str]:
    return [c["name"] for c in db.query(f"PRAGMA table_info({table})")]


def _rows_csv(cols: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(cols)
    for row in rows:
        w.writerow(["" if v is None else (v.hex() if isinstance(v, (bytes, bytearray)) else safe_cell(v)) for v in row])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def build_patient(db: Database, settings, practice_id: int, patient_id: int) -> tuple[bytes, dict[str, int]]:
    """Alle Daten EINER Person als ZIP (im Speicher, nichts bleibt auf dem Server): jede Tabelle mit ihren Zeilen als CSV
    (direkt über patient_id oder eine Stufe darunter über einen Fremdschlüssel, z. B. Rechnungszeilen), ihre Dokumente
    und eigenen Übungsvideos. Ohne Geheimnisse (wie der Praxis-Export). Nur Zeilen der eigenen Praxis."""
    patient = db.one("SELECT * FROM patients WHERE id = ? AND practice_id = ?", (patient_id, practice_id))
    if patient is None:
        raise LookupError("patient")
    tables = [t for t in _tables(db) if t not in PATIENT_SKIP_TABLES and t != "practices"]
    picked: dict[str, list] = {}
    ids: dict[str, list[int]] = {}
    for t in tables:
        cols = _cols(db, t)
        if t == "patients":
            where, params = "practice_id = ? AND id = ?", (practice_id, patient_id)
        elif "patient_id" in cols:
            where, params = "practice_id = ? AND patient_id = ?", (practice_id, patient_id)
        else:
            continue
        keep = [c for c in cols if c not in SKIP_COLUMNS]
        rows = db.query(f"SELECT {', '.join(keep)} FROM {t} WHERE {where} ORDER BY rowid", params)  # noqa: S608 - Namen aus sqlite_master
        if rows:
            picked[t] = [keep, rows]
            if "id" in keep:
                ids[t] = [r["id"] for r in rows]
    for t in tables:                                   # eine Stufe darunter: Zeilen, die auf eine Zeile oben verweisen
        if t in picked:
            continue
        for fk in db.query(f"PRAGMA foreign_key_list({t})"):
            parent, col = fk["table"], fk["from"]
            if parent not in ids or parent == "patients":
                continue
            keep = [c for c in _cols(db, t) if c not in SKIP_COLUMNS]
            found = []
            for k in range(0, len(ids[parent]), 500):
                part = ids[parent][k:k + 500]
                found += db.query(f"SELECT {', '.join(keep)} FROM {t} WHERE practice_id = ? AND {col} IN ({','.join('?' * len(part))})"  # noqa: S608
                                  " ORDER BY rowid", [practice_id, *part])
            if found:
                picked[t] = [keep, found]
            break
    counts = {t: len(v[1]) for t, v in picked.items()}
    buf = io.BytesIO()
    docs_root = settings.documents_dir.resolve()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for t, (cols, rows) in sorted(picked.items()):
            zf.writestr(f"tabellen/{t}.csv", _rows_csv(cols, rows))
        n_docs = 0
        if "documents" in picked:
            for d in db.query("SELECT id, filename, path FROM documents WHERE practice_id = ? AND patient_id = ?", (practice_id, patient_id)):
                try:
                    f = Path(d["path"]).resolve()
                    if docs_root in f.parents and f.is_file():
                        zf.write(f, f"dokumente/{d['id']}_{Path(d['filename']).name}")
                        n_docs += 1
                except OSError:
                    pass
        n_videos = 0
        vids = settings.data_dir / "videos" / str(int(practice_id))
        if "program_items" in picked and vids.is_dir():
            cols, rows = picked["program_items"]
            names = {Path(str(r["video_path"])).name for r in rows if "video_path" in cols and r["video_path"]}
            for name in sorted(names):
                f = vids / name
                if f.is_file() and not f.is_symlink():
                    zf.write(f, f"videos/{name}")
                    n_videos += 1
        readme = [
            f"N-Fizyo – Datenauskunft für {patient['first_name']} {patient['last_name']} ({patient['number']})",
            f"Erstellt: {datetime.now(timezone.utc).isoformat(timespec='seconds')} (UTC)", "",
            "Diese Datei enthält alle Personendaten, die die Praxis in N-Fizyo zu dieser Person bearbeitet (Art. 25 DSG):",
            "tabellen/*.csv  – eine Datei pro Bereich (Trennzeichen ;, UTF-8), z. B. patients = Stammdaten, appointments = Termine,",
            "                  notes = Behandlungsnotizen, invoices / invoice_lines = Rechnungen, messages = Nachrichten.",
            f"dokumente/      – Berichte und Scans ({n_docs}).   videos/ – eigene Übungsvideos ({n_videos}).",
            "Zweck: Behandlung, Terminplanung, Abrechnung. Empfänger: Versicherer (elektronische Rechnung über MediData),",
            "zuweisende Ärztinnen und Ärzte (Berichte über HIN), SMS-Anbieter (nur Terminhinweise). Aufbewahrung: nach kantonalem Recht.",
            "Nicht enthalten: Passwörter und Schlüssel, Zugangscodes, das interne Zugriffsprotokoll.", "",
            "Contenu (FR) : toutes les données personnelles traitées par le cabinet dans N-Fizyo pour cette personne (art. 25 LPD).", "",
            "Anzahl Zeilen:"] + [f"  {t}: {n}" for t, n in sorted(counts.items())]
        zf.writestr("LIESMICH.txt", "\r\n".join(readme))
    return buf.getvalue(), counts


def list_exports(settings, practice_id: int) -> list[dict]:
    d = export_dir(settings, practice_id)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("n-fizyo-export-*.zip"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "created": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    "expires": datetime.fromtimestamp(st.st_mtime + KEEP_HOURS * 3600, tz=timezone.utc).isoformat()})
    return out


def file_for(settings, practice_id: int, name: str) -> Path | None:
    if not name.startswith("n-fizyo-export-") or "/" in name or "\\" in name or not name.endswith(".zip"):
        return None
    p = export_dir(settings, practice_id) / name
    return p if p.is_file() else None


def cleanup(settings, max_age_hours: int = KEEP_HOURS) -> int:
    """Exporte älter als 24 Stunden löschen (alle Praxen)."""
    root = settings.data_dir / "exports"
    n = 0
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    for p in list(root.glob("*/n-fizyo-export-*")):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n
