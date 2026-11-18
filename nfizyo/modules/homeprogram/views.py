"""Heimprogramm: Therapeut/in stellt zusammen und gibt frei; Patient/in sieht es in der App (in der eigenen Sprache)."""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from pathlib import Path

from aiohttp import web

from ...core import languages
from ...core.audit import audit
from ...core.db import Database
from ...core.keys import AI, DB, I18N, JOBS, SETTINGS
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.web import int_param, redirect_with_flash, render
from ...knowledge.regions import FACES
from ..patients import api as patients_api
from . import dose, drawings, library_data, repo

log = logging.getLogger(__name__)


def ensure_library(db: Database, practice_id: int) -> None:
    if db.value("SELECT COUNT(*) FROM exercises WHERE practice_id = ?", (practice_id,), 0):
        return
    with practice_scope(practice_id):
        for code, areas, ic, sets, reps, hold, nde, nfr, tde, tfr in library_data.EXERCISES:
            repo.add_exercise(db, code=code, regions=areas, icon=ic, sets=sets, reps=reps, hold_sec=hold, name_de=nde,
                              name_fr=nfr, instructions_de=tde, instructions_fr=tfr,
                              drawing=code if drawings.valid(code) else None, duration_min=library_data.DURATIONS.get(code))


async def seed_libraries(app: web.Application) -> None:
    db = app[DB]
    for p in db.query("SELECT id FROM practices"):
        ensure_library(db, p["id"])


def helpers(request: web.Request, short: bool = True) -> dict:
    """Für die Vorlagen: Bild einer Übung (Zeichnung oder Körperbereich) und Dosierung als Text."""
    i18n, lang = request.app[I18N], request["lang"]
    return {"pic_of": drawings.picture, "dose_text": lambda row, short=short: dose.text(i18n, lang, row, short=short)}


# --------------------------------------------------------------------------- Therapie
async def patient_program(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    prog = repo.current_program(db, patient["id"])
    q, area = request.query.get("q", ""), request.query.get("bereich", "")
    lib = repo.library(db, q, area)
    return await patients_api.file_page(request, patient, "program", "homeprogram/patient.html", {
        "prog": prog, "items": repo.items(db, prog["id"]) if prog else [], "lib": lib, "q": q, "area": area,
        "areas": repo.AREAS, "adherence": repo.adherence(db, patient["id"], 14),
        "patient_lang_name": languages.name_for(patient["lang"], request["lang"]),
        "translate_on": request.app[AI].can_translate(),
        **helpers(request),
    })


async def program_add(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    form = await request.post()
    prog = repo.current_program(db, patient["id"], create=True)
    if str(form.get("exercise_id", "")).isdigit():
        try:
            repo.add_item(db, prog, int(form["exercise_id"]), str(form.get("note") or "").strip() or None, request["lang"])
        except KeyError:            # fremde oder deaktivierte Übung
            raise web.HTTPNotFound()
    raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}/heimprogramm", "hp.added")


async def item_update(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    it = repo.item(db, int_param(request, "iid"))
    if it is None or not patients_api.can_open(request, it["patient_id"]):
        raise web.HTTPNotFound()
    form = await request.post()
    if form.get("remove") == "1":
        _delete_video_file(request, repo.clear_video(db, it["id"]))
        repo.remove_item(db, it["id"])
    else:
        repo.set_essential(db, it["id"], form.get("essential") == "1")
        def num(k):
            v = str(form.get(k, "")).strip()
            return int(v) if v.isdigit() and 0 < int(v) <= LIMITS[k] else None
        repo.update_item(db, it["id"], sets=num("sets"), reps=num("reps"), hold_sec=num("hold_sec"), duration_min=num("duration_min"),
                         per_day=num("per_day") or 1, note=str(form.get("note") or "").strip() or None, note_lang=request["lang"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{it['patient_id']}/heimprogramm", "common.saved")


async def program_approve(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = patients_api.require_patient(request)
    prog = repo.current_program(db, patient["id"])
    if prog is None:
        raise web.HTTPNotFound()
    repo.approve(db, prog)
    raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}/heimprogramm", "hp.approved")


async def library_page(request: web.Request) -> web.StreamResponse:
    """Übungsbibliothek der Praxis: suchen, bearbeiten, deaktivieren. Neue Übung auf eigener Seite."""
    db = request.app[DB]
    q, area = request.query.get("q", ""), request.query.get("bereich", "")
    inactive = request.query.get("inaktiv") == "1"
    return await render(request, "homeprogram/library.html", {
        "lib": repo.library(db, q, area, active=not inactive), "q": q, "area": area, "areas": repo.AREAS, "inactive": inactive,
        "n_inactive": len(repo.library(db, active=False)), **helpers(request)})


LIMITS = {"sets": 20, "reps": 200, "hold_sec": 600, "duration_min": 240, "per_day": 10}


def _exercise_form(form) -> tuple[dict, dict, set[str]]:
    """Formular → (Daten für die Datenbank, eingegebene Werte, Fehler)."""
    v = {k: str(form.get(k) or "").strip() for k in ("name_de", "name_fr", "instructions_de", "instructions_fr", "drawing",
                                                      *LIMITS)}
    areas = [a for a in repo.AREAS if a in form.getall("area", [])]
    v["areas"] = areas
    errors: set[str] = set()
    if not (v["name_de"] or v["name_fr"]):
        errors.add("name")
    if not (v["instructions_de"] or v["instructions_fr"]):
        errors.add("instructions")
    nums: dict = {}
    for k, top in LIMITS.items():
        if not v[k]:
            nums[k] = None
        elif v[k].isdigit() and len(v[k]) <= 4 and 0 < int(v[k]) <= top:
            nums[k] = int(v[k])
        else:
            nums[k] = None          # Fehler: Formular zeigt das Feld markiert (nie 500)
            errors.add(k)
    data = {"name_de": v["name_de"] or v["name_fr"], "name_fr": v["name_fr"] or v["name_de"],
            "instructions_de": v["instructions_de"] or v["instructions_fr"],
            "instructions_fr": v["instructions_fr"] or v["instructions_de"],
            "regions": ",".join(areas) or "general", "drawing": v["drawing"] if drawings.valid(v["drawing"]) else None,
            **nums}
    data["per_day"] = data["per_day"] or 1
    return data, v, errors


def _form_values(ex) -> dict:
    v = {k: ("" if ex[k] is None else str(ex[k])) for k in ("name_de", "name_fr", "instructions_de", "instructions_fr", "drawing",
                                                              *LIMITS)}
    v["areas"] = [a for a in (ex["regions"] or "").split(",") if a in repo.AREAS]
    return v


async def _form_page(request: web.Request, v: dict, errors: set[str], ex=None) -> web.StreamResponse:
    db = request.app[DB]
    usage = repo.exercise_usage(db, ex["id"]) if ex else 0
    return await render(request, "homeprogram/exercise_form.html", {
        "v": v, "errors": errors, "ex": ex, "areas": repo.AREAS, "choices": drawings.choices(request["lang"]), "limits": LIMITS,
        "usage": dose.plural(request.app[I18N], request["lang"], "hp.edit_usage", usage) if usage else "", **helpers(request)},
        status=400 if errors else 200)


async def exercise_new(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    if request.method == "POST":
        data, v, errors = _exercise_form(await request.post())
        if not errors:
            eid = repo.add_exercise(db, icon="fitness_center", **data)
            audit(db, "create", "exercise", eid)
            raise redirect_with_flash(f"{prefix(request)}/uebungen", "hp.exercise_added")
        return await _form_page(request, v, errors)
    area = request.query.get("bereich", "")
    return await _form_page(request, {"areas": [area] if area in repo.AREAS else []}, set())


async def exercise_edit(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    ex = repo.exercise(db, int_param(request, "eid"))
    if ex is None:
        raise web.HTTPNotFound()
    if request.method == "POST":
        data, v, errors = _exercise_form(await request.post())
        if not errors:
            repo.update_exercise(db, ex["id"], **data)
            raise redirect_with_flash(f"{prefix(request)}/uebungen", "hp.exercise_saved")
        return await _form_page(request, v, errors, ex)
    return await _form_page(request, _form_values(ex), set(), ex)


async def exercise_active(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    ex = repo.exercise(db, int_param(request, "eid"))
    if ex is None:
        raise web.HTTPNotFound()
    on = (await request.post()).get("on") == "1"
    repo.set_exercise_active(db, ex["id"], on)
    raise redirect_with_flash(f"{prefix(request)}/uebungen" + ("" if on else "?inaktiv=1"),
                              "hp.exercise_reactivated" if on else "hp.exercise_deactivated")


# --------------------------------------------------------------------------- Eigenes Video (nur mit Einwilligung)
VIDEO_MAX = 60 * 1024 * 1024


def video_kind(data: bytes) -> tuple[str, str] | None:
    """(MIME-Typ, Endung) aus den ersten Bytes – nur MP4, MOV und WebM."""
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ("video/quicktime", ".mov") if data[8:10] == b"qt" else ("video/mp4", ".mp4")
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", ".webm"
    return None


def _video_dir(request: web.Request) -> Path:
    d = request.app[SETTINGS].data_dir / "videos" / str(current_practice_id())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _delete_video_file(request: web.Request, path: str | None) -> None:
    if not path:
        return
    p = Path(path)
    root = (request.app[SETTINGS].data_dir / "videos").resolve()
    if p.resolve().is_relative_to(root):
        p.unlink(missing_ok=True)


def _serve_video(it) -> web.StreamResponse:
    if not it["video_path"] or not Path(it["video_path"]).exists():
        raise web.HTTPNotFound()
    return web.FileResponse(it["video_path"], headers={"Content-Type": it["video_mime"] or "video/mp4", "Content-Disposition": "inline",
                                                       "Cache-Control": "private, no-store"})


def _own_item(request: web.Request):
    it = repo.item(request.app[DB], int_param(request, "iid"))
    if it is None or not patients_api.can_open(request, it["patient_id"]):
        raise web.HTTPNotFound()
    return it


async def video_upload(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    it = _own_item(request)
    back = f"{prefix(request)}/patienten/{it['patient_id']}/heimprogramm"
    form = await request.post()
    if form.get("consent") != "1":
        raise redirect_with_flash(back, "hp.video_need_consent")
    field = form.get("video")
    data = field.file.read(VIDEO_MAX + 1) if hasattr(field, "file") else b""
    kind = video_kind(data) if data else None
    if kind is None or len(data) > VIDEO_MAX:
        raise redirect_with_flash(back, "hp.video_bad")
    path = _video_dir(request) / f"{secrets.token_hex(16)}{kind[1]}"
    path.write_bytes(data)
    _delete_video_file(request, it["video_path"])
    repo.set_video(db, it["id"], str(path), kind[0], len(data))
    audit(db, "create", "exercise_video", it["id"], it["patient_id"], {"size": len(data), "consent": True})
    if shutil.which("ffmpeg"):
        request.app[JOBS].enqueue(current_practice_id(), "video_convert", it["id"])
    raise redirect_with_flash(back, "hp.video_saved")


def convert_command(src: Path, out: Path) -> list[str]:
    """H.264/AAC in MP4, längste Seite höchstens 1280 px, schneller Start im Browser (faststart)."""
    scale = "scale='if(gte(iw,ih),min(1280,trunc(iw/2)*2),-2)':'if(gte(iw,ih),-2,min(1280,trunc(ih/2)*2))'"
    return ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", scale,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", str(out)]


async def job_video_convert(app: web.Application, practice_id: int, item_id: int) -> None:
    """iPhone-Videos (.mov, oft HEVC) spielen auf Android-Handys und in Firefox/Chrome nicht ab. Deshalb jedes eigene Video
    in MP4 (H.264) umwandeln – läuft überall, ist kleiner. Misslingt es, bleibt das Original."""
    import subprocess

    db = app[DB]
    it = repo.item(db, item_id)
    if it is None or not it["video_path"] or it["video_path"].endswith("-web.mp4") or not Path(it["video_path"]).exists():
        return
    src = Path(it["video_path"])
    out = src.with_name(src.stem + "-web.mp4")
    try:
        done = await asyncio.to_thread(subprocess.run, convert_command(src, out), capture_output=True, timeout=900)
        ok = done.returncode == 0 and out.exists() and out.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        log.warning("Video %s nicht umgewandelt – Original bleibt", item_id)
        out.unlink(missing_ok=True)
        return
    if repo.swap_video_file(db, item_id, str(src), str(out), "video/mp4", out.stat().st_size):
        src.unlink(missing_ok=True)
    else:
        out.unlink(missing_ok=True)   # inzwischen ersetzt oder gelöscht


async def video_delete(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    it = _own_item(request)
    _delete_video_file(request, repo.clear_video(db, it["id"]))
    audit(db, "delete", "exercise_video", it["id"], it["patient_id"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{it['patient_id']}/heimprogramm", "hp.video_deleted")


async def video_view(request: web.Request) -> web.StreamResponse:
    return _serve_video(_own_item(request))


async def card_program(request: web.Request, patient) -> dict | None:
    db = request.app[DB]
    prog = repo.current_program(db, patient["id"])
    if prog is None:
        return {"prog": None, "patient": patient}
    adherence = repo.adherence(db, patient["id"], 7)
    return {"prog": prog, "items": repo.items(db, prog["id"])[:4], "days_done": sum(1 for d in adherence if d["n"]),
            "patient": patient, **helpers(request)}


def tab_program(request: web.Request, patient) -> dict:
    return {"key": "program", "label_key": "hp.tab", "path": "heimprogramm", "count": 0}


# --------------------------------------------------------------------------- Patient (App)
async def _localized(request: web.Request, text: str | None, src: str) -> dict:
    """Text für die Patientin in ihrer Sprache – maschinell übersetzt (mit Original) oder Original."""
    lang = request["lang"]
    if not text:
        return {"text": "", "machine": False, "original": None, "lang": lang}
    if lang == src:
        return {"text": text, "machine": False, "original": None, "lang": src}
    db = request.app[DB]
    res = await asyncio.to_thread(request.app[AI].translate, db, current_practice_id(), text, src, lang)
    if res.text and res.engine not in ("identity", "none"):
        return {"text": res.text, "machine": True, "original": text, "demo": res.engine == "demo", "lang": lang, "src_lang": src}
    return {"text": text, "machine": False, "original": None, "untranslated": True, "lang": src}


def _source(row, field: str, lang: str) -> tuple[str, str]:
    """Quelltext wählen: Patientensprache wenn vorhanden (de/fr), sonst Deutsch als Übersetzungsquelle."""
    if lang == "fr":
        return row[f"{field}_fr"], "fr"
    return row[f"{field}_de"], "de"


async def p_list(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    prog = repo.active_program(db, p["id"])
    rows = []
    if prog:
        done = repo.done_today(db, p["id"])
        for it in repo.items(db, prog["id"]):
            src, src_lang = _source(it, "name", request["lang"])
            rows.append({"it": it, "name": await _localized(request, src, src_lang), "done": it["id"] in done,
                         "dose": dose.text(request.app[I18N], request["lang"], it), "pic": drawings.picture(it)})
    reason = request.query.get("grund")
    return await render(request, "homeprogram/p_list.html", {
        "rows": rows, "prog": prog, "reasons": repo.SKIP_REASONS, "skip": repo.skip_today(db, p["id"]) if prog else None,
        "answer": reason if reason in repo.SKIP_REASONS else None, "has_essential": any(r["it"]["essential"] for r in rows),
        "done_all": bool(rows) and all(r["done"] for r in rows),
    })


async def p_detail(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    p = request["patient"]
    it = repo.item(db, int_param(request, "iid"))
    if it is None or it["patient_id"] != p["id"] or it["program_status"] != "active":
        raise web.HTTPNotFound()
    if request.method == "POST":
        form = await request.post()
        pain = int(form["pain"]) if str(form.get("pain", "")).isdigit() else None
        repo.log_done(db, p["id"], it["id"], pain)
        raise redirect_with_flash("/p/uebungen", "p.ex.logged")
    lang = request["lang"]
    name_src, nl = _source(it, "name", lang)
    text_src, tl = _source(it, "instructions", lang)
    return await render(request, "homeprogram/p_detail.html", {
        "it": it, "name": await _localized(request, name_src, nl), "text": await _localized(request, text_src, tl),
        "note": await _localized(request, it["note"], it["note_lang"]) if it["note"] else None,
        "dose": dose.text(request.app[I18N], lang, it), "picture": drawings.picture(it),
        "done": it["id"] in repo.done_today(db, p["id"]), "faces": FACES,
    })


def _patient_item(request: web.Request):
    it = repo.item(request.app[DB], int_param(request, "iid"))
    if it is None or it["patient_id"] != request["patient"]["id"] or it["program_status"] != "active":
        raise web.HTTPNotFound()
    return it


async def p_video(request: web.Request) -> web.StreamResponse:
    return _serve_video(_patient_item(request))


async def p_video_delete(request: web.Request) -> web.StreamResponse:
    """Die Patientin / der Patient darf das eigene Video jederzeit löschen."""
    db = request.app[DB]
    it = _patient_item(request)
    _delete_video_file(request, repo.clear_video(db, it["id"]))
    audit(db, "delete", "exercise_video", it["id"], it["patient_id"], {"by": "patient"})
    raise redirect_with_flash(f"/p/uebungen/{it['id']}", "p.ex.video_deleted")


async def p_skip(request: web.Request) -> web.StreamResponse:
    """Heute nicht geübt: Grund wählen (freiwillig). Die Praxis sieht ihn vor dem nächsten Termin."""
    form = await request.post()
    reason = str(form.get("reason") or "")
    if reason not in repo.SKIP_REASONS:
        raise redirect_with_flash("/p/uebungen", "p.ex.skip_choose")
    repo.log_skip(request.app[DB], request["patient"]["id"], reason, str(form.get("note") or ""))
    raise web.HTTPFound(f"/p/uebungen?grund={reason}")


def p_card(request: web.Request) -> dict | None:
    return repo.today_summary(request.app[DB], request["patient"]["id"])


def setup(ctx: ModuleContext) -> None:
    P = "/patienten/{pid:\\d+}/heimprogramm"
    ctx.page("therapie", P, patient_program)
    ctx.page("therapie", P, program_add, ("POST",))
    ctx.page("therapie", P + "/freigeben", program_approve, ("POST",))
    ctx.page("therapie", "/heimprogramm/{iid:\\d+}", item_update, ("POST",))
    ctx.page("therapie", "/heimprogramm/{iid:\\d+}/video", video_view)
    ctx.page("therapie", "/heimprogramm/{iid:\\d+}/video", video_upload, ("POST",))
    ctx.page("therapie", "/heimprogramm/{iid:\\d+}/video/loeschen", video_delete, ("POST",))
    ctx.job("video_convert", job_video_convert, background=True)   # bis 2 Min. Rechenzeit – eigener Arbeiter, hält kein Diktat auf
    ctx.page("therapie", "/uebungen", library_page)
    ctx.page("therapie", "/uebungen/neu", exercise_new, ("GET", "POST"))
    ctx.page("therapie", "/uebungen/{eid:\\d+}", exercise_edit, ("GET", "POST"))
    ctx.page("therapie", "/uebungen/{eid:\\d+}/aktiv", exercise_active, ("POST",))
    ctx.nav("therapie", NavItem("nav.exercises", "fitness_center", "/uebungen", order=80))
    ctx.slot("therapie.patient.right", 30, card_program, "homeprogram/card_program.html")
    ctx.slot("therapie.patient.tab", 45, tab_program)
    ctx.patient_page("/uebungen", p_list)
    ctx.patient_page("/uebungen/{iid:\\d+}", p_detail, ("GET", "POST"))
    ctx.patient_page("/uebungen/{iid:\\d+}/video", p_video)
    ctx.patient_page("/uebungen/{iid:\\d+}/video/loeschen", p_video_delete, ("POST",))
    ctx.patient_page("/uebungen/nicht-geschafft", p_skip, ("POST",))
    ctx.nav("patient", NavItem("p.nav.exercises", "fitness_center", "/uebungen", order=20))
    ctx.slot("patient.home.card", 20, p_card, "homeprogram/p_card.html")
    ctx.on_startup(seed_libraries)
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('programs', 'exercise_logs', 'exercise_skips'), (
        "UPDATE programs SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE exercise_logs SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "DELETE FROM exercise_skips WHERE practice_id = :pid AND patient_id = :drop AND EXISTS (SELECT 1 FROM "
        "exercise_skips k WHERE k.practice_id = :pid AND k.patient_id = :keep AND k.day = exercise_skips.day)",
        "UPDATE exercise_skips SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
