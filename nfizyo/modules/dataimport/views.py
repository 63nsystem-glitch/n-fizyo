"""Leitung › Datenübernahme: Datei hochladen → Spalten zuordnen → Probelauf → Übernahme → Fehlerliste."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
from pathlib import Path

from aiohttp import web

from ...core.audit import audit
from ...core.db import utcnow
from ...core.keys import DB, I18N, SETTINGS
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.screens import prefix
from ...core.spreadsheet import safe_row
from ...core.tenant import current_practice_id, current_user_id
from ...core.web import int_param, redirect_with_flash, render
from . import engine, reader

MAX_BYTES = 20 * 1024 * 1024
ALLOWED = (".csv", ".txt", ".xlsx")


def _agenda(request: web.Request):
    if not enabled(request, "agenda"):
        return None
    from ..agenda import api as agenda_api

    return agenda_api


def _billing(request: web.Request):
    if not enabled(request, "billing"):
        return None
    from ..billing import api as billing_api

    return billing_api


def _dir(request: web.Request) -> Path:
    d = request.app[SETTINGS].data_dir / "imports" / str(current_practice_id())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job(request: web.Request):
    row = request.app[DB].one("SELECT * FROM import_jobs WHERE id = ? AND practice_id = ?", (int_param(request, "jid"), current_practice_id()))
    if row is None:
        raise web.HTTPNotFound()
    return row


def _remove_file(row) -> None:
    if row["path"]:
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- Start: Art wählen, Datei hochladen
async def start(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    if request.method == "POST":
        form = await request.post()
        kind = str(form.get("kind") or "")
        field = form.get("file")
        if kind not in engine.KINDS or field is None or not hasattr(field, "file"):
            raise redirect_with_flash(f"{S}/import", "dataimport.invalid")
        if kind == "appointments" and _agenda(request) is None:
            raise redirect_with_flash(f"{S}/import", "dataimport.no_agenda")
        if kind == "balances" and _billing(request) is None:
            raise redirect_with_flash(f"{S}/import", "dataimport.no_billing")
        name = Path(field.filename or "import.csv").name
        suffix = Path(name).suffix.lower()
        data = field.file.read(MAX_BYTES + 1)
        if suffix not in ALLOWED or not data:
            raise redirect_with_flash(f"{S}/import", "dataimport.bad_type")
        if len(data) > MAX_BYTES:
            raise redirect_with_flash(f"{S}/import", "dataimport.too_big")
        path = _dir(request) / f"{secrets.token_hex(12)}{'.xlsx' if suffix == '.xlsx' else '.csv'}"
        path.write_bytes(data)
        path.chmod(0o600)
        try:
            headers, rows = await asyncio.to_thread(reader.read, path)      # im Hintergrund: der Server bleibt bedienbar
        except reader.ReadError as exc:
            path.unlink(missing_ok=True)
            raise redirect_with_flash(f"{S}/import", f"dataimport.read.{exc.args[0] if exc.args else 'xlsx'}")
        mapping = engine.guess_mapping(kind, headers)
        jid = db.execute("INSERT INTO import_jobs (practice_id, kind, filename, path, rows, mapping, created_by, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?)", (current_practice_id(), kind, name[:200], str(path), len(rows),
                                                       json.dumps(mapping), current_user_id(), utcnow()))
        audit(db, "upload", "import", jid, None, {"kind": kind, "rows": len(rows)})
        raise web.HTTPFound(f"{S}/import/{jid}")
    jobs = db.query("SELECT j.*, u.name AS user_name FROM import_jobs j LEFT JOIN users u ON u.id = j.created_by"
                    " WHERE j.practice_id = ? ORDER BY j.id DESC LIMIT 20", (current_practice_id(),))
    return await render(request, "dataimport/start.html", {
        "kinds": engine.KINDS, "jobs": [dict(j) | {"res": json.loads(j["result"] or "{}")} for j in jobs],
        "fields": engine.FIELDS, "agenda_on": _agenda(request) is not None, "billing_on": _billing(request) is not None})


# --------------------------------------------------------------------------- Zuordnen, Probelauf, Übernahme
async def job_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    job = _job(request)
    here = f"{S}/import/{job['id']}"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if job["status"] != "draft":
            raise web.HTTPFound(here)
        if do == "discard":
            _remove_file(job)
            db.execute("UPDATE import_jobs SET status = 'discarded', path = NULL, preview = NULL WHERE id = ?", (job["id"],))
            audit(db, "discard", "import", job["id"])
            raise redirect_with_flash(f"{S}/import", "dataimport.discarded")
        headers, rows = await _read(job)
        if do in ("map", "run"):
            mapping = {}
            for f in engine.FIELDS[job["kind"]]:
                v = str(form.get(f"f_{f.key}") or "")
                if v.isdigit() and len(v) <= 3 and int(v) < len(headers):
                    mapping[f.key] = int(v)
            if do == "run":
                if not job["preview"]:
                    raise redirect_with_flash(here, "dataimport.check_first")
                mapping = json.loads(job["mapping"] or "{}")
            missing = [f.key for f in engine.FIELDS[job["kind"]] if f.required and f.key not in mapping]
            if missing:
                db.execute("UPDATE import_jobs SET mapping = ?, preview = NULL WHERE id = ?", (json.dumps(mapping), job["id"]))
                raise redirect_with_flash(here, "dataimport.map_required")
            checked = engine.check(db, job["kind"], rows, mapping, _agenda(request), _billing(request))
            if do == "map":
                db.execute("UPDATE import_jobs SET mapping = ?, preview = ? WHERE id = ?",
                           (json.dumps(mapping), json.dumps(_preview(checked)), job["id"]))
                raise web.HTTPFound(here + "#probelauf")
            options = {"billed_externally": form.get("billed_externally") == "1"} if job["kind"] == "appointments" else {}
            result = engine.apply(db, job["kind"], checked, _agenda(request), _billing(request), options)
            res = {**engine.summary(checked), **result, "issues": _issues(checked)}
            _remove_file(job)
            db.execute("UPDATE import_jobs SET status = 'done', path = NULL, result = ?, done_at = ? WHERE id = ?",
                       (json.dumps(res), utcnow(), job["id"]))
            audit(db, "import", job["kind"], job["id"], None, {"created": result["created"], "rows": len(rows)})
            raise redirect_with_flash(here, "dataimport.done")
        raise web.HTTPFound(here)

    ctx = {"job": job, "fields": engine.FIELDS[job["kind"]], "mapping": json.loads(job["mapping"] or "{}")}
    if job["status"] == "draft" and job["path"]:
        headers, rows = await _read(job)
        ctx.update({"headers": headers, "sample": rows[:5], "preview": json.loads(job["preview"]) if job["preview"] else None})
    else:
        ctx["result"] = json.loads(job["result"] or "{}")
    return await render(request, "dataimport/job.html", ctx)


async def _read(job) -> tuple[list[str], list[list[str]]]:
    """Hochgeladene Datei lesen – in einem eigenen Thread (grosse Dateien blockieren den Server nicht)."""
    try:
        return await asyncio.to_thread(reader.read, Path(job["path"]))
    except (reader.ReadError, OSError):
        raise web.HTTPBadRequest(text="Datei nicht lesbar")


def _preview(checked: list[engine.Row]) -> dict:
    s = engine.summary(checked)
    s["rows"] = [{"line": r.line, "status": r.status, "notes": r.notes, "label": _label(r)} for r in checked[:30]]
    s["issues"] = _issues(checked)[:200]
    return s


def _label(r: engine.Row) -> str:
    v = r.values
    if "_label" in v:
        return v["_label"]
    if "last_name" in v:
        return f"{v.get('last_name')} {v.get('first_name')}" + (f", {v['birthdate']}" if v.get("birthdate") else "")
    if "name" in v:
        return v["name"]
    if "start" in v:
        return v["start"].strftime("%d.%m.%Y %H:%M")
    return ""


def _issues(checked: list[engine.Row]) -> list[dict]:
    return [{"line": r.line, "status": r.status, "notes": r.notes} for r in checked if r.status != "new" or r.notes]


async def issues_csv(request: web.Request) -> web.StreamResponse:
    """Fehler- und Hinweisliste als CSV (Zeile, Ergebnis, Hinweis) – ohne Personendaten."""
    job = _job(request)
    data = json.loads(job["result"] or job["preview"] or "{}")
    i18n = request.app[I18N]
    lang = request["lang"]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([i18n.t(lang, "dataimport.line"), i18n.t(lang, "dataimport.result"), i18n.t(lang, "dataimport.hint")])
    for it in data.get("issues", []):
        w.writerow(safe_row([it["line"], i18n.t(lang, f"dataimport.st.{it['status']}"),
                             ", ".join(i18n.t(lang, f"dataimport.note.{n}") for n in it["notes"])]))
    return web.Response(body=("﻿" + buf.getvalue()).encode("utf-8"), content_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="import-{job["id"]}-hinweise.csv"'})


async def cleanup(app: web.Application) -> None:
    """Liegengebliebene Dateien (Entwurf älter als 2 Tage) löschen – Patientendaten nicht länger als nötig aufbewahren."""
    from datetime import datetime, timedelta, timezone

    db = app[DB]
    limit = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat()
    for row in db.query("SELECT id, path FROM import_jobs WHERE status = 'draft' AND path IS NOT NULL AND created_at < ?", (limit,)):
        _remove_file(row)
        db.execute("UPDATE import_jobs SET status = 'discarded', path = NULL, preview = NULL WHERE id = ?", (row["id"],))


def setup(ctx: ModuleContext) -> None:
    ctx.page("leitung", "/import", start, ("GET", "POST"))
    ctx.page("leitung", "/import/{jid:\\d+}", job_page, ("GET", "POST"))
    ctx.page("leitung", "/import/{jid:\\d+}/hinweise.csv", issues_csv)
    ctx.nav("leitung", NavItem("dataimport.nav", "upload_file", "/import", order=85))
    ctx.on_startup(cleanup)
