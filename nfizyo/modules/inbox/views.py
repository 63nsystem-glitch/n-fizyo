"""Posteingang: Berichte hochladen oder scannen → lokale KI liest → Vorschläge → Therapeut/in bestätigt."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import web

from ...core import access
from ...core import patients as core_patients
from ...core.audit import audit
from ...core.events import publish
from ...core.keys import AI, DB, HIN, I18N, JOBS, SETTINGS
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.web import int_param, redirect_with_flash, render
from ..patients import api as patients_api
from . import repo
from ...core.timeutil import today as zurich_today

log = logging.getLogger(__name__)
SCAN_INTERVAL = 15


# --------------------------------------------------------------------------- Hintergrund: lesen und auswerten
async def job_read(app: web.Application, practice_id: int, did: int) -> None:
    db = app[DB]
    doc = repo.get(db, did)
    if doc is None:
        return
    db.execute("UPDATE documents SET status = 'processing', error = NULL WHERE id = ? AND practice_id = ?", (did, practice_id))
    ai = app[AI]
    try:
        text, method = await asyncio.to_thread(ai.read_document, Path(doc["path"]), doc["mime"])
        if not text.strip():
            db.execute("UPDATE documents SET status = 'failed', error = 'no_text', text_method = ? WHERE id = ? AND practice_id = ?",
                       (method, did, practice_id))
            return
        lang = db.value("SELECT default_lang FROM practices WHERE id = ?", (practice_id,), "de")
        result = await asyncio.to_thread(ai.extract, text, lang, zurich_today())
        matches = core_patients.find_by_name_birthdate(db, result.patient_name, result.patient_birthdate)
        repo.save_extraction(db, did, text, method, result, matches)
    except Exception as exc:  # noqa: BLE001 - Fehler sichtbar machen statt verschlucken
        db.execute("UPDATE documents SET status = 'failed', error = ? WHERE id = ? AND practice_id = ?",
                   (str(exc)[:300], did, practice_id))
        raise


PROCESSED_KEEP_DAYS = 14


def _prune_processed(done_dir: Path, now: float | None = None) -> int:
    """Abgeholte Scans liegen zusätzlich unter «verarbeitet» – nur 14 Tage als Kontrolle für die Praxis. Danach löschen:
    das Dokument ist im Posteingang gespeichert, und eine Kopie ausserhalb von N-Fizyo würde Löschen und Zusammenführen
    überdauern (Patientendaten ohne Zugriffsschutz und ohne Aufbewahrungsregel)."""
    if not done_dir.is_dir():
        return 0
    cutoff = (now or time.time()) - PROCESSED_KEEP_DAYS * 86400
    n = 0
    for old in done_dir.iterdir():
        if old.is_file() and old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
            n += 1
    return n


async def scan_loop(app: web.Application) -> None:
    """Scan-Ordner: Der Scanner legt PDFs in <daten>/scan-eingang/<praxis>/ – N-Fizyo holt sie automatisch ab."""
    settings = app[SETTINGS]
    db = app[DB]
    while True:
        try:
            for practice in db.query("SELECT id, slug FROM practices"):
                folder = settings.scan_dir / practice["slug"]
                folder.mkdir(parents=True, exist_ok=True)
                for f in sorted(folder.iterdir()):
                    if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in repo.ALLOWED:
                        continue
                    if f.stat().st_mtime > time.time() - 5:
                        continue  # Datei wird evtl. noch geschrieben
                    with practice_scope(practice["id"]):
                        did, new = repo.store_file(db, settings.documents_dir, f.name, f.read_bytes(), source="scan", uploaded_by=None)
                        if new:
                            app[JOBS].enqueue(practice["id"], "read_document", did)
                    done_dir = folder / "verarbeitet"
                    done_dir.mkdir(exist_ok=True)
                    f.rename(done_dir / f.name)
                _prune_processed(folder / "verarbeitet")
        except Exception:  # pragma: no cover
            log.exception("Scan-Ordner")
        await asyncio.sleep(SCAN_INTERVAL)


SCAN_TASK = web.AppKey("nfizyo_inbox_scan", asyncio.Task)


async def hin_poll_once(app: web.Application) -> int:
    """HIN-Postfach abholen: Anhänge (PDF, Bilder) und Text landen im Posteingang der Praxis, der das Postfach gehört.
    Gibt die Zahl neuer Dokumente zurück. Erst nach dem Speichern wird die Mail als gelesen markiert."""
    mailbox = app[HIN]
    if not mailbox.can_receive:
        return 0
    db = app[DB]
    practice = db.one("SELECT id FROM practices WHERE slug = ?", (app[SETTINGS].hin_practice,))
    if practice is None:
        log.warning("HIN: Praxis %r nicht gefunden – nichts abgeholt", app[SETTINGS].hin_practice)
        return 0
    messages = await asyncio.to_thread(mailbox.fetch_unseen)
    new_docs, done = 0, []
    lang = db.value("SELECT default_lang FROM practices WHERE id = ?", (practice["id"],), "de")
    t = app[I18N].t
    with practice_scope(practice["id"]):
        for m in messages:
            try:
                files = _hin_files(m, lambda key, **kw: t(lang if lang in ("de", "fr") else "de", key, **kw))
                for name, data in files:
                    did, new = repo.store_file(db, app[SETTINGS].documents_dir, name, data, source="hin", uploaded_by=None)
                    if new:                     # dieselbe Datei schon da (erneut geschickt): Herkunft der ersten nicht überschreiben
                        repo.set_origin(db, did, m.sender, m.subject)
                        new_docs += 1
                        app[JOBS].enqueue(practice["id"], "read_document", did)
            except Exception:  # noqa: BLE001 - eine Nachricht darf die anderen nicht aufhalten; bleibt ungelesen → nächster Versuch
                log.exception("HIN-Nachricht %s nicht übernommen", m.uid)
                continue
            done.append(m.uid)
    await asyncio.to_thread(mailbox.mark_seen, done)
    return new_docs


def _hin_files(m, t) -> list[tuple[str, bytes]]:
    """Was aus einer HIN-Mail in den Posteingang kommt. Nichts verschwindet still: Word-Dateien werden als Text übernommen,
    andere Formate und zu grosse Anhänge stehen mit Namen in einer Notiz (die Mail bleibt im HIN-Postfach)."""
    from ...core.hin import docx_text

    files: list[tuple[str, bytes]] = []
    missing: list[str] = []
    for name, data in m.attachments:
        suffix = Path(name).suffix.lower()
        if suffix in repo.ALLOWED:
            files.append((name, data))
        elif suffix == ".docx" and (text := docx_text(data)):
            files.append((f"{Path(name).stem}.txt", f"{Path(name).name}\n\n{text}".encode("utf-8")))
        else:
            missing.append(f"- {Path(name).name} ({max(1, len(data) // 1024)} KB)")
    missing += [f"- {Path(name).name} ({size // (1024 * 1024)} MB, {t('inbox.hin_too_large')})" for name, size in m.too_large]
    # Kopfzeile mit Absender und Nachrichtennummer: zwei Notizen mit gleichem Text (z. B. zwei leere oder unlesbare Mails)
    # wären sonst dieselbe Datei – die zweite würde als «schon da» verworfen und überschriebe den Absender der ersten.
    head = f"HIN · {m.sender or '?'} · #{m.uid}"
    if m.unreadable:
        return [(f"hin-{m.uid}.txt", f"{head}\n\n{t('inbox.hin_unreadable')}".encode("utf-8"))]
    if missing or not files:
        body = "\n\n".join(x for x in (m.subject, m.text.strip(), (t("inbox.hin_not_taken") + "\n" + "\n".join(missing)) if missing else "") if x)
        files.append((f"hin-{m.uid}.txt", f"{head}\n\n{body or t('inbox.hin_empty')}".encode("utf-8")))
    return files


async def hin_loop(app: web.Application) -> None:
    while True:
        try:
            await hin_poll_once(app)
        except Exception:  # pragma: no cover - HIN Client nicht erreichbar → später erneut
            log.exception("HIN-Postfach")
        await asyncio.sleep(app[SETTINGS].hin_poll_minutes * 60)


HIN_TASK = web.AppKey("nfizyo_inbox_hin", asyncio.Task)


async def start_scan(app: web.Application) -> None:
    app[SCAN_TASK] = asyncio.create_task(scan_loop(app))
    if app[HIN].can_receive:
        app[HIN_TASK] = asyncio.create_task(hin_loop(app))


async def stop_scan(app: web.Application) -> None:
    for key in (SCAN_TASK, HIN_TASK):
        if key in app:
            app[key].cancel()


# --------------------------------------------------------------------------- Zugriff
def is_therapie(request: web.Request) -> bool:
    return request["screen"].key == "therapie"


def _visible_to(request: web.Request):
    return (request["user"]["id"], access.patient_ids_sql(request["user"]["id"]))


def _load(request: web.Request):
    """Therapie: nur Dokumente eigener Patient/innen (oder selbst hochgeladen, noch ohne Zuordnung)."""
    doc = repo.get(request.app[DB], int_param(request, "did"))
    if doc is None:
        raise web.HTTPNotFound()
    if is_therapie(request):
        own_upload = doc["patient_id"] is None and doc["uploaded_by"] == request["user"]["id"]
        if not own_upload and not patients_api.can_open(request, doc["patient_id"]):
            raise web.HTTPNotFound()
    return doc


def _marks(items) -> dict:
    rows = [{"id": s["id"], "kind": s["kind"], "structure_id": s["structure_id"], "side": s["side"], "region": s["region"],
             "spine_levels": s["spine_levels"], "until": None} for s in items
            if s["status"] in ("open", "accepted") and s["kind"] not in ("insurer", "prescription")]
    return patients_api.finding_marks(rows)


def _safe_back(request: web.Request, value, default: str) -> str:
    value = str(value or "")
    return value if value.startswith(prefix(request) + "/") and "//" not in value[1:] else default


# --------------------------------------------------------------------------- Liste und Hochladen (beide Bildschirme)
async def inbox(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    f = request.query.get("f", "open")
    if is_therapie(request):
        docs = repo.listing(db, "open" if f == "open" else "done", visible_to=_visible_to(request))
        return await render(request, "inbox/t_list.html", {
            "docs": docs, "f": f, "n_open": repo.count_open(db, visible_to=_visible_to(request)),
            "patients": patients_api.my_patients(request), "ai_status": request.app[AI].status()})
    # Empfang: nur zuordnen – keine Inhalte. Statt eines Server-Pfads ein einfacher Stand des Scan-Ordners.
    docs = repo.listing(db, None if f == "alle" else "open", unassigned=f != "alle", limit=300)
    return await render(request, "inbox/r_list.html", {
        "docs": docs, "f": f, "n_unassigned": repo.count_open(db, unassigned=True), "scan": repo.scan_status(db)})


async def upload(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    form = await request.post()
    patient_id = int(form["patient_id"]) if str(form.get("patient_id", "")).isdigit() else None
    if patient_id and (core_patients.get(db, patient_id) is None or (is_therapie(request) and not patients_api.can_open(request, patient_id))):
        patient_id = None
    created = 0
    first = None
    for field in form.getall("files", []):
        if not hasattr(field, "file"):
            continue
        data = field.file.read()
        if not data:
            continue
        try:
            did, new = repo.store_file(db, request.app[SETTINGS].documents_dir, field.filename or "dokument.pdf", data,
                                       source="upload", patient_id=patient_id)
        except ValueError:
            raise redirect_with_flash(f"{S}/posteingang", "inbox.bad_type")
        first = first or did
        if new:
            request.app[JOBS].enqueue(current_practice_id(), "read_document", did)
            created += 1
            if patient_id:
                await publish(request.app, "document.assigned", document_id=did, patient_id=patient_id)
    default = (f"{S}/posteingang/{first}" if first and is_therapie(request) else f"{S}/posteingang")
    raise redirect_with_flash(_safe_back(request, form.get("back"), default), "inbox.uploaded" if created else "inbox.already")


async def status(request: web.Request) -> web.StreamResponse:
    doc = _load(request)
    return web.json_response({"done": doc["status"] in ("review", "done", "failed"), "status": doc["status"]})


async def assign(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    doc = _load(request)
    form = await request.post()
    pid = int(form["patient_id"]) if str(form.get("patient_id", "")).isdigit() else None
    if pid is None or core_patients.get(db, pid) is None or (is_therapie(request) and not patients_api.can_open(request, pid)):
        raise web.HTTPNotFound()
    repo.assign_patient(db, doc["id"], pid)
    repo.recheck_duplicates(db, doc["id"], pid)
    await publish(request.app, "document.assigned", document_id=doc["id"], patient_id=pid)
    S = prefix(request)
    raise redirect_with_flash(f"{S}/posteingang/{doc['id']}" if is_therapie(request) else f"{S}/posteingang", "inbox.assigned")


async def retry(request: web.Request) -> web.StreamResponse:
    doc = _load(request)
    request.app[JOBS].enqueue(current_practice_id(), "read_document", doc["id"])
    raise web.HTTPFound(f"{prefix(request)}/posteingang/{doc['id']}")


# --------------------------------------------------------------------------- Empfang: Zuordnen ohne Inhalt
async def r_detail(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    doc = _load(request)
    candidates = []
    if doc["match_ids"]:
        candidates = [c for c in (core_patients.get(db, int(x)) for x in doc["match_ids"].split(",") if x) if c]
    pending = doc["status"] in ("new", "processing") or request.app[JOBS].pending(current_practice_id(), "read_document", doc["id"])
    return await render(request, "inbox/r_detail.html", {
        "doc": doc, "candidates": candidates, "patients": core_patients.search(db, "", limit=2000), "pending": pending,
        "team": access.team(db, doc["patient_id"]) if doc["patient_id"] else []})


# --------------------------------------------------------------------------- Therapie: lesen und übernehmen
async def detail(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    doc = _load(request)
    items = repo.suggestions(db, doc["id"])
    candidates = []
    if doc["match_ids"]:
        candidates = [c for c in (core_patients.get(db, int(x)) for x in doc["match_ids"].split(",") if x)
                      if c and patients_api.can_open(request, c["id"])]
    pending = doc["status"] in ("new", "processing") or request.app[JOBS].pending(current_practice_id(), "read_document", doc["id"])
    audit(db, "view", "document", doc["id"], doc["patient_id"])
    return await render(request, "inbox/detail.html", {
        "doc": doc, "items": items, "marked": repo.highlighted(doc["text"] or "", items), "marks": _marks(items),
        "candidates": candidates, "patients": patients_api.my_patients(request),
        "warnings": json.loads(doc["warnings"]) if doc["warnings"] else [], "pending": pending,
    })


async def sheet(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    doc = _load(request)
    items = repo.suggestions(db, doc["id"])
    return await render(request, "inbox/sheet.html", {"doc": doc, "marked": repo.highlighted(doc["text"] or "", items)})


def _disposition(filename: str) -> str:
    """Dateiname im Header: ASCII-Ersatz plus UTF-8-Fassung (RFC 6266) – Umlaute zerbrechen den Header nicht."""
    from urllib.parse import quote

    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in filename) or "dokument"
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def read_text_file(path: str) -> str:
    """Text-Original lesen (UTF-8, sonst Windows-1252 der älteren Geräte)."""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


async def download(request: web.Request) -> web.StreamResponse:
    doc = _load(request)
    if not doc["path"] or not Path(doc["path"]).exists():
        raise web.HTTPNotFound()
    audit(request.app[DB], "export", "document", doc["id"], doc["patient_id"])
    headers = {"Content-Disposition": _disposition(doc["filename"]), "Cache-Control": "private, no-store"}
    if (doc["mime"] or "").startswith("text/"):
        # Text immer als UTF-8 ausliefern (sonst zeigt der Browser «LasÃ¨gue»)
        return web.Response(body=read_text_file(doc["path"]).encode("utf-8"), headers={
            **headers, "Content-Type": "text/plain; charset=utf-8"})
    return web.FileResponse(doc["path"], headers={**headers, "Content-Type": doc["mime"] or "application/octet-stream"})


async def original(request: web.Request) -> web.StreamResponse:
    """Original im Rahmen der App (mit Zurück): Text lesbar, Bilder direkt, PDF zum Öffnen."""
    doc = _load(request)
    if not doc["path"] or not Path(doc["path"]).exists():
        raise web.HTTPNotFound()
    mime = doc["mime"] or ""
    text = read_text_file(doc["path"]) if mime.startswith("text/") else None
    audit(request.app[DB], "view", "document", doc["id"], doc["patient_id"], {"original": True})
    return await render(request, "inbox/original.html", {
        "doc": doc, "text": text, "image": mime in ("image/png", "image/jpeg", "image/webp"), "pdf": mime == "application/pdf"})


async def decide(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    s = repo.suggestion(db, int_param(request, "sid"))
    if s is None or not patients_api.can_open(request, s["patient_id"]):
        raise web.HTTPNotFound()
    form = await request.post()
    accept = form.get("action") == "accept"
    try:
        info = repo.decide(db, s["id"], accept, title=str(form.get("title") or "").strip() or None,
                           until=str(form.get("until") or "").strip() or None)
    except ValueError:
        raise redirect_with_flash(_safe_back(request, form.get("back"), f"{S}/posteingang"), "inbox.need_patient")
    default = f"{S}/posteingang/{info['document_id']}" if info else f"{S}/posteingang"
    raise redirect_with_flash(_safe_back(request, form.get("back"), default), "inbox.accepted" if accept else "inbox.rejected")


async def accept_all(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    doc = _load(request)
    if not doc["patient_id"]:
        raise redirect_with_flash(f"{S}/posteingang/{doc['id']}", "inbox.need_patient")
    for s in repo.suggestions(db, doc["id"]):
        if s["status"] == "open":
            repo.decide(db, s["id"], True)
    raise redirect_with_flash(f"{S}/patienten/{doc['patient_id']}", "inbox.all_accepted")


async def done(request: web.Request) -> web.StreamResponse:
    doc = _load(request)
    repo.mark_done(request.app[DB], doc["id"])
    raise redirect_with_flash(f"{prefix(request)}/posteingang", "inbox.done")


# --------------------------------------------------------------------------- Akte: neue Berichte, Dokumente
async def card_new_report(request: web.Request, patient) -> dict | None:
    items = repo.open_for_patient(request.app[DB], patient["id"])
    if not items:
        return None
    docs: dict[int, dict] = {}
    for s in items:
        d = docs.setdefault(s["document_id"], {"id": s["document_id"], "title": s["doc_title"], "kind": s["doc_kind"], "sender": s["sender"],
                                               "created_at": s["doc_created"], "items": []})
        d["items"].append(s)
    return {"docs": list(docs.values())[:2], "patient": patient}


async def patient_documents(request: web.Request) -> web.StreamResponse:
    patient = patients_api.require_patient(request)
    docs = repo.listing(request.app[DB], None, patient_id=patient["id"])
    return await patients_api.file_page(request, patient, "documents", "inbox/t_patient.html", {"docs": docs})


def tab_documents(request: web.Request, patient) -> dict:
    n = request.app[DB].value("SELECT COUNT(*) FROM suggestions WHERE practice_id = ? AND patient_id = ? AND status = 'open'",
                              (patient["practice_id"], patient["id"]), 0)
    return {"key": "documents", "label_key": "inbox.tab", "path": "dokumente", "count": n}


def badge_mine(db, practice_id: int, user) -> int:
    with practice_scope(practice_id):
        return repo.count_open(db, visible_to=(user["id"], access.patient_ids_sql(user["id"])))


def badge_unassigned(db, practice_id: int, user) -> int:
    with practice_scope(practice_id):
        return repo.count_open(db, unassigned=True)


def todo_reports(request: web.Request) -> dict:
    return {"label_key": "today.open_inbox", "icon": "inbox", "href": f"{prefix(request)}/posteingang",
            "n": badge_mine(request.app[DB], request["user"]["practice_id"], request["user"])}


def work_inbox(request: web.Request) -> dict:
    return {"label_key": "stats.open.inbox_open", "icon": "inbox", "n": repo.count_open(request.app[DB]),
            "href": f"{prefix(request)}/offene-berichte"}


async def open_work_page(request: web.Request) -> web.StreamResponse:
    """Leitung: offene Berichte im Posteingang – wer muss zuordnen / prüfen (ohne Titel, Absender oder Inhalt)."""
    db = request.app[DB]
    rows = []
    per: dict[str, int] = {}
    for d in repo.listing(db, "open", limit=500):
        names = [m["name"] for m in access.team(db, d["patient_id"])] if d["patient_id"] else []
        rows.append({"d": d, "team": names})
        for key in (names or [""]):
            per[key] = per.get(key, 0) + 1
    return await render(request, "inbox/l_open.html", {"rows": rows, "per": sorted(per.items())})


def setup(ctx: ModuleContext) -> None:
    D = "/posteingang/{did:\\d+}"
    for screen in ("therapie", "empfang"):
        ctx.page(screen, "/posteingang", inbox)
        ctx.page(screen, "/posteingang/hochladen", upload, ("POST",))
        ctx.page(screen, D + "/status", status)
        ctx.page(screen, D + "/patient", assign, ("POST",))
        ctx.page(screen, D + "/neu-lesen", retry, ("POST",))
    ctx.page("empfang", D, r_detail)
    ctx.page("therapie", D, detail)
    ctx.page("therapie", D + "/blatt", sheet)
    ctx.page("therapie", D + "/datei", download)
    ctx.page("therapie", D + "/original", original)
    ctx.page("leitung", "/offene-berichte", open_work_page)
    ctx.page("therapie", D + "/alle", accept_all, ("POST",))
    ctx.page("therapie", D + "/erledigt", done, ("POST",))
    ctx.page("therapie", "/posteingang/vorschlag/{sid:\\d+}", decide, ("POST",))
    ctx.page("therapie", "/patienten/{pid:\\d+}/dokumente", patient_documents)
    ctx.nav("therapie", NavItem("nav.inbox", "inbox", "/posteingang", order=40, badge="inbox_mine", phone=True, short_key="nav.inbox_short"))
    ctx.nav("empfang", NavItem("nav.inbox", "inbox", "/posteingang", order=30, badge="inbox_unassigned", short_key="nav.inbox_short"))
    ctx.badge("inbox_mine", badge_mine)
    ctx.badge("inbox_unassigned", badge_unassigned)
    ctx.slot("therapie.patient.center", 20, card_new_report, "inbox/card_new_report.html")
    ctx.slot("therapie.patient.tab", 50, tab_documents)
    ctx.slot("therapie.today.todo", 10, todo_reports)
    ctx.slot("leitung.overview.work", 30, work_inbox)
    ctx.job("read_document", job_read, background=True)
    ctx.on_startup(start_scan)
    ctx.on_cleanup(stop_scan)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('documents', 'suggestions'), (
        "UPDATE documents SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE suggestions SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
