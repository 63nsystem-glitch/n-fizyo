"""Berichte an Zuweisende (Therapie-Bildschirm): Vorschlag aus der Akte → bearbeiten → abschliessen → drucken /
als versendet markieren."""
from __future__ import annotations

import asyncio

from aiohttp import web

from ...core import hin as core_hin
from ...core import patients as core_patients
from ...core.audit import audit
from ...core.keys import AI, DB, HIN, I18N, SETTINGS
from ...core.modules import ModuleContext, enabled
from ...core.screens import prefix
from ...core.tenant import current_practice_id
from ...core.web import int_param, redirect_with_flash, render
from ..patients import api as patients_api
from . import generate, repo


def _load(request: web.Request):
    r = repo.get(request.app[DB], int_param(request, "rid"))
    if r is None or not patients_api.can_open(request, r["patient_id"]):
        raise web.HTTPNotFound()
    return r


def _referrers(db):
    return db.query("SELECT * FROM referrers WHERE practice_id = ? AND active = 1 ORDER BY name", (current_practice_id(),))


def _done(request: web.Request, pr) -> int:
    if pr is None or not enabled(request, "agenda"):
        return 0
    from ..agenda import api as agenda_api

    return agenda_api.used_sessions(request.app[DB], pr["id"])    # gleiche Zählung wie Agenda (inkl. «bisher» aus der Übernahme)


# --------------------------------------------------------------------------- Patient: Liste + neu
async def patient_reports(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    if request.method == "POST":
        form = await request.post()
        kind = str(form.get("kind") or "progress")
        if kind not in repo.KINDS:
            kind = "progress"
        ref = None
        if str(form.get("referrer_id", "")).isdigit():
            ref = db.one("SELECT * FROM referrers WHERE id = ? AND practice_id = ?", (int(form["referrer_id"]), current_practice_id()))
        lang = str(form.get("lang") or (ref["lang"] if ref else request["lang"]))
        if lang not in ("de", "fr"):
            lang = "de"
        draft = generate.build(db, request.app[I18N], patient, ref, kind, lang, with_agenda=enabled(request, "agenda"))
        rid = repo.create(db, patient["id"], kind=kind, lang=lang, referrer_id=ref["id"] if ref else None,
                          prescription_id=draft.prescription_id, subject=draft.subject, salutation=draft.salutation,
                          sections=draft.sections)
        q = ("?hinweis=" + ",".join(draft.warnings)) if draft.warnings else ""
        raise web.HTTPFound(f"{prefix(request)}/berichte/{rid}{q}")
    pr = core_patients.active_prescription(db, patient["id"])
    return await patients_api.file_page(request, patient, "reports", "reports/patient.html", {
        "reports": repo.for_patient(db, patient["id"]), "referrers": _referrers(db), "default_ref": patient["referrer_id"],
        "suggested_kind": "renewal" if pr and _done(request, pr) >= pr["sessions"] - 2 else "progress", "kinds": repo.KINDS,
        "can_write": True,
    })


# --------------------------------------------------------------------------- Bericht
async def report_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    r = _load(request)
    if request.method == "POST":
        form = await request.post()
        if r["status"] == "draft":
            repo.save(db, r["id"], {k: str(form.get(k) or "").strip() for k in ("subject", "salutation", *repo.SECTIONS)
                                    if k in form})
            if form.get("do") == "finalize":
                repo.finalize(db, r["id"])
                raise redirect_with_flash(f"{S}/berichte/{r['id']}", "reports.finalized")
        raise redirect_with_flash(f"{S}/berichte/{r['id']}", "common.saved")
    hints = [h for h in request.query.get("hinweis", "").split(",") if h in ("other_language", "no_assessment")]
    patient = core_patients.get(db, r["patient_id"])
    return await render(request, "reports/report.html", {
        "r": r, "patient": patient, "hints": hints, "sections": repo.SECTIONS, "sent_via": repo.SENT_VIA,
        "can_write": True, "can_translate": request.app[AI].can_translate(), "hin": hin_status(request, r),
    })


async def print_view(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    r = _load(request)
    practice = db.one("SELECT * FROM practices WHERE id = ?", (current_practice_id(),))
    author = db.one("SELECT name, gln FROM users WHERE id = ? AND practice_id = ?", (r["author_id"], current_practice_id()))
    i18n = request.app[I18N]
    return await render(request, "reports/print.html", {
        "r": r, "pr": practice, "author": author, "tl": lambda key, **kw: i18n.t(r["lang"], key, **kw),
        "sections": repo.SECTIONS,
    })


def _pdf_bytes(request: web.Request, r) -> bytes:
    from . import pdf

    db = request.app[DB]
    practice = db.one("SELECT * FROM practices WHERE id = ?", (current_practice_id(),))
    author = db.one("SELECT name, gln FROM users WHERE id = ? AND practice_id = ?", (r["author_id"], current_practice_id()))
    i18n = request.app[I18N]
    return pdf.build(r, practice, author, lambda key, **kw: i18n.t(r["lang"], key, **kw),
                     demo=bool(practice["is_demo"]) or request.app[SETTINGS].demo)


def hin_status(request: web.Request, r) -> dict:
    """Kann dieser Bericht direkt über HIN gehen? (Box eingerichtet, Postfach gehört dieser Praxis, Adresse vorhanden)"""
    mailbox = request.app[HIN]
    slug = request.app[DB].value("SELECT slug FROM practices WHERE id = ?", (current_practice_id(),))
    return {"ready": mailbox.owns(slug) and bool(r["referrer_hin"]) and r["status"] == "final",
            "configured": mailbox.owns(slug), "address": r["referrer_hin"]}


async def pdf_download(request: web.Request) -> web.StreamResponse:
    r = _load(request)
    data = await asyncio.to_thread(_pdf_bytes, request, r)
    audit(request.app[DB], "export", "report", r["id"], r["patient_id"], {"format": "pdf"})
    name = f"bericht-{r['id']}.pdf"
    return web.Response(body=data, content_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


async def send_hin(request: web.Request) -> web.StreamResponse:
    """Abgeschlossenen Bericht als PDF über HIN an die Zuweiserin / den Zuweiser senden."""
    db = request.app[DB]
    S = prefix(request)
    r = _load(request)
    status = hin_status(request, r)
    if not status["ready"]:
        raise redirect_with_flash(f"{S}/berichte/{r['id']}", "reports.hin_not_ready")
    i18n = request.app[I18N]
    tl = lambda key, **kw: i18n.t(r["lang"], key, **kw)  # noqa: E731
    data = await asyncio.to_thread(_pdf_bytes, request, r)
    subject = f"{tl('reports.kind.' + r['kind'])}: {r['first_name']} {r['last_name']}" + (
        f", {r['birthdate'][8:10]}.{r['birthdate'][5:7]}.{r['birthdate'][:4]}" if r["birthdate"] else "")
    body = tl("reports.hin_body")
    try:
        ref = await asyncio.to_thread(request.app[HIN].send, r["referrer_hin"], subject, body,
                                      [(f"bericht-{r['id']}.pdf", data, "application/pdf")])
    except Exception as exc:  # noqa: BLE001 - Fehler anzeigen, nichts als versendet markieren
        core_hin.record_sent(db, current_practice_id(), r["referrer_hin"], f"Bericht #{r['id']}", ok=False, error=str(exc))
        raise redirect_with_flash(f"{S}/berichte/{r['id']}", "reports.hin_failed")
    core_hin.record_sent(db, current_practice_id(), r["referrer_hin"], f"Bericht #{r['id']}", ok=True, ref=ref)
    repo.mark_sent(db, r["id"], "hin")
    raise redirect_with_flash(f"{S}/berichte/{r['id']}", "reports.hin_sent")


async def mark_sent(request: web.Request) -> web.StreamResponse:
    r = _load(request)
    form = await request.post()
    repo.mark_sent(request.app[DB], r["id"], str(form.get("via") or ""))
    raise redirect_with_flash(f"{prefix(request)}/berichte/{r['id']}", "reports.marked_sent")


async def delete(request: web.Request) -> web.StreamResponse:
    r = _load(request)
    repo.delete_draft(request.app[DB], r["id"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{r['patient_id']}/berichte", "reports.deleted")


async def translate(request: web.Request) -> web.StreamResponse:
    """Beurteilung und Procedere maschinell in die Berichtssprache übersetzen (lokal). Nur Entwürfe."""
    db = request.app[DB]
    ai = request.app[AI]
    S = prefix(request)
    r = _load(request)
    src = request["lang"]
    if r["status"] != "draft" or not ai.can_translate() or src == r["lang"]:
        raise web.HTTPFound(f"{S}/berichte/{r['id']}")
    out = {}
    for k in ("assessment", "recommendation"):
        if r[k]:
            res = await asyncio.to_thread(ai.translate, db, current_practice_id(), r[k], src, r["lang"])
            if res.text and res.engine not in ("identity", "none"):
                out[k] = res.text
    if not out:   # KI nicht erreichbar oder Text abgelehnt: nicht «übersetzt» melden
        raise redirect_with_flash(f"{S}/berichte/{r['id']}", "reports.translate_failed")
    repo.save(db, r["id"], out)
    raise redirect_with_flash(f"{S}/berichte/{r['id']}", "reports.translated")


async def overview(request: web.Request) -> web.StreamResponse:
    return await render(request, "reports/list.html", {"drafts": repo.drafts(request.app[DB], request["user"]["id"])})


# --------------------------------------------------------------------------- Steckplätze
async def card_due(request: web.Request, patient) -> dict | None:
    """Hinweis: Verordnung fast aufgebraucht und noch kein Bericht seit Ausstellung."""
    if enabled(request, "rxwatch"):
        return None  # «Verordnungen im Blick» zeigt Stand und Gesuch in einer Karte
    db = request.app[DB]
    pr = core_patients.active_prescription(db, patient["id"])
    if pr is None:
        return None
    done = _done(request, pr)
    if done < pr["sessions"] - 2 or repo.last_since(db, patient["id"], pr["created_at"]):
        return None
    ref = db.one("SELECT name FROM referrers WHERE id = ? AND practice_id = ?", (pr["referrer_id"], current_practice_id())) \
        if pr["referrer_id"] else None
    return {"patient": patient, "done": done, "total": pr["sessions"], "ref": ref["name"] if ref else None,
            "referrer_id": pr["referrer_id"]}


def tab_reports(request: web.Request, patient) -> dict:
    n = request.app[DB].value("SELECT COUNT(*) FROM reports WHERE practice_id = ? AND patient_id = ? AND status = 'draft'",
                              (patient["practice_id"], patient["id"]), 0)
    return {"key": "reports", "label_key": "reports.tab", "path": "berichte", "count": n}


def todo_reports(request: web.Request) -> dict | None:
    n = len(repo.drafts(request.app[DB], request["user"]["id"]))
    return {"label_key": "today.open_reports", "icon": "description", "href": f"{prefix(request)}/berichte", "n": n} if n else None


def work_reports(request: web.Request) -> dict:
    return {"label_key": "stats.open.reports_draft", "icon": "description", "n": len(repo.drafts(request.app[DB])),
            "href": f"{prefix(request)}/offene-berichtsentwuerfe"}


async def open_drafts_leitung(request: web.Request) -> web.StreamResponse:
    """Leitung: Berichte im Entwurf – wer muss noch abschliessen (nur Art, Datum, Patient/in, Autor/in; kein Inhalt)."""
    rows = repo.drafts(request.app[DB])
    per: dict[str, int] = {}
    for r in rows:
        per[r["author_name"]] = per.get(r["author_name"], 0) + 1
    return await render(request, "reports/l_open.html", {"rows": rows, "per": sorted(per.items())})


def setup(ctx: ModuleContext) -> None:
    ctx.page("leitung", "/offene-berichtsentwuerfe", open_drafts_leitung)
    R = "/berichte/{rid:\\d+}"
    ctx.page("therapie", "/patienten/{pid:\\d+}/berichte", patient_reports, ("GET", "POST"))
    ctx.page("therapie", "/berichte", overview)
    ctx.page("therapie", R, report_page, ("GET", "POST"))
    ctx.page("therapie", R + "/druck", print_view)
    ctx.page("therapie", R + "/versendet", mark_sent, ("POST",))
    ctx.page("therapie", R + "/pdf", pdf_download)
    ctx.page("therapie", R + "/hin", send_hin, ("POST",))
    ctx.page("therapie", R + "/loeschen", delete, ("POST",))
    ctx.page("therapie", R + "/uebersetzen", translate, ("POST",))
    ctx.slot("therapie.patient.right", 12, card_due, "reports/card_due.html")
    ctx.slot("therapie.patient.tab", 60, tab_reports)
    ctx.slot("therapie.today.todo", 40, todo_reports)
    ctx.slot("leitung.overview.work", 40, work_reports)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('reports',), (
        "UPDATE reports SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
