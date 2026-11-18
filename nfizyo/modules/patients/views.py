"""Patientenakte.

Therapie (/therapie):  «Meine Patient/innen» und die Akte (alles auf einen Blick: Körperkarte, Befunde, Einschränkungen,
                       Messwerte + Karten anderer Module), Stammdaten zum Nachschauen.
Empfang (/empfang):    alle Patient/innen, Aufnahme, Stammdaten, Verordnungen, Behandlungsteam – ohne medizinische Inhalte.
"""
from __future__ import annotations

import json
from datetime import date

from aiohttp import web

from ...core import access, formats, languages
from ...core import patients as core_patients
from ...core import sms as core_sms
from ...core.db import utcnow
from ...core.audit import audit
from ...core.events import publish
from ...core.charts import sparkline_svg
from ...core.i18n import PATIENT_LANGS
from ...core.keys import DB, I18N
from ...core.modules import ModuleContext, NavItem, enabled
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.slots import collect, patient_tabs
from ...core.tenant import current_practice_id
from ...core.web import int_param, redirect_with_flash, render
from ...knowledge import anatomy
from . import repo
from .api import file_page, header, require_patient
from ...core.timeutil import today as zurich_today

LAWS = ("KVG", "UVG", "IVG", "MVG", "VVG", "privat")
MEASURE_KINDS = ("flexion", "extension", "abduction", "external_rotation", "internal_rotation", "elevation", "pain_vas")


def _date_or_none(value) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _int_or_none(value) -> int | None:
    v = str(value if value is not None else "").strip()
    return int(v) if v.isdigit() and len(v) <= 9 else None     # nur kleine positive Zahlen (IDs) – nie ein Serverfehler


def patient_form_data(form, db=None) -> tuple[dict, list[str]]:
    """Stammdaten aus dem Formular des Empfangs (ohne medizinische Felder). Mit db werden Versicherer, Zuweisende und
    Therapeut/in gegen die Listen der Praxis geprüft."""
    insurer_ids = {r["id"] for r in core_patients.all_insurers(db)} if db is not None else None
    referrer_ids = {r["id"] for r in db.query("SELECT id FROM referrers WHERE practice_id = ?", (current_practice_id(),))} \
        if db is not None else None
    therapist_ids = {r["id"] for r in core_patients.therapists(db)} if db is not None else None
    errors = []
    data = {
        "first_name": str(form.get("first_name", "")).strip(),
        "last_name": str(form.get("last_name", "")).strip(),
        "birthdate": _date_or_none(form.get("birthdate")),
        "sex": form.get("sex") if form.get("sex") in ("f", "m", "x") else None,
        "lang": form.get("lang") if form.get("lang") in PATIENT_LANGS else "de",
        "spoken_lang": form.get("spoken_lang") if form.get("spoken_lang") in languages.BY_CODE else None,
        "needs_interpreter": form.get("needs_interpreter") == "1",
        "phone": str(form.get("phone", "")).strip() or None,
        "email": str(form.get("email", "")).strip() or None,
        "address": str(form.get("address", "")).strip() or None,
        "zip": str(form.get("zip", "")).strip() or None,
        "city": str(form.get("city", "")).strip() or None,
        "insurer_id": _int_or_none(form.get("insurer_id")),
        "law": form.get("law") if form.get("law") in LAWS else None,
        "insured_number": str(form.get("insured_number", "")).strip() or None,
        "ahv_number": None,
        "case_number": str(form.get("case_number", "")).strip() or None,
        "accident_date": _date_or_none(form.get("accident_date")),
        "employer": str(form.get("employer", "")).strip() or None,
        "referrer_id": _int_or_none(form.get("referrer_id")),
        "therapist_id": _int_or_none(form.get("therapist_id")),
        "admin_note": str(form.get("admin_note", "")).strip() or None,
        "email_ok_at": "keep" if form.get("email_ok") == "1" else None,
        "reminder_channel": form.get("reminder_channel") if form.get("reminder_channel") in core_patients.REMINDER_CHANNELS else "sms",
    }
    if not data["email"]:
        data["email_ok_at"] = None
        if data["reminder_channel"] in ("email", "both"):
            data["reminder_channel"] = "sms"
    if not data["first_name"]:
        errors.append("first_name")
    if not data["last_name"]:
        errors.append("last_name")
    if form.get("birthdate") and not data["birthdate"]:
        errors.append("birthdate")
    try:
        data["ahv_number"] = core_patients.clean_ahv(str(form.get("ahv_number", "")))
    except ValueError:
        errors.append("ahv_number")
        data["ahv_input"] = str(form.get("ahv_number", ""))[:20]
    if data["phone"]:
        # «79 555 66 77» (ohne 0) ist eine Schweizer Nummer → als 079 555 66 77 speichern; Unmögliches wird nicht gespeichert
        # (sonst ginge die SMS samt Einladungscode ins Ausland). Ausland nur ausdrücklich mit + oder 00.
        number, how = core_sms.classify_phone(data["phone"][:40])
        if number is None:
            errors.append("phone")
        elif how == "guessed":
            data["phone"] = formats.phone(number)
    for key, choices in (("insurer_id", insurer_ids), ("referrer_id", referrer_ids), ("therapist_id", therapist_ids)):
        if data[key] is not None and choices is not None and data[key] not in choices:
            data[key] = None                 # unbekannte Auswahl (z. B. gelöscht oder manipuliert) – nie ein Serverfehler
    return data, errors


def form_choices(db) -> dict:
    return {
        "insurers": core_patients.insurers(db), "referrers": core_patients.referrers(db), "therapists": core_patients.therapists(db),
        "laws": LAWS, "spoken_languages": languages.LANGUAGES, "patient_langs": PATIENT_LANGS,
    }


# =========================================================================== Therapie
async def my_patients(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    q = (request.query.get("q") or "").strip()
    sql, params = access.patient_ids_sql(request["user"]["id"])
    where, extra = "", []
    if q:
        like = f"%{q}%"
        where = " AND (p.first_name LIKE ? OR p.last_name LIKE ? OR (p.first_name || ' ' || p.last_name) LIKE ? OR p.birthdate LIKE ?)"
        extra = [like, like, like, like]
    rows = db.query(
        "SELECT p.*, i.name AS insurer_name, (t.user_id IS NULL) AS delegated FROM patients p"
        " LEFT JOIN insurers i ON i.id = p.insurer_id"
        " LEFT JOIN patient_team t ON t.patient_id = p.id AND t.practice_id = p.practice_id AND t.user_id = ?"
        f" WHERE p.practice_id = ? AND p.archived = 0 AND p.id IN ({sql}){where} ORDER BY p.last_name, p.first_name LIMIT 300",
        [request["user"]["id"], current_practice_id(), *params, *extra])
    return await render(request, "patients/t_list.html", {"patients": rows, "q": q, "age": core_patients.age,
                                                         "delegations": access.delegations_for(db, request["user"]["id"])})


async def patient_file(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = require_patient(request)
    audit(db, "view", "patient", patient["id"], patient["id"])
    columns = {c: await collect(request, f"therapie.patient.{c}", patient) for c in ("left", "center", "right")}
    ctx = await header(request, patient)
    ctx.update({"panels": columns, "tabs": await patient_tabs(request, patient, "overview"), "measure_kinds": MEASURE_KINDS})
    return await render(request, "patients/t_file.html", ctx)


async def patient_master_t(request: web.Request) -> web.StreamResponse:
    """Stammdaten zum Nachschauen; die Therapie pflegt nur das Therapieziel (in Worten der Patientin / des Patienten)."""
    db = request.app[DB]
    patient = require_patient(request)
    if request.method == "POST":
        form = await request.post()
        core_patients.update(db, patient["id"], {"goal": str(form.get("goal") or "").strip()[:300] or None})
        raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}/stammdaten", "common.saved")
    return await file_page(request, patient, "master", "patients/t_master.html", {
        "prescriptions": core_patients.prescriptions(db, patient["id"]), "team": access.team(db, patient["id"]),
        "age": core_patients.age(patient["birthdate"]),
    })


async def finding_add(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = require_patient(request)
    back = f"{prefix(request)}/patienten/{patient['id']}"
    form = await request.post()
    kind = form.get("kind", "finding")
    title = str(form.get("title", "")).strip()
    if kind not in repo.FINDING_KINDS or not title:
        raise redirect_with_flash(back, "patients.finding_invalid")
    structure_id = form.get("structure_id") or None
    region = form.get("region") or None
    side = form.get("side") if form.get("side") in ("l", "r", "b") else None
    bone = form.get("bone") or None
    levels = None
    if bone and not structure_id:
        st, bside = anatomy.bone_structure(bone)
        structure_id = st.id if st else None
        side = side or bside
        if bone in anatomy.VERTEBRAE:
            levels = [bone]
    if region and not side and region[-2:] in ("_l", "_r"):
        side = region[-1]
    repo.add_finding(db, patient["id"], kind=kind, title=title, detail=str(form.get("detail", "")).strip() or None,
                     structure_id=structure_id, region=region, side=side, spine_levels=levels,
                     severity=_int_or_none(form.get("severity")), on_date=_date_or_none(form.get("on_date")),
                     until=_date_or_none(form.get("until")))
    raise redirect_with_flash(back, "patients.finding_added")


def _back(request: web.Request, form, default: str) -> str:
    back = str(form.get("back") or "")
    return back if back.startswith(prefix(request) + "/") else default


async def finding_status(request: web.Request) -> web.StreamResponse:
    patient = require_patient(request)
    form = await request.post()
    status = "resolved" if form.get("status") == "resolved" else "active"
    repo.set_finding_status(request.app[DB], patient["id"], int_param(request, "fid"), status)
    raise redirect_with_flash(_back(request, form, f"{prefix(request)}/patienten/{patient['id']}"), "common.saved")


async def measurement_add(request: web.Request) -> web.StreamResponse:
    patient = require_patient(request)
    form = await request.post()
    kind = form.get("kind")
    try:
        value = float(str(form.get("value", "")).replace(",", "."))
    except ValueError:
        value = None
    if kind not in MEASURE_KINDS or value is None:
        raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}", "patients.measure_invalid")
    goal = None
    if str(form.get("goal", "")).strip():
        try:
            goal = float(str(form.get("goal")).replace(",", "."))
        except ValueError:
            goal = None
    repo.add_measurement(request.app[DB], patient["id"], kind, value, "/10" if kind == "pain_vas" else "°",
                         joint=form.get("joint") or None, side=form.get("side") if form.get("side") in ("l", "r") else None,
                         measured_on=_date_or_none(form.get("measured_on")), goal=goal)
    raise redirect_with_flash(_back(request, form, f"{prefix(request)}/patienten/{patient['id']}"), "patients.measure_added")


# --- Karten der Akte (Therapie)
async def card_bodymap(request: web.Request, patient) -> dict:
    return {"marks": repo.finding_marks(repo.findings(request.app[DB], patient["id"])), "patient": patient}


async def card_clinical(request: web.Request, patient) -> dict:
    rows = [f for f in repo.findings(request.app[DB], patient["id"])
            if f["kind"] in ("diagnosis", "surgery", "finding", "complication", "pain", "red_flag", "goal")]
    return {"rows": rows, "patient": patient}


async def card_restrictions(request: web.Request, patient) -> dict:
    today = zurich_today()
    rows = []
    for f in repo.findings(request.app[DB], patient["id"]):
        if f["kind"] not in ("restriction", "clearance", "red_flag"):
            continue
        item = dict(f)
        item["days_left"] = (date.fromisoformat(f["until"]) - today).days if f["until"] else None
        rows.append(item)
    active = [f for f in rows if not (f["kind"] == "restriction" and f["days_left"] is not None and f["days_left"] < 0)]
    expired = [f for f in rows if f["kind"] == "restriction" and f["days_left"] is not None and f["days_left"] < 0]
    return {"rows": active, "expired": expired, "patient": patient}


async def card_measurements(request: web.Request, patient) -> dict:
    series = []
    shared = repo.shares(request.app[DB], patient["id"])
    for s in repo.measurement_series(request.app[DB], patient["id"]):
        values = [v for _, v in s["points"]]
        series.append({**s, "svg": sparkline_svg(s["points"], s["goal"]), "last": values[-1], "first": values[0], "count": len(values),
                       "shared": (s["kind"], s["joint"] or "", s["side"] or "") in shared})
    return {"series": series, "patient": patient, "app_on": enabled(request, "patientapp")}


async def measurement_share(request: web.Request) -> web.StreamResponse:
    """Messreihe in der Patienten-App zeigen oder ausblenden (nur Zahlen und Ziel)."""
    patient = require_patient(request)
    form = await request.post()
    kind = form.get("kind")
    if kind not in MEASURE_KINDS:
        raise web.HTTPBadRequest()
    on = form.get("on") == "1"
    repo.set_share(request.app[DB], patient["id"], kind, str(form.get("joint") or "") or None,
                   form.get("side") if form.get("side") in ("l", "r") else None, on)
    raise redirect_with_flash(_back(request, form, f"{prefix(request)}/patienten/{patient['id']}"),
                              "patients.share_saved" if on else "patients.share_removed")


def p_progress(request: web.Request) -> dict | None:
    """Patienten-App: freigegebene Messreihen als kleiner Verlauf."""
    rows = []
    for s in repo.shared_series(request.app[DB], request["patient"]["id"]):
        values = [v for _, v in s["points"]]
        if len(values) < 2:
            continue
        rows.append({**s, "svg": sparkline_svg(s["points"], s["goal"], w=300, h=72), "first": values[0], "last": values[-1],
                     "first_on": s["points"][0][0], "last_on": s["points"][-1][0]})
    return {"rows": rows} if rows else None


def fact_age_insurance(request: web.Request, patient) -> str | None:
    parts = []
    a = core_patients.age(patient["birthdate"])
    if a is not None:
        parts.append(request.app[I18N].t(request["lang"], "patients.age_short", n=a))
    ins = " ".join(x for x in (patient["law"], patient["insurer_name"]) if x)
    if ins:
        parts.append(ins)
    return " · ".join(parts) or None


def tab_master(request: web.Request, patient) -> dict:
    return {"key": "master", "label_key": "patients.tab.master", "path": "stammdaten"}


def action_call(request: web.Request, patient) -> dict | None:
    if not patient["phone"]:
        return None
    return {"label_key": "patients.call", "icon": "call", "href": f"tel:{patient['phone']}", "icon_only": True}


# =========================================================================== Empfang
async def all_patients(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    q = request.query.get("q", "")
    archived = request.query.get("archiv") == "1"
    rows = core_patients.search(db, q, limit=300, archived=archived)
    return await render(request, "patients/r_list.html", {"patients": rows, "q": q, "archived": archived, "age": core_patients.age})


async def patient_new(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    values: dict = {"lang": "de"}
    errors: list[str] = []
    same: list = []
    if request.method == "POST":
        form = await request.post()
        values, errors = patient_form_data(form, db)
        if not errors and form.get("confirm_new") != "1" and values.get("first_name") and values.get("last_name"):
            # Schon da? (häufigster Fehler am Telefon: dieselbe Person ein zweites Mal anlegen) → zuerst zeigen, dann bewusst bestätigen
            same = core_patients.find_exact(db, values["first_name"], values["last_name"], values.get("birthdate") or None)
        if not errors and not same:
            if values.get("email_ok_at") == "keep":
                values["email_ok_at"] = utcnow()
            pid = core_patients.create(db, values)
            raise redirect_with_flash(f"{prefix(request)}/patienten/{pid}", "patients.created")
    return await render(request, "patients/r_form.html", {"p": values, "errors": errors, "same": same, **form_choices(db)})


async def patient_record(request: web.Request) -> web.StreamResponse:
    """Stammblatt des Empfangs: Daten, Verordnungen, Behandlungsteam + Karten anderer Module (Termine, App, Rechnungen)."""
    db = request.app[DB]
    patient = require_patient(request)
    errors: list[str] = []
    values = dict(patient)
    orig = {k: patient[k] for k in core_patients.PATIENT_FIELDS if k in patient.keys()}
    if request.method == "POST":
        form = await request.post()
        values, errors = patient_form_data(form, db)
        orig = _orig_values(form.get("_orig")) or orig
        if not errors:
            if values.get("email_ok_at") == "keep":
                values["email_ok_at"] = patient["email_ok_at"] if patient["email_ok_at"] and patient["email"] == values["email"] else utcnow()
            core_patients.update(db, patient["id"], changed_fields(values, _orig_values(form.get("_orig"))))
            raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}", "common.saved")
    agenda = _agenda(request)
    return await render(request, "patients/r_record.html", {
        "n_future": len(agenda.future_for_patient(db, patient["id"])) if agenda is not None and not patient["archived"] else 0,
        "patient": patient, "p": values, "errors": errors, "orig": json.dumps(orig, ensure_ascii=False),
        "prescriptions": core_patients.prescriptions(db, patient["id"]),
        "pr_kinds": core_patients.PRESCRIPTION_KINDS, "canton": db.value("SELECT canton FROM practices WHERE id = ?", (current_practice_id(),)),
        "team": access.team(db, patient["id"]), "cards": await collect(request, "empfang.patient.card", patient),
        "actions": [c["ctx"] for c in await collect(request, "empfang.patient.action", patient)], **form_choices(db),
    })


def _orig_values(raw) -> dict | None:
    """Stand der Stammdaten beim Öffnen des Formulars (verstecktes Feld _orig)."""
    try:
        data = json.loads(str(raw or ""))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def changed_fields(values: dict, orig: dict | None) -> dict:
    """Nur was diese Person im Formular wirklich geändert hat. Hat inzwischen jemand anderes (zweiter Empfang, Verordnung aus dem
    Posteingang) z. B. die Telefonnummer oder die Versicherung geändert, setzt ein älteres offenes Formular sie nicht still zurück."""
    if not orig:
        return values                      # ohne Ausgangsstand (alte Seite): wie bisher alles speichern
    return {k: v for k, v in values.items() if k not in orig or _norm(v) != _norm(orig[k])}


def _prescription_values(db, form) -> dict:
    sessions = _int_or_none(form.get("sessions")) or 9
    referrer = _int_or_none(form.get("referrer_id"))
    if referrer and not db.value("SELECT 1 FROM referrers WHERE id = ? AND practice_id = ?", (referrer, current_practice_id())):
        referrer = None
    kind = form.get("kind") if form.get("kind") in core_patients.PRESCRIPTION_KINDS else "physio"
    return {"sessions": max(1, min(sessions, 36)), "issued_on": _date_or_none(form.get("issued_on")), "referrer_id": referrer,
            "kind": kind, "diagnosis": str(form.get("diagnosis", "")).strip()[:300] or None}


async def prescription_add(request: web.Request) -> web.StreamResponse:
    patient = require_patient(request)
    form = await request.post()
    v = _prescription_values(request.app[DB], form)
    core_patients.add_prescription(request.app[DB], patient["id"], sessions=v["sessions"], issued_on=v["issued_on"],
                                   referrer_id=v["referrer_id"], diagnosis=v["diagnosis"], kind=v["kind"])
    raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}#verordnungen", "patients.prescription_added")


async def prescription_change(request: web.Request) -> web.StreamResponse:
    """Verordnung korrigieren (Tippfehler, protokolliert) oder beenden (aufgebraucht / abgelaufen) bzw. wieder aktiv setzen."""
    db = request.app[DB]
    patient = require_patient(request)
    pr = core_patients.prescription(db, int_param(request, "rid"))
    if pr is None or pr["patient_id"] != patient["id"]:
        raise web.HTTPNotFound()
    form = await request.post()
    back = f"{prefix(request)}/patienten/{patient['id']}#verordnungen"
    if form.get("do") == "status":
        ok = core_patients.set_prescription_status(db, pr["id"], str(form.get("status") or ""), reason=str(form.get("reason") or "").strip() or None)
        raise redirect_with_flash(back, "patients.pr_status_saved" if ok else "patients.pr_unchanged")
    v = _prescription_values(db, form)
    if "diagnosis" not in form:
        v.pop("diagnosis")
    changed = core_patients.update_prescription(db, pr["id"], v)
    raise redirect_with_flash(back, "patients.pr_corrected" if changed else "patients.pr_unchanged")


# =========================================================================== Leitung › Einstellungen › Doppelte Einträge
def _patient_ref(db, value):
    """Patient/in aus Nummer (P00012) oder ID – nur der eigenen Praxis."""
    value = str(value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return core_patients.get(db, int(value))
    row = db.one("SELECT id FROM patients WHERE practice_id = ? AND upper(number) = upper(?)", (current_practice_id(), value))
    return core_patients.get(db, row["id"]) if row else None


async def duplicates_page(request: web.Request) -> web.StreamResponse:
    """Nur Leitung: doppelte Einträge vergleichen und zusammenführen, leere Einträge löschen. Nur Stammdaten und Anzahlen,
    keine medizinischen Inhalte. Zusammenführen ist endgültig (Bestätigung, Protokoll)."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/duplikate"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "merge":
            keep, drop = _patient_ref(db, form.get("keep_id")), _patient_ref(db, form.get("drop_id"))
            if keep is None or drop is None:
                raise web.HTTPNotFound()
            if keep["id"] == drop["id"] or form.get("confirm") != "1":
                raise redirect_with_flash(f"{back}?behalten={keep['id']}&doppelt={drop['id']}", "patients.dup.confirm_needed")
            try:
                await core_patients.merge(request.app, drop, keep)
            except core_patients.MergeError:
                raise redirect_with_flash(f"{back}?behalten={keep['id']}&doppelt={drop['id']}", "patients.dup.merge_failed")
            raise redirect_with_flash(f"{back}?behalten={keep['id']}", "patients.dup.merged")
        if do == "delete":
            patient = _patient_ref(db, form.get("patient_id"))
            if patient is None:
                raise web.HTTPNotFound()
            if form.get("confirm") != "1":
                raise redirect_with_flash(f"{back}?doppelt={patient['id']}", "patients.dup.confirm_needed")
            if not await core_patients.delete_empty(request.app, patient):
                raise redirect_with_flash(f"{back}?doppelt={patient['id']}", "patients.dup.has_data")
            raise redirect_with_flash(back, "patients.dup.deleted")
        raise web.HTTPBadRequest()
    keep = _patient_ref(db, request.query.get("behalten"))
    drop = _patient_ref(db, request.query.get("doppelt"))
    q = request.query.get("q", "").strip()[:60]
    ctx = {"keep": keep, "drop": drop, "q": q, "found": core_patients.search(db, q, limit=20) + core_patients.search(db, q, limit=10, archived=True)
           if q else [], "candidates": core_patients.likely_duplicates(db) if not (keep or drop or q) else []}
    for side, p in (("keep", keep), ("drop", drop)):
        ctx[f"{side}_links"] = await core_patients.linked_counts(request.app, p["id"]) if p else {}
    ctx["fill"] = core_patients.merge_fill(keep, drop) if keep and drop and keep["id"] != drop["id"] else {}
    return await settings_page(request, "duplikate", "patients/s_duplicates.html", ctx)


async def team_change(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    patient = require_patient(request)
    form = await request.post()
    uid = _int_or_none(form.get("user_id"))
    if uid and db.value("SELECT 1 FROM users WHERE id = ? AND practice_id = ?", (uid, current_practice_id())):
        if form.get("do") == "remove":
            access.remove_from_team(db, patient["id"], uid)
        else:
            access.add_to_team(db, patient["id"], uid, "manual")
            audit(db, "team_add", "patient", patient["id"], patient["id"], {"user_id": uid})
    raise redirect_with_flash(f"{prefix(request)}/patienten/{patient['id']}#team", "common.saved")


def _agenda(request: web.Request):
    if not enabled(request, "agenda"):
        return None
    from ..agenda import api as agenda_api

    return agenda_api


async def patient_archive(request: web.Request) -> web.StreamResponse:
    """Archivieren. Hat die Person noch künftige Termine, zeigt eine Bestätigungsseite sie an und bietet an, sie abzusagen
    (die Person auf Wunsch per SMS informieren) – sonst blieben die Zeiten belegt und Erinnerungen gingen weiter hinaus."""
    db = request.app[DB]
    patient = require_patient(request)
    form = await request.post()
    archive = form.get("archive") == "1"
    S = prefix(request)
    flash = "patients.archived" if archive else "patients.unarchived"
    agenda = _agenda(request) if archive else None
    future = agenda.future_for_patient(db, patient["id"]) if agenda is not None else []
    if future and form.get("confirm") != "1":
        return await render(request, "patients/r_archive.html", {
            "patient": patient, "future": future, "can_sms": core_sms.normalize_phone(patient["phone"]) is not None})
    if future and form.get("cancel_future") == "1":
        cancelled = agenda.cancel_future(db, patient["id"], request.app[I18N].t(request["lang"], "patients.archive_reason"))
        for a in cancelled:
            await publish(request.app, "appointment.status", appointment_id=a["id"], patient_id=patient["id"], status="cancelled")
        if cancelled and form.get("notify") == "1":
            _notify_cancelled(request, patient, cancelled)
        flash = "patients.archived_cancelled"
    core_patients.set_archived(db, patient["id"], archive)
    raise redirect_with_flash(f"{S}/patienten" if archive else f"{S}/patienten/{patient['id']}", flash)


def _notify_cancelled(request: web.Request, patient, appts) -> None:
    """SMS an die Person (in ihrer Sprache): diese Termine sind abgesagt. Nur mit gültiger Nummer."""
    from ...core.keys import SMS
    from ...core.timeutil import fmt_date, fmt_time, from_iso

    if core_sms.normalize_phone(patient["phone"]) is None:
        return
    practice = request.app[DB].one("SELECT name, phone FROM practices WHERE id = ?", (current_practice_id(),))
    lang = patient["lang"] or "de"
    dates = ", ".join(f"{fmt_date(from_iso(a['starts_at']), lang, weekday=True, year=False)} {fmt_time(from_iso(a['starts_at']))}"
                      for a in appts[:6]) + (" …" if len(appts) > 6 else "")
    text = request.app[I18N].t(lang, "p.sms.cancelled", practice=practice["name"], phone=practice["phone"] or "", dates=dates)
    request.app[SMS].send(current_practice_id(), "sms", patient["phone"], text)


def setup(ctx: ModuleContext) -> None:
    P = "/patienten/{pid:\\d+}"
    # Therapie
    ctx.page("therapie", "/patienten", my_patients)
    ctx.page("therapie", P, patient_file)
    ctx.page("therapie", P + "/stammdaten", patient_master_t, ("GET", "POST"))
    ctx.page("therapie", P + "/befunde", finding_add, ("POST",))
    ctx.page("therapie", P + "/befunde/{fid:\\d+}/status", finding_status, ("POST",))
    ctx.page("therapie", P + "/messwerte", measurement_add, ("POST",))
    ctx.page("therapie", P + "/messwerte/teilen", measurement_share, ("POST",))
    ctx.nav("therapie", NavItem("nav.my_patients", "group", "/patienten", order=20, phone=True, short_key="nav.patients"))
    ctx.slot("therapie.patient.left", 10, card_bodymap, "patients/card_bodymap.html")
    ctx.slot("therapie.patient.center", 50, card_clinical, "patients/card_clinical.html")
    ctx.slot("therapie.patient.right", 10, card_restrictions, "patients/card_restrictions.html")
    ctx.slot("therapie.patient.right", 40, card_measurements, "patients/card_measurements.html")
    ctx.slot("therapie.patient.fact", 10, fact_age_insurance)
    ctx.slot("therapie.patient.tab", 90, tab_master)
    ctx.slot("therapie.patient.action", 90, action_call)
    # Patienten-App: freigegebener Verlauf
    ctx.slot("patient.home.card", 25, p_progress, "patients/p_progress.html")
    # Empfang
    ctx.page("empfang", "/patienten", all_patients)
    ctx.page("empfang", "/patienten/neu", patient_new, ("GET", "POST"))
    ctx.page("empfang", P, patient_record, ("GET", "POST"))
    ctx.page("empfang", P + "/verordnungen", prescription_add, ("POST",))
    ctx.page("empfang", P + "/verordnungen/{rid:\\d+}", prescription_change, ("POST",))
    # Leitung: doppelte Einträge zusammenführen oder leere löschen
    ctx.page("leitung", "/einstellungen/duplikate", duplicates_page, ("GET", "POST"))
    ctx.slot("leitung.settings", 69, lambda request: {"key": "duplikate", "label_key": "patients.dup.title", "path": "duplikate"})
    ctx.page("empfang", P + "/team", team_change, ("POST",))
    ctx.page("empfang", P + "/archivieren", patient_archive, ("POST",))
    ctx.nav("empfang", NavItem("nav.patients", "group", "/patienten", order=20))
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('findings', 'measurements', 'measurement_shares'), (
        "UPDATE findings SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE measurements SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "DELETE FROM measurement_shares WHERE practice_id = :pid AND patient_id = :drop AND EXISTS (SELECT 1 FROM "
        "measurement_shares k WHERE k.practice_id = :pid AND k.patient_id = :keep AND k.kind = "
        "measurement_shares.kind AND k.joint = measurement_shares.joint AND k.side = measurement_shares.side)",
        "UPDATE measurement_shares SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
