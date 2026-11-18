"""Diktat: Aufnahme (Tablet/Telefon, auch offline) → Transkript → Notizentwurf → Freigabe."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid as uuidlib
from pathlib import Path

from aiohttp import web

from ...ai.stt import STTRejected, STTUnavailable
from ...ai.types import NoteContext
from ...core import patients as core_patients
from ...core.events import publish
from ...core.keys import AI, DB, I18N, JOBS, SETTINGS
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.timeutil import from_iso, now_local
from ...core.web import int_param, redirect_with_flash, render
from ...knowledge import anatomy
from ..agenda import api as agenda_api
from ..documentation import api as notes_api
from ..patients import api as patients_api
from . import demo_transcripts, repo
from ...core.timeutil import today as zurich_today

log = logging.getLogger(__name__)
BASE = "/therapie/diktat"  # Diktat gibt es nur im Therapie-Bildschirm


def _patient_context(db, patient_id: int) -> tuple[NoteContext, str | None]:
    patient = core_patients.get(db, patient_id) if patient_id else None
    joint = side = None
    for f in patients_api.findings(db, patient_id) if patient_id else []:
        st = anatomy.get(f["structure_id"])
        if st is not None:
            joint = st.joint or {"cervical_spine": "cervical", "lumbar_spine": "lumbar", "disc": "lumbar"}.get(st.id)
            side = f["side"] if f["side"] in ("l", "r") else None
            break
    ctx = NoteContext(patient_first_name=patient["first_name"] if patient else "",
                      patient_last_name=patient["last_name"] if patient else "",
                      joint=joint if joint in ("knee", "shoulder", "hip", "elbow", "ankle", "wrist", "cervical", "lumbar") else None,
                      side=side)
    return ctx, joint


# --------------------------------------------------------------------------- Hintergrundaufgaben
async def job_transcribe(app: web.Application, practice_id: int, rid: int) -> None:
    db = app[DB]
    rec = repo.get(db, rid)
    if rec is None or rec["status"] not in ("uploaded", "transcribing", "failed"):
        return
    repo.set_status(db, rid, "transcribing")
    ai = app[AI]
    try:
        if not await asyncio.to_thread(ai.can_transcribe):   # fragt den KI-Server ggf. im Thread (Webserver wartet nicht)
            raise STTUnavailable()
        t = await asyncio.to_thread(ai.transcribe, Path(rec["path"]), rec["language"])
        text, engine = t.text, t.engine
    except STTRejected as exc:
        # Diese Aufnahme geht nicht (zu lang, nicht lesbar): nicht immer wieder senden – Text selbst eingeben
        repo.set_status(db, rid, "failed", error=exc.reason)
        return
    except STTUnavailable:
        if not ai.simulate_demo:
            repo.set_status(db, rid, "failed", error="stt_unavailable")   # wird automatisch nachgeholt (retry_waiting)
            return
        _, joint = _patient_context(db, rec["patient_id"])
        text, engine = demo_transcripts.pick(rec["language"], joint), "demo"
    except Exception:
        # Unerwarteter Fehler (z. B. beschädigte Aufnahme): nie «wird verarbeitet …» für immer – sichtbar «fehlgeschlagen»,
        # die Warteschlange versucht es noch zweimal; danach kann die Fachperson den Text selbst eingeben.
        repo.set_status(db, rid, "failed", error="unknown")
        raise
    if not text.strip():
        repo.set_status(db, rid, "failed", error="empty")
        return
    repo.set_status(db, rid, "transcribed", transcript=text, transcript_engine=engine)
    app[JOBS].enqueue(practice_id, "draft_note", rid)


async def job_draft(app: web.Application, practice_id: int, rid: int) -> None:
    db = app[DB]
    rec = repo.get(db, rid)
    if rec is None or not rec["transcript"]:
        return
    repo.set_status(db, rid, "drafting")
    try:
        await _draft(app, practice_id, rid, rec)
    except Exception:
        # Entwurf ging schief: nicht «wird erstellt …» für immer – der erkannte Text bleibt und steht zum Bearbeiten bereit
        repo.set_status(db, rid, "failed", error="draft")
        raise


async def _draft(app: web.Application, practice_id: int, rid: int, rec) -> None:
    db = app[DB]
    if rec["patient_id"]:
        # andere Module (z. B. Patienteninformation) arbeiten mit demselben Text weiter
        with practice_scope(practice_id, rec["user_id"]):
            await publish(app, "dictation.transcribed", recording_id=rid, patient_id=rec["patient_id"], user_id=rec["user_id"],
                          transcript=rec["transcript"], language=rec["language"])
    ctx, _ = _patient_context(db, rec["patient_id"])
    draft = await asyncio.to_thread(app[AI].draft_note, rec["transcript"], rec["language"], ctx)
    sections = {s: getattr(draft, s) for s in notes_api.SECTIONS}
    note_id = rec["note_id"]
    if rec["patient_id"]:
        with practice_scope(practice_id, rec["user_id"]):
            session_date = (from_iso(rec["recorded_at"]).date() if "T" in (rec["recorded_at"] or "") else zurich_today()).isoformat()
            if note_id:
                notes_api.save(db, note_id, sections)
            else:
                note_id = notes_api.create_draft(db, rec["patient_id"], sections, appointment_id=rec["appointment_id"],
                                                 source="dictation", recording_id=rid, session_date=session_date,
                                                 author_id=rec["user_id"])
    repo.set_status(db, rid, "drafted", draft_json=draft.to_dict(), note_id=note_id)


PARTIAL = "upload-"          # Audiodatei, die gerade hereinkommt (noch ohne Diktat-Eintrag)
PARTIAL_KEEP_HOURS = 6


def purge_partial_uploads(audio_dir: Path, now_ts: float | None = None) -> int:
    """Reste abgebrochener Uploads (z. B. Neustart des Servers mitten im Hochladen) – Gesundheitsdaten bleiben nicht liegen."""
    import time

    cutoff = (now_ts or time.time()) - PARTIAL_KEEP_HOURS * 3600
    n = 0
    for f in audio_dir.glob(f"*/{PARTIAL}*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


RETRY_INTERVAL = 120
RETRY_TASK = web.AppKey("nfizyo_dictation_retry", asyncio.Task)


def retry_waiting(app: web.Application) -> int:
    """Diktate, die auf die KI warten mussten (KI-Server/Internet weg, Box neu gestartet), automatisch nachholen."""
    if not app[AI].can_transcribe():
        return 0
    db = app[DB]
    n = 0
    for rec in db.query("SELECT id, practice_id FROM recordings WHERE status = 'failed' AND error = 'stt_unavailable'"
                        " AND path IS NOT NULL AND created_at >= datetime('now', '-14 days')"):
        app[JOBS].enqueue(rec["practice_id"], "transcribe", rec["id"])
        n += 1
    return n


async def _retry_loop(app: web.Application) -> None:
    while True:
        await asyncio.sleep(RETRY_INTERVAL)
        try:
            n = await asyncio.to_thread(retry_waiting, app)
            if n:
                log.info("%s wartende Diktate werden jetzt verarbeitet", n)
        except Exception:  # pragma: no cover
            log.exception("Diktat-Nachholung")


async def _start_retry(app: web.Application) -> None:
    if not app[AI].simulate_demo:
        app[RETRY_TASK] = asyncio.create_task(_retry_loop(app))


async def _stop_retry(app: web.Application) -> None:
    if RETRY_TASK in app:
        app[RETRY_TASK].cancel()


# --------------------------------------------------------------------------- Zugriff
def _rec(request: web.Request):
    """Diktat laden: eigene Aufnahme oder Aufnahme zu einer Akte, die diese Person öffnen darf."""
    rec = repo.get(request.app[DB], int_param(request, "rid"))
    if rec is None or (rec["user_id"] != request["user"]["id"] and not patients_api.can_open(request, rec["patient_id"])):
        raise web.HTTPNotFound()
    return rec


def _appointment(request: web.Request, appointment_id: int | None, patient_id: int | None):
    """Termin nur, wenn die Agenda eingeschaltet ist und er zur Patientin / zum Patienten gehört."""
    if not appointment_id or not enabled(request, "agenda"):
        return None
    a = agenda_api.get(request.app[DB], appointment_id)
    return a if a is not None and a["patient_id"] == patient_id else None


def _context_hint(request: web.Request, draft: dict | None) -> dict | None:
    """Diktat nennt ein anderes Gelenk / eine andere Seite als die Akte → kurzer Hinweis auf der Prüfseite."""
    cd = (draft or {}).get("context_differs")
    if not cd:
        return None
    t, lang = request.app[I18N].t, request["lang"]

    def label(loc: dict) -> str:
        parts = [t(lang, f"joint.{loc['joint']}") if loc.get("joint") else ""]
        parts.append(t(lang, f"common.side.{loc['side']}") if loc.get("side") in ("l", "r") else t(lang, "dictation.no_side"))
        return " ".join(p for p in parts if p)

    return {"dictated": ", ".join(label(loc) for loc in cd.get("dictated", [])), "stored": label(cd.get("stored") or {})}


# --------------------------------------------------------------------------- Seiten
def _stt_flags(request: web.Request) -> dict:
    ai = request.app[AI]
    state = ai.stt_state()
    return {"stt": state == "ready", "stt_state": state, "demo_stt": ai.simulate_demo}


async def quick(request: web.Request) -> web.StreamResponse:
    """Telefon: Schnelldiktat für heutige Patient/innen, funktioniert auch offline (Warteschlange im Gerät)."""
    db = request.app[DB]
    user = request["user"]
    appts = [a for a in agenda_api.on_day(db, now_local().date(), therapist_id=user["id"]) if a["patient_id"]] \
        if enabled(request, "agenda") else []
    preselect = int(request.query["patient"]) if request.query.get("patient", "").isdigit() else None
    return await render(request, "dictation/quick.html", {
        "appts": appts, "pending": repo.pending_for(db, user["id"]), "patients": patients_api.my_patients(request),
        "preselect": preselect if preselect and patients_api.can_open(request, preselect) else None,
        **_stt_flags(request),
    })


async def for_appointment(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    a = agenda_api.get(db, int_param(request, "aid")) if enabled(request, "agenda") else None
    if a is None or not a["patient_id"] or not patients_api.can_open(request, a["patient_id"]):
        raise web.HTTPNotFound()
    patient = patients_api.require_patient(request, patient_id=a["patient_id"])
    today = zurich_today().isoformat()
    restrictions = [f for f in patients_api.findings(db, patient["id"])
                    if f["kind"] == "restriction" and not (f["until"] and f["until"] < today)]
    return await render(request, "dictation/record.html", {
        "a": a, "patient": patient, "restrictions": restrictions, "recordings": repo.for_appointment(db, a["id"]),
        "note": notes_api.for_appointment(db, a["id"]), "session": agenda_api.session_number(db, a),
        **_stt_flags(request), "start": from_iso(a["starts_at"]),
    })


async def upload(request: web.Request) -> web.StreamResponse:
    """Aufnahme vom Gerät (fetch, multipart). Idempotent über client_uuid – doppelte Uploads sind harmlos."""
    db = request.app[DB]
    settings = request.app[SETTINGS]
    reader = await request.multipart()
    meta: dict[str, str] = {}
    saved_path = None
    mime = None
    size = 0
    practice_dir = settings.audio_dir / str(current_practice_id())
    practice_dir.mkdir(parents=True, exist_ok=True)
    async for part in reader:
        if part.name == "audio":
            mime = (part.headers.get("Content-Type") or "audio/webm").split(";")[0].strip()
            if mime not in repo.AUDIO_TYPES:
                return web.json_response({"error": "type"}, status=415)
            saved_path = practice_dir / f"{PARTIAL}{uuidlib.uuid4().hex}{repo.AUDIO_TYPES[mime]}"
            try:
                with saved_path.open("wb") as fh:
                    while True:
                        chunk = await part.read_chunk(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        fh.write(chunk)
            except BaseException:
                # Verbindung mitten im Hochladen weg (Handy im Funkloch): halbe Audiodatei sofort löschen – das Gerät
                # schickt die Aufnahme später nochmals ganz
                saved_path.unlink(missing_ok=True)
                raise
        else:
            meta[part.name] = (await part.text())[:200]
    client_uuid = meta.get("client_uuid", "")
    if not saved_path:
        return web.json_response({"error": "missing"}, status=400)
    if meta.get("user") and meta["user"] != str(request["user"]["id"]):
        # Gerät wird geteilt: Aufnahme einer anderen Fachperson nie unter diesem Konto ablegen (falsche Urheberschaft)
        saved_path.unlink(missing_ok=True)
        return web.json_response({"error": "other_user"}, status=409)
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", client_uuid):
        saved_path.unlink(missing_ok=True)
        return web.json_response({"error": "uuid"}, status=400)
    existing = repo.by_uuid(db, client_uuid)
    if existing is not None:  # bereits angekommen (z. B. zweiter Versuch nach Verbindungsabbruch)
        saved_path.unlink(missing_ok=True)
        return web.json_response({"id": existing["id"], "status": existing["status"], "url": f"{BASE}/{existing['id']}"})
    final_path = practice_dir / f"{client_uuid}{saved_path.suffix}"
    saved_path.rename(final_path)
    saved_path = final_path
    patient_id = int(meta["patient_id"]) if meta.get("patient_id", "").isdigit() else None
    if patient_id and not patients_api.can_open(request, patient_id):
        patient_id = None
    appointment_id = int(meta["appointment_id"]) if meta.get("appointment_id", "").isdigit() else None
    a = _appointment(request, appointment_id, patient_id)
    lang = meta.get("lang") if meta.get("lang") in ("de", "fr") else request["user"]["lang"]
    rid = repo.create(db, client_uuid=client_uuid, patient_id=patient_id, appointment_id=a["id"] if a else None, language=lang,
                      recorded_at=meta.get("recorded_at"), path=str(saved_path), mime=mime, size=size)
    request.app[JOBS].enqueue(current_practice_id(), "transcribe", rid)
    return web.json_response({"id": rid, "status": "uploaded", "url": f"{BASE}/{rid}"})


async def typed_new(request: web.Request) -> web.StreamResponse:
    """Ohne Spracherkennung: Text tippen (oder mit der Diktierfunktion der Tastatur) → gleicher Ablauf."""
    db = request.app[DB]
    form = await request.post()
    text = str(form.get("text", "")).strip()
    patient_id = int(form["patient_id"]) if str(form.get("patient_id", "")).isdigit() else None
    back = str(form.get("back") or "")
    if not text or not patient_id or not patients_api.can_open(request, patient_id):
        raise redirect_with_flash(back if back.startswith(BASE) else BASE, "dictation.text_missing")
    appointment_id = int(form["appointment_id"]) if str(form.get("appointment_id", "")).isdigit() else None
    a = _appointment(request, appointment_id, patient_id)
    rid = repo.create(db, client_uuid=uuidlib.uuid4().hex, patient_id=patient_id, appointment_id=a["id"] if a else None,
                      language=request["user"]["lang"], recorded_at=None, transcript=text)
    request.app[JOBS].enqueue(current_practice_id(), "draft_note", rid)
    raise web.HTTPFound(f"{BASE}/{rid}")


async def typed_existing(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    rec = _rec(request)
    form = await request.post()
    text = str(form.get("text", "")).strip()
    if text:
        repo.set_status(db, rec["id"], "transcribed", transcript=text, transcript_engine="typed", error=None)
        request.app[JOBS].enqueue(current_practice_id(), "draft_note", rec["id"])
    raise web.HTTPFound(f"{BASE}/{rec['id']}")


async def review(request: web.Request) -> web.StreamResponse:
    from ...core.slots import collect

    db = request.app[DB]
    rec = _rec(request)
    note = notes_api.get(db, rec["note_id"]) if rec["note_id"] else None
    draft = json.loads(rec["draft_json"]) if rec["draft_json"] else None
    patient = core_patients.get(db, rec["patient_id"]) if rec["patient_id"] else None
    pending = rec["status"] in ("uploaded", "transcribing", "transcribed", "drafting") or request.app[JOBS].pending(
        current_practice_id(), "draft_note", rec["id"])
    # weitere Karten zum Prüfen (z. B. Patienteninformation in der Sprache der Patientin / des Patienten)
    extra = await collect(request, "therapie.dictation.review", rec) if patient and not pending else []
    return await render(request, "dictation/review.html", {
        "rec": rec, "note": note, "draft": draft, "patient": patient, "pending": pending, "extra": extra,
        "context_hint": _context_hint(request, draft), "audio_kept": bool(rec["path"]), "patients": patients_api.my_patients(request) if not patient else [],
    })


async def status(request: web.Request) -> web.StreamResponse:
    rec = _rec(request)
    done = rec["status"] in ("drafted", "failed", "approved")
    return web.json_response({"done": done, "status": rec["status"]})


async def assign_patient(request: web.Request) -> web.StreamResponse:
    """Schnelldiktat ohne Patient: nachträglich zuordnen, dann Entwurf erzeugen."""
    db = request.app[DB]
    rec = _rec(request)
    form = await request.post()
    pid = int(form["patient_id"]) if str(form.get("patient_id", "")).isdigit() else None
    if pid is None or not patients_api.can_open(request, pid):
        raise web.HTTPNotFound()
    repo.set_patient(db, rec["id"], pid)
    if rec["transcript"]:
        request.app[JOBS].enqueue(current_practice_id(), "draft_note", rec["id"])
    raise web.HTTPFound(f"{BASE}/{rec['id']}")


async def approve(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    rec = _rec(request)
    if not rec["note_id"]:
        raise web.HTTPNotFound()
    form = await request.post()
    sections = {s: form.get(s, "") for s in notes_api.SECTIONS}
    note = notes_api.get(db, rec["note_id"])
    if note["status"] == "draft":
        notes_api.save(db, note["id"], sections)
    # angehakte Messwerte übernehmen
    draft = json.loads(rec["draft_json"]) if rec["draft_json"] else {}
    for i, m in enumerate(draft.get("measurements", [])):
        if form.get(f"m{i}") == "1":
            patients_api.add_measurement(db, rec["patient_id"], m["kind"], float(m["value"]), m["unit"], joint=m.get("joint"),
                                         side=m.get("side"), measured_on=note["session_date"], note_id=note["id"])
    await notes_api.approve(request.app, note["id"])  # meldet «note.approved» → Agenda: Termin erledigt
    repo.set_status(db, rec["id"], "approved")
    if request.app[SETTINGS].delete_audio_after_approval:
        repo.delete_audio(db, rec)
    await publish(request.app, "dictation.approved", recording_id=rec["id"], note_id=note["id"], patient_id=rec["patient_id"],
                  appointment_id=rec["appointment_id"], form=form)
    raise redirect_with_flash(f"{prefix(request)}/patienten/{rec['patient_id']}", "dictation.approved")


async def drafts(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    user = request["user"]
    return await render(request, "dictation/drafts.html", {
        "notes": notes_api.drafts_for(db, user["id"]),
        "pending": [r for r in repo.pending_for(db, user["id"]) if r["status"] in ("uploaded", "transcribing", "failed", "transcribed", "drafting")],
    })


async def service_worker(request: web.Request) -> web.StreamResponse:
    js = (Path(__file__).parent / "sw.js").read_text(encoding="utf-8")
    return web.Response(text=js, content_type="application/javascript",
                        headers={"Service-Worker-Allowed": BASE, "Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- Steckplätze, Ereignisse
def action_dictate(request: web.Request, patient) -> dict:
    a = agenda_api.today_for_patient(request.app[DB], patient["id"]) if enabled(request, "agenda") else None
    href = f"{BASE}/termin/{a['id']}" if a else f"{BASE}?patient={patient['id']}"
    return {"label_key": "dictation.dictate_session", "icon": "mic", "href": href, "primary": True}


def appointment_dictate(request: web.Request, a) -> dict | None:
    if not a["patient_id"] or a["status"] not in ("booked", "arrived", "done"):
        return None
    return {"label_key": "today.dictate", "icon": "mic", "href": f"{BASE}/termin/{a['id']}", "primary": True}


def todo_pending(request: web.Request) -> dict:
    n = sum(1 for r in repo.pending_for(request.app[DB], request["user"]["id"]) if r["status"] in ("failed", "uploaded", "transcribing"))
    return {"label_key": "today.open_dictations", "icon": "mic", "href": f"{BASE}/entwuerfe", "n": n}


def on_note_discarding(app: web.Application, *, note_id: int, **_) -> None:
    repo.unlink_note(app[DB], note_id)


def usage_month(request: web.Request) -> dict:
    from ...core.timeutil import day_bounds_utc, today

    lo, _ = day_bounds_utc(today().replace(day=1))
    n = request.app[DB].value("SELECT COUNT(*) FROM recordings WHERE practice_id = ? AND created_at >= ?",
                              (current_practice_id(), lo), 0)
    return {"label_key": "stats.app.dictations", "n": n}


def setup(ctx: ModuleContext) -> None:
    R = "/diktat/{rid:\\d+}"
    ctx.page("therapie", "/diktat", quick)
    ctx.public(BASE + "/sw.js", service_worker)
    ctx.page("therapie", "/diktat/entwuerfe", drafts)
    ctx.page("therapie", "/diktat/termin/{aid:\\d+}", for_appointment)
    ctx.page("therapie", "/diktat/upload", upload, ("POST",))
    ctx.page("therapie", "/diktat/text", typed_new, ("POST",))
    ctx.page("therapie", R, review)
    ctx.page("therapie", R + "/status", status)
    ctx.page("therapie", R + "/text", typed_existing, ("POST",))
    ctx.page("therapie", R + "/patient", assign_patient, ("POST",))
    ctx.page("therapie", R + "/freigeben", approve, ("POST",))
    ctx.nav("therapie", NavItem("nav.dictation", "mic", "/diktat", order=15, phone=True, rail=False))
    ctx.slot("therapie.patient.action", 10, action_dictate)
    ctx.slot("therapie.appointment.action", 10, appointment_dictate)
    ctx.slot("therapie.today.todo", 20, todo_pending)
    ctx.on("note.discarding", on_note_discarding)
    ctx.job("transcribe", job_transcribe)
    ctx.job("draft_note", job_draft)
    audio_dir = ctx.app[SETTINGS].audio_dir
    ctx.housekeeping(lambda db, now: purge_partial_uploads(audio_dir, now.timestamp()))
    ctx.on_startup(_start_retry)
    ctx.on_cleanup(_stop_retry)
    ctx.slot("leitung.overview.usage", 40, usage_month)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('recordings',), (
        "UPDATE recordings SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
