"""Posteingang: Dokumente speichern, auswerten, Vorschläge übernehmen."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from markupsafe import Markup, escape

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...core import patients as core_patients
from ..patients import api as patients_api

ALLOWED = {".pdf": "application/pdf", ".txt": "text/plain", ".png": "image/png", ".jpg": "image/jpeg",
           ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp"}
FINDING_KINDS = ("diagnosis", "surgery", "finding", "restriction", "clearance", "complication", "red_flag")


def store_file(db: Database, base_dir: Path, filename: str, data: bytes, *, source: str, patient_id: int | None = None,
               uploaded_by: int | None = None) -> tuple[int, bool]:
    """Datei ablegen. Rückgabe (document_id, neu?). Gleiche Datei zweimal → kein Duplikat."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("type")
    sha = hashlib.sha256(data).hexdigest()
    pid = current_practice_id()
    existing = db.one("SELECT id FROM documents WHERE practice_id = ? AND sha256 = ?", (pid, sha))
    if existing:
        return existing["id"], False
    target_dir = base_dir / str(pid)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{sha}{suffix}"
    path.write_bytes(data)
    did = db.execute(
        "INSERT INTO documents (practice_id, patient_id, filename, path, mime, size, sha256, source, uploaded_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, patient_id, Path(filename).name[:200], str(path), ALLOWED[suffix], len(data), sha, source,
         uploaded_by if uploaded_by is not None else current_user_id(), utcnow()),
    )
    audit(db, "create", "document", did, patient_id, {"source": source})
    return did, True


def set_origin(db: Database, did: int, sender: str | None, title: str | None) -> None:
    """Absender und Betreff (z. B. aus einer HIN-Mail) – nur wenn noch leer."""
    db.execute("UPDATE documents SET sender = COALESCE(sender, ?), title = COALESCE(title, ?) WHERE id = ? AND practice_id = ?",
               ((sender or "")[:200] or None, (title or "")[:200] or None, did, current_practice_id()))


def get(db: Database, did: int):
    return db.one(
        "SELECT d.*, p.first_name, p.last_name, p.birthdate AS patient_birthdate FROM documents d"
        " LEFT JOIN patients p ON p.id = d.patient_id WHERE d.id = ? AND d.practice_id = ?", (did, current_practice_id()))


OPEN = "('new', 'processing', 'review', 'failed')"


def listing(db: Database, status: str | None = None, patient_id: int | None = None, limit: int = 200, *,
            visible_to: tuple[int, tuple[str, list]] | None = None, unassigned: bool = False):
    """Dokumente. visible_to=(user_id, patient_ids_sql): nur Dokumente eigener Patient/innen oder selbst hochgeladen ohne Zuordnung.
    unassigned=True: nur Dokumente ohne Patient/in (Empfang: zuordnen)."""
    sql = ("SELECT d.*, p.first_name, p.last_name, p.birthdate AS patient_birthdate,"
           " (SELECT COUNT(*) FROM suggestions s WHERE s.document_id = d.id AND s.status = 'open') AS open_count"
           " FROM documents d LEFT JOIN patients p ON p.id = d.patient_id WHERE d.practice_id = ?")
    params: list = [current_practice_id()]
    if status == "open":
        sql += f" AND d.status IN {OPEN}"
    elif status:
        sql += " AND d.status = ?"
        params.append(status)
    if patient_id:
        sql += " AND d.patient_id = ?"
        params.append(patient_id)
    if unassigned:
        sql += " AND d.patient_id IS NULL"
    if visible_to:
        uid, (ids_sql, ids_params) = visible_to
        sql += f" AND (d.patient_id IN ({ids_sql}) OR (d.patient_id IS NULL AND d.uploaded_by = ?))"
        params += [*ids_params, uid]
    return db.query(sql + " ORDER BY d.id DESC LIMIT ?", params + [limit])


def count_open(db: Database, *, visible_to=None, unassigned: bool = False) -> int:
    return len(listing(db, "open", limit=1000, visible_to=visible_to, unassigned=unassigned))


def suggestion(db: Database, sid: int):
    return db.one("SELECT * FROM suggestions WHERE id = ? AND practice_id = ?", (sid, current_practice_id()))


def suggestions(db: Database, did: int):
    return db.query("SELECT * FROM suggestions WHERE practice_id = ? AND document_id = ? ORDER BY id", (current_practice_id(), did))


def open_for_patient(db: Database, patient_id: int):
    return db.query(
        "SELECT s.*, d.title AS doc_title, d.kind AS doc_kind, d.sender, d.created_at AS doc_created, d.source AS doc_source FROM suggestions s"
        " JOIN documents d ON d.id = s.document_id WHERE s.practice_id = ? AND s.patient_id = ? AND s.status = 'open'"
        " ORDER BY s.document_id DESC, s.id", (current_practice_id(), patient_id))


def save_extraction(db: Database, did: int, text: str, method: str, result, matches: list) -> None:
    pid = current_practice_id()
    patient_id = matches[0]["id"] if len(matches) == 1 else None
    doc = db.one("SELECT patient_id, kind FROM documents WHERE id = ? AND practice_id = ?", (did, pid))
    patient_id = doc["patient_id"] or patient_id
    kinds = {s.kind for s in result.suggestions}
    kind = doc["kind"]
    if kind == "report":
        kind = "surgery" if "surgery" in kinds and any(w in text[:400].lower() for w in ("op-bericht", "operationsbericht", "austritt", "protocole opératoire")) else (
            "prescription" if "prescription" in kinds and len(kinds) <= 3 else "report")
    with db.tx():
        db.execute(
            "UPDATE documents SET text = ?, text_method = ?, status = 'review', language = ?, doc_date = ?, extracted_name = ?,"
            " extracted_birthdate = ?, match_ids = ?, engine = ?, warnings = ?, patient_id = ?, kind = ?, processed_at = ?,"
            " title = COALESCE(title, ?), sender = COALESCE(sender, ?) WHERE id = ? AND practice_id = ?",
            (text, method, result.language, result.document_date.isoformat() if result.document_date else None,
             result.patient_name, result.patient_birthdate.isoformat() if result.patient_birthdate else None,
             ",".join(str(m["id"]) for m in matches), result.engine, json.dumps(result.warnings), patient_id, kind, utcnow(),
             guess_title(text), guess_sender(text), did, pid),
        )
        db.execute("DELETE FROM suggestions WHERE document_id = ? AND practice_id = ? AND status = 'open'", (did, pid))
        existing = patients_repo_findings(db, patient_id)
        for s in result.suggestions:
            status = "duplicate" if _is_duplicate(s, existing) else "open"
            db.execute(
                "INSERT INTO suggestions (practice_id, document_id, patient_id, kind, text, excerpt, structure_id, region, side,"
                " spine_levels, on_date, until, sessions, insurer, law, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, did, patient_id, s.kind, s.text, s.excerpt, s.structure_id, s.region, s.side, ",".join(s.levels) or None,
                 s.on_date.isoformat() if s.on_date else None, s.until.isoformat() if s.until else None, s.sessions,
                 s.insurer, s.law, status, utcnow()),
            )


def patients_repo_findings(db: Database, patient_id: int | None):
    if not patient_id:
        return []
    return patients_api.findings(db, patient_id)


def _is_duplicate(s, findings) -> bool:
    """Steht das schon in der Akte? (gleiche Art, Struktur, Seite und Datum)"""
    for f in findings:
        if f["kind"] != s.kind or not s.structure_id:
            continue
        if f["structure_id"] != s.structure_id or (f["side"] or None) != (s.side or None):
            continue
        if s.kind in ("diagnosis", "surgery"):
            return True
        if s.kind == "restriction" and s.until and f["until"] == s.until.isoformat():
            return True
    return False


def guess_title(text: str) -> str | None:
    for line in text.splitlines()[:25]:
        l = line.strip()
        if re.fullmatch(r"(?i)((austrittsbericht|kontrollbericht|op-?bericht|operationsbericht)( und \w+)?|zuweisung[\w ]*|"
                        r"verordnung[\w ]*|rapport[\w ']*|lettre de sortie|ordonnance[\w ]*|bericht[\w ]*|mri[\w ]*|irm[\w ]*)", l):
            return l[:120]
    return None


def scan_status(db: Database) -> dict:
    """Stand des Scan-Ordners für den Empfang (ohne Server-Pfad): letzter Scan und Scans heute."""
    from ...core.timeutil import day_bounds_utc, today

    lo, hi = day_bounds_utc(today())
    row = db.one("SELECT MAX(created_at) AS last, SUM(CASE WHEN created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) AS today"
                 " FROM documents WHERE practice_id = ? AND source = 'scan'", (lo, hi, current_practice_id()))
    return {"last": row["last"] if row else None, "today": (row["today"] or 0) if row else 0}


def guess_sender(text: str) -> str | None:
    for line in text.splitlines()[:6]:
        l = line.strip()
        if l and re.search(r"(?i)(dr\.?|spital|hôpital|klinik|clinique|praxis|centre|orthopädie|hausarzt)", l):
            return l[:160]
    return None


def recheck_duplicates(db: Database, did: int, patient_id: int) -> None:
    """Nach Zuordnung: Vorschläge, die schon in der Akte stehen, als Duplikat markieren."""
    findings = patients_api.findings(db, patient_id)
    for s in suggestions(db, did):
        if s["status"] != "open" or not s["structure_id"]:
            continue
        for f in findings:
            same = (f["kind"] == s["kind"] and f["structure_id"] == s["structure_id"] and (f["side"] or None) == (s["side"] or None))
            if same and (s["kind"] in ("diagnosis", "surgery") or (s["kind"] == "restriction" and f["until"] == s["until"])):
                db.execute("UPDATE suggestions SET status = 'duplicate' WHERE id = ? AND practice_id = ?", (s["id"], current_practice_id()))
                break


def assign_patient(db: Database, did: int, patient_id: int) -> None:
    pid = current_practice_id()
    with db.tx():
        db.execute("UPDATE documents SET patient_id = ? WHERE id = ? AND practice_id = ?", (patient_id, did, pid))
        db.execute("UPDATE suggestions SET patient_id = ? WHERE document_id = ? AND practice_id = ? AND status = 'open'",
                   (patient_id, did, pid))
        audit(db, "update", "document", did, patient_id, {"assigned": True})


def decide(db: Database, sid: int, accept: bool, *, title: str | None = None, until: str | None = None) -> dict | None:
    """Vorschlag übernehmen oder verwerfen. Rückgabe: Infos für die Rückmeldung."""
    pid = current_practice_id()
    s = db.one("SELECT * FROM suggestions WHERE id = ? AND practice_id = ?", (sid, pid))
    if s is None or s["status"] != "open":
        return None
    if not s["patient_id"]:
        raise ValueError("no_patient")
    finding_id = None
    with db.tx():
        if accept:
            if s["kind"] in FINDING_KINDS:
                finding_id = patients_api.add_finding(
                    db, s["patient_id"], kind=s["kind"], title=(title or s["text"]).strip(), structure_id=s["structure_id"],
                    region=s["region"], side=s["side"], spine_levels=s["spine_levels"], on_date=s["on_date"],
                    until=until or s["until"], source="document", source_document_id=s["document_id"], source_excerpt=s["excerpt"],
                )
            elif s["kind"] == "prescription" and s["sessions"]:
                doc = db.one("SELECT doc_date FROM documents WHERE id = ? AND practice_id = ?", (s["document_id"], pid))
                core_patients.add_prescription(db, s["patient_id"], sessions=s["sessions"], issued_on=doc["doc_date"],
                                               diagnosis=None, document_id=s["document_id"])
            elif s["kind"] == "insurer" and s["insurer"]:
                ins = core_patients.insurer_by_name(db, s["insurer"])
                if ins is not None:
                    core_patients.update(db, s["patient_id"], {"insurer_id": ins["id"], "law": s["law"] or ins["law"]})
        db.execute("UPDATE suggestions SET status = ?, finding_id = ?, decided_by = ?, decided_at = ? WHERE id = ? AND practice_id = ?",
                   ("accepted" if accept else "rejected", finding_id, current_user_id(), utcnow(), sid, pid))
        audit(db, "accept" if accept else "reject", "suggestion", sid, s["patient_id"], {"kind": s["kind"]})
        _maybe_done(db, s["document_id"])
    return {"document_id": s["document_id"], "patient_id": s["patient_id"]}


def _maybe_done(db: Database, did: int) -> None:
    left = db.value("SELECT COUNT(*) FROM suggestions WHERE document_id = ? AND practice_id = ? AND status = 'open'",
                    (did, current_practice_id()), 0)
    has_patient = db.value("SELECT patient_id FROM documents WHERE id = ? AND practice_id = ?", (did, current_practice_id()))
    if not left and has_patient:
        db.execute("UPDATE documents SET status = 'done', done_at = ? WHERE id = ? AND practice_id = ? AND status = 'review'",
                   (utcnow(), did, current_practice_id()))


def mark_done(db: Database, did: int) -> None:
    db.execute("UPDATE suggestions SET status = 'rejected', decided_by = ?, decided_at = ? WHERE document_id = ? AND practice_id = ? AND status = 'open'",
               (current_user_id(), utcnow(), did, current_practice_id()))
    db.execute("UPDATE documents SET status = 'done', done_at = ? WHERE id = ? AND practice_id = ?", (utcnow(), did, current_practice_id()))
    audit(db, "update", "document", did, db.value("SELECT patient_id FROM documents WHERE id = ?", (did,)), {"done": True})


def highlighted(text: str, items) -> Markup:
    """Originaltext mit markierten Zitaten der Vorschläge (unverändert, nur hervorgehoben)."""
    spans: list[tuple[int, int, int, str]] = []
    for s in items:
        excerpt = (s["excerpt"] or "").strip()
        if len(excerpt) < 3:
            continue
        pattern = r"\s+".join(re.escape(part) for part in excerpt.split())
        m = re.search(pattern, text)
        if m and not any(not (m.end() <= a or m.start() >= b) for a, b, _, _ in spans):
            spans.append((m.start(), m.end(), s["id"], s["status"]))
    spans.sort()
    out, pos = [], 0
    for a, b, sid, status in spans:
        out.append(escape(text[pos:a]))
        cls = "ok" if status == "accepted" else ""
        out.append(Markup(f'<mark id="s{sid}" class="{cls}">') + escape(text[a:b]) + Markup("</mark>"))
        pos = b
    out.append(escape(text[pos:]))
    return Markup("").join(out)
