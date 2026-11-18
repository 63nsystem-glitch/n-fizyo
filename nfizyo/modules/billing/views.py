"""Abrechnung: erledigte Termine → Rechnungsentwurf → abschliessen → PDF mit QR-Zahlteil / XML 5.0 → Zahlung → Mahnung.

Empfang (/empfang):  bereitet vor – Entwürfe erstellen und bearbeiten, PDF drucken, Zahlungen erfassen (auch Bankdatei
                     camt.054), Mahnungen erstellen.
Leitung (/leitung):  kontrolliert – Entwürfe abschliessen (Nummer, unveränderbar), stornieren, elektronisch senden
                     (XML 5.0 über MediData), Einstellungen › Tarife und › Zahlung.
"""
from __future__ import annotations

import asyncio
import math
from datetime import date

from aiohttp import web

from ...core import formats
from ...core import patients as core_patients
from ...core.audit import audit
from ...core.i18n import PATIENT_LANGS
from ...core.keys import DB, I18N, MAIL, SETTINGS
from ...core.timeutil import fmt_date, today
from ...core.modules import ModuleContext, NavItem
from ...core.pages import settings_page
from ...core.screens import prefix
from ...core.tenant import current_practice_id, current_user_id, practice_scope
from ...core.web import int_param, redirect_with_flash, render, form_int
from ...knowledge import tariffs as catalog
from . import camt, followup, medidata, medidata_cert, medidata_sync, payment, private, repo, response, simulator, xml500
from .medidata import MediDataClient
from ...core.timeutil import today as zurich_today


def _load(request: web.Request):
    inv = repo.get(request.app[DB], int_param(request, "iid"))
    if inv is None:
        raise web.HTTPNotFound()
    return inv


def _float(v, default: float | None = None) -> float | None:
    try:
        f = float(str(v).replace(",", ".").strip()[:30])
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _id(value) -> int | None:
    """ID aus einem Formular – None bei Unsinn (5000 Ziffern: int() hätte eine Ausnahme, also 500)."""
    v = str(value or "").strip()
    return int(v) if v.isdigit() and len(v) <= 12 else None


def _cash_day_closed(request: web.Request, method: str, day: str) -> bool:
    """Bar, Twint, Karte auf einen schon abgeschlossenen Kassentag? (Bank zählt nicht zur Kasse.)"""
    from .api import cash_day_closed

    return method != "bank" and cash_day_closed(request.app, request.app[DB], day)


def _day_or_today(value) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return today().isoformat()


def is_leitung(request: web.Request) -> bool:
    return request["screen"].key == "leitung"


# --------------------------------------------------------------------------- Übersicht (Empfang und Leitung)
TABS = ("bereit", "entwuerfe", "abgeschlossen", "offen", "zahlungen", "antworten")
WORK_TABS = ("bereit", "entwuerfe", "offen", "zahlungen", "antworten")   # «abgeschlossen» ist Archiv, keine Arbeit


def default_tab(counts: dict) -> str:
    """Erster Arbeits-Reiter mit Inhalt (meist «Bereit»), sonst «Bereit»."""
    return next((k for k in WORK_TABS if counts.get(k)), "bereit")


async def overview(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "create" and str(form.get("patient_id", "")).isdigit():
            patient = core_patients.get(db, int(form["patient_id"]))
            if patient is None:
                raise web.HTTPNotFound()
            iid = repo.create_from_appointments(db, patient, request["lang"], private_=form.get("private") == "1")
            if iid is None:
                raise redirect_with_flash(f"{S}/abrechnung", "billing.cannot_create")
            raise web.HTTPFound(f"{S}/rechnungen/{iid}")
        if form.get("do") == "assign" and str(form.get("payment_id", "")).isdigit():
            number = str(form.get("number") or "").strip()
            inv = db.one("SELECT id FROM invoices WHERE practice_id = ? AND number = ?", (current_practice_id(), number))
            if inv is None or not repo.assign_payment(db, int(form["payment_id"]), inv["id"]):
                raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.pay.assign_failed")
            raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.pay.assigned")
        if form.get("do") in ("dismiss", "undismiss", "unassign") and str(form.get("payment_id", "")).isdigit():
            pay_id, reason = int(form["payment_id"]), str(form.get("reason") or "")
            ok = {"dismiss": lambda: repo.dismiss_payment(db, pay_id, reason), "undismiss": lambda: repo.undismiss_payment(db, pay_id),
                  "unassign": lambda: repo.unassign_payment(db, pay_id, reason)}[str(form["do"])]()
            raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", f"billing.pay.{form['do']}_ok" if ok else "billing.pay.change_failed")
        if form.get("do") == "resp_assign" and str(form.get("response_id", "")).isdigit():
            ok = followup.assign_response(db, int(form["response_id"]), str(form.get("number") or ""))
            raise redirect_with_flash(f"{S}/abrechnung?ansicht=antworten", "billing.resp.assigned" if ok else "billing.resp.assign_failed")
        if form.get("do") == "resp_handled" and str(form.get("response_id", "")).isdigit():
            followup.mark_handled(db, int(form["response_id"]), str(form.get("note") or "").strip())
            raise redirect_with_flash(f"{S}/abrechnung?ansicht=antworten", "billing.resp.handled")
        raise redirect_with_flash(f"{S}/abrechnung", "billing.invalid")
    ready_rows = repo.ready(db)
    open_items = repo.open_items(db)
    unmatched = repo.unmatched_payments(db)
    counts = {"bereit": len(ready_rows), "entwuerfe": repo.open_total(db)["n"],
              "abgeschlossen": db.value("SELECT COUNT(*) FROM invoices WHERE practice_id = ? AND status != 'draft'", (current_practice_id(),), 0),
              "offen": len(open_items), "zahlungen": len(unmatched), "antworten": followup.count_open_rejections(db)}
    tab = request.query.get("ansicht") if request.query.get("ansicht") in TABS else default_tab(counts)
    kvg_now = catalog.tariff_for_law("KVG", today().isoformat())
    ctx = {"tab": tab, "kvg_value": repo.tp_value(db, kvg_now.key if kvg_now else "kvg312"), "open": repo.open_total(db), "leitung": is_leitung(request),
           "pay_problems": repo.payment_problems(db), "open_responses": counts["antworten"], "counts": counts, "tabs": TABS}
    if tab == "entwuerfe":
        ctx["invoices"] = repo.listing(db, "draft")
    elif tab == "abgeschlossen":
        ctx["invoices"] = [i for i in repo.listing(db, None) if i["status"] != "draft"]
        ctx["balances"] = {i["id"]: repo.balance(db, i) for i in ctx["invoices"]}
        ctx["md_states"] = medidata_sync.latest_states(db, [i["id"] for i in ctx["invoices"]])
    elif tab == "offen":
        ctx["items"] = open_items
        ctx["open_sum"] = round(sum(x["b"]["open"] for x in ctx["items"]), 2)
    elif tab == "antworten":
        ctx["responses"] = followup.open_responses(db)
        ctx["handled"] = followup.recent_handled(db)
    elif tab == "zahlungen":
        ctx["unmatched"] = unmatched
        ctx["dismissed"] = repo.dismissed_payments(db)
        ctx["imports"] = db.query("SELECT * FROM payment_imports WHERE practice_id = ? ORDER BY id DESC LIMIT 10", (current_practice_id(),))
    else:
        ctx["ready"] = [{"r": r, "tariff": repo.tariff_key(r["law"]), "billable": repo.tariff_key(r["law"]) is not None,
                         "private_alt": repo.tariff_key(r["law"]) not in (None, private.PRIVATE)} for r in ready_rows]
        ctx["has_prices"] = bool(private.items(db, ("service",)))
    return await render(request, "billing/overview.html", ctx)


# --------------------------------------------------------------------------- Rechnung
async def invoice_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    inv = _load(request)
    if inv["status"] == "draft":                   # Stammdaten nachgeführt (z. B. Schadennummer)? Dann gilt das sofort (F22)
        inv = repo.refresh_draft(db, inv)
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        inv = repo.get(db, inv["id"]) or inv          # Stand nach dem Hochladen des Formulars (z. B. inzwischen abgeschlossen)
        if inv["status"] == "draft":
            if do == "line" and str(form.get("line_id", "")).isdigit():
                repo.update_line(db, inv, int(form["line_id"]), code=str(form.get("code") or ""),
                                 quantity=_float(form.get("quantity"), 0) or 0, lang=request["lang"])
            elif do == "remove" and str(form.get("line_id", "")).isdigit():
                repo.remove_line(db, inv, int(form["line_id"]))
            elif do == "add":
                repo.add_line(db, inv, code=str(form.get("code") or ""), quantity=_float(form.get("quantity"), 1) or 1,
                              service_date=str(form.get("service_date") or zurich_today().isoformat()), lang=request["lang"],
                              chf=_float(form.get("chf"), 0) or None, detail=str(form.get("detail") or "").strip() or None)
            elif do == "finalize" and is_leitung(request):
                inv = repo.get(db, inv["id"])
                if repo.problems(db, inv, repo.lines(db, inv["id"])):
                    raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.fix_first")
                repo.finalize(db, inv)
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.finalized")
            elif do == "delete":
                repo.delete_draft(db, inv)
                raise redirect_with_flash(f"{S}/abrechnung?ansicht=entwuerfe", "billing.deleted")
        elif inv["status"] == "final" and do == "cancel" and is_leitung(request):
            reason = str(form.get("reason") or "").strip()[:300]
            if reason:
                repo.cancel(db, inv, reason)
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.cancelled")
        elif inv["status"] == "final" and do == "payment":
            amount = _float(form.get("amount"))
            booked = _day_or_today(form.get("booked_on"))
            method = str(form.get("method") or "bank")
            if not amount or amount <= 0 or amount > 100000:
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.invalid")
            if _cash_day_closed(request, method, booked):       # Tagesabschluss der Kasse bleibt unverändert (auch rückdatiert)
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.pay.day_closed")
            if repo.add_manual_payment(db, inv, amount=amount, booked_on=booked, method=method,
                                       note=str(form.get("note") or "").strip()[:200] or None) is None:
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.invalid")
            raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.pay.saved")
        elif inv["status"] != "draft" and do in ("pay_reverse", "pay_unassign") and _id(form.get("payment_id")):
            pm = repo.payment_row(db, _id(form.get("payment_id")))
            if pm is None or pm["invoice_id"] != inv["id"]:
                raise web.HTTPNotFound()
            reason = str(form.get("reason") or "")
            if do == "pay_reverse" and _cash_day_closed(request, pm["method"], today().isoformat()):
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.pay.day_closed")
            if do == "pay_reverse":
                ok = is_leitung(request) and repo.reverse_payment(db, pm["id"], reason, today().isoformat()) is not None
            else:
                ok = repo.unassign_payment(db, pm["id"], reason)
            raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", ("billing.pay.reversed" if do == "pay_reverse" else "billing.pay.unassign_ok")
                                      if ok else "billing.pay.change_failed")
        elif inv["status"] != "draft" and do == "refund" and is_leitung(request):
            amount = _float(form.get("amount"))
            day = _day_or_today(form.get("booked_on"))
            method = str(form.get("method") or "")
            if _cash_day_closed(request, method, day):
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.pay.day_closed")
            ok = amount is not None and repo.refund_payment(db, inv, amount=amount, method=method, day=day,
                                                           reason=str(form.get("reason") or "")) is not None
            raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.pay.refunded" if ok else "billing.pay.change_failed")
        elif inv["status"] != "draft" and do == "tr_mark" and is_leitung(request) and str(form.get("tid", "")).isdigit():
            t = repo.transmission(db, int(form["tid"]))
            if t is None or t["invoice_id"] != inv["id"]:
                raise web.HTTPNotFound()
            ok = repo.mark_transmission(db, t["id"], str(form.get("status") or ""))
            raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.xml.marked" if ok else "billing.invalid")
        elif inv["status"] == "final" and do == "reminder" and str(form.get("level", "")).isdigit():
            if repo.create_reminder(db, inv, int(form["level"])) is None:
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.rem.not_due")
            raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.rem.created")
        elif inv["status"] in ("final", "cancelled") and do in ("xml", "send") and is_leitung(request):
            await _electronic(request, inv, form, send=(do == "send"))
        elif inv["status"] != "draft" and do == "resp_handled" and str(form.get("response_id", "")).isdigit():
            r = followup.get_response(db, int(form["response_id"]))
            if r is not None and r["invoice_id"] == inv["id"]:
                followup.mark_handled(db, r["id"], str(form.get("note") or "").strip())
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.resp.handled")
        elif inv["status"] == "final" and do == "email":
            ok = await _send_by_email(request, inv)
            raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.mail.sent" if ok else "billing.mail.not_allowed")
        elif inv["status"] == "final" and do == "correct" and is_leitung(request):
            reason = str(form.get("reason") or "").strip()[:300]
            new = followup.copy_to_draft(db, inv, reason) if reason else None
            if new is None:
                raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.invalid")
            raise redirect_with_flash(f"{S}/rechnungen/{new}", "billing.resp.corrected")
        raise web.HTTPFound(f"{S}/rechnungen/{inv['id']}")
    lns = repo.lines(db, inv["id"])
    priv = repo.is_private(inv)
    xml_missing = xml500.missing(repo.xml_data(db, inv)) if inv["status"] != "draft" and not priv else []
    md = MediDataClient(request.app[SETTINGS])
    if priv:
        positions = [{"code": it["code"], "label_de": it["name_de"], "label_fr": it["name_fr"], "price": it["price_chf"]}
                     for it in private.items(db, ("service", "product"))]
    else:
        positions = catalog.positions(inv["tariff"])
    return await render(request, "billing/invoice.html", {
        "inv": inv, "lines": lns, "problems": repo.problems(db, inv, lns) if inv["status"] == "draft" else [],
        "hints": repo.warnings(db, inv, lns) if inv["status"] == "draft" else [],
        "private": priv, "vat": private.vat_summary(lns), "methods": repo.PAYMENT_METHODS,
        "positions": positions, "tariff": catalog.TARIFFS.get(inv["tariff"]), "can_write": True,
        "can_finalize": is_leitung(request), "today": zurich_today().isoformat(),
        "b": repo.balance(db, inv), "payments": repo.payments_for(db, inv["id"]), "reminders": repo.reminders_for(db, inv["id"]),
        "next_reminder": repo.next_reminder(db, inv), "transmissions": repo.transmissions_for(db, inv["id"]),
        "xml_missing": xml_missing, "medidata": medidata_sync.active_here(db, request.app[SETTINGS]), "medidata_mode": md.mode,
        "md_check": _md_check(db, inv) if not priv and inv["status"] != "draft" else None,
        "md_notes": medidata_sync.notifications(db, invoice_id=inv["id"]), "md_codes": _code_texts(request),
        "xml_kinds": _xml_kinds(inv, repo.reminders_for(db, inv["id"]), payment.settings(db)),
        "ref": pdf_ref(inv), "warnings": followup.warnings(db, inv), "responses": followup.responses_for(db, inv["id"]),
        "line_labels": {i + 1: ln["label"] for i, ln in enumerate(lns)},
        "email_ok": core_patients.email_allowed(core_patients.get(db, inv["patient_id"])),
        "emailed": db.value("SELECT MAX(at) FROM audit_log WHERE practice_id = ? AND action = 'email' AND entity = 'invoice'"
                            " AND entity_id = ?", (current_practice_id(), inv["id"])),
    })


def _md_check(db, inv) -> dict | None:
    """Vor dem Senden anzeigen: ist der Versicherer bei MediData erreichbar, nimmt er XML 5.0 (und Tiers garant) an?"""
    insurer = core_patients.insurer(db, inv["insurer_id"]) if inv["insurer_id"] else None
    if insurer is None:
        return None
    problems, reach = medidata_sync.recipient_problems(db, insurer["gln"], tiers=inv["tiers"], size=0)
    return {"reach": reach, "problems": problems, "directory": medidata_sync.directory_info(db)}


def _code_texts(request: web.Request) -> dict:
    """MediData-Fehlercodes → Klartext (de/fr), nur die bekannten."""
    i18n, lang = request.app[I18N], request["lang"]
    out = {c: i18n.t(lang, medidata.code_key(c)) for c in medidata.KNOWN_CODES}
    return out


async def _send_by_email(request: web.Request, inv) -> bool:
    """Rechnung als PDF per E-Mail an die Patientin / den Patienten (nur mit Einverständnis, nur Tiers garant/Selbstzahler)."""
    db = request.app[DB]
    patient = core_patients.get(db, inv["patient_id"])
    if not core_patients.email_allowed(patient) or inv["tiers"] != "garant":
        return False
    practice = repo.practice_row(db)
    lang = _pdf_lang(patient, practice) if patient["lang"] not in PATIENT_LANGS else patient["lang"]
    data = await asyncio.to_thread(_pdf, request, inv)
    i18n = request.app[I18N]
    params = {"name": patient["first_name"], "number": inv["number"], "amount": formats.money(inv['total_chf']),
              "due": fmt_date(inv["due_on"], lang) if inv["due_on"] else "", "practice": practice["name"], "phone": practice["phone"] or ""}
    body = i18n.t(lang, "p.mail.invoice_body", **params) + "\n\n" + i18n.t(lang, "p.mail.footer", **params)
    request.app[MAIL].send(practice["id"], patient["email"], i18n.t(lang, "p.mail.invoice_subject", **params), body,
                           attachment=(f"rechnung-{inv['number']}.pdf", data))
    audit(db, "email", "invoice", inv["id"], inv["patient_id"], {"number": inv["number"]})
    return True


def pdf_ref(inv) -> str:
    from ...core.pdf import format_reference

    return format_reference(inv["qr_reference"]) if inv["qr_reference"] else ""


def _xml_kinds(inv, reminders, st) -> list[dict]:
    """Welche elektronischen Dokumente zu dieser Rechnung möglich sind (Selbstzahler: keine)."""
    if repo.is_private(inv):
        return []
    if inv["status"] == "cancelled":
        return [{"kind": "storno", "label_key": "billing.xml.storno"}]
    out = []
    if inv["tiers"] == "payant":
        out.append({"kind": "invoice", "label_key": "billing.xml.invoice"})
    elif st["copy_to_insurer"]:
        out.append({"kind": "copy", "label_key": "billing.xml.copy"})
    if inv["tiers"] == "payant":
        for r in reminders:
            out.append({"kind": "reminder", "level": r["level"], "label_key": f"billing.xml.reminder_{r['level']}"})
    return out


async def _electronic(request: web.Request, inv, form, *, send: bool) -> None:
    """XML 5.0 erzeugen (und prüfen); auf Wunsch über MediData senden. Fehler werden gespeichert und angezeigt."""
    db = request.app[DB]
    S = prefix(request)
    kind = str(form.get("kind") or "")
    level = form_int(form, "level")
    allowed = {(k["kind"], k.get("level")) for k in _xml_kinds(inv, repo.reminders_for(db, inv["id"]), payment.settings(db))}
    if (kind, level) not in allowed:
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.invalid")
    data = repo.xml_data(db, inv)
    st = payment.settings(db)
    md = MediDataClient(request.app[SETTINGS])
    modus = "production" if (st["xml_modus"] == "production" and md.mode == "production") else "test"
    reminder = None
    if kind == "reminder":
        r = next(r for r in repo.reminders_for(db, inv["id"]) if r["level"] == level)
        reminder = {"level": level, "date": r["created_at"], "fee": r["fee_chf"], "open": r["open_chf"]}
    # Empfänger-GLN (transport@to) laut Teilnehmerverzeichnis: glnReceiver (Schnittstellendokumentation V5.2.0, 3.6.2)
    reach = medidata_sync.reach(db, data.insurer.gln if data.insurer else None)
    to_gln = reach["receiver"] if reach and reach.get("listed") else None
    try:
        xml, guid = xml500.build(data, modus=modus, kind=kind, reminder=reminder, to_gln=to_gln)
    except xml500.XmlInvalid as exc:
        audit(db, "xml_invalid", "invoice", inv["id"], inv["patient_id"], {"error": str(exc)[:300]})
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.xml.invalid")
    if not send:
        repo.add_transmission(db, inv, kind=kind, guid=guid, modus=modus, channel="file", status="created", xml=xml, reminder_level=level)
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.xml.created")
    if not medidata_sync.active_here(db, request.app[SETTINGS]):
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.xml.no_medidata")
    problems, _reach = medidata_sync.recipient_problems(db, data.insurer.gln if data.insurer else None, tiers=inv["tiers"],
                                                        size=len(xml))
    if problems:                                  # MediData würde ablehnen – vorher klar sagen, nichts senden
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", f"billing.md.{problems[0]}")
    earlier = {t["status"] for t in repo.transmissions_for(db, inv["id"])
               if t["channel"] == "medidata" and t["kind"] == kind and t["reminder_level"] == level and t["modus"] == modus}
    if "unknown" in earlier:                     # erst klären: vielleicht schon angekommen
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.xml.unclear_first")
    if "sent" in earlier and form.get("again") != "1":
        raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", "billing.xml.already_sent")
    info = md.info(modus=modus, document_reference=guid, correlation_reference=data.number)
    result = await md.upload(xml, f"{inv['number'] or inv['id']}_{kind}.xml", info)
    repo.add_transmission(db, inv, kind=kind, guid=guid, modus=modus, channel="medidata", status=result.status,
                          xml=xml, reminder_level=level, reference=result.reference, error=result.error, document_reference=guid,
                          correlation_reference=data.number, error_code=result.error_code, resolution=result.resolution)
    raise redirect_with_flash(f"{S}/rechnungen/{inv['id']}", {"sent": "billing.xml.sent", "unknown": "billing.xml.send_unclear"}.get(
        result.status, "billing.xml.send_failed"))


def _tl(request: web.Request, lang: str):
    i18n = request.app[I18N]
    return lambda key, **kw: i18n.t(lang, key, **kw)


def _pdf_lang(patient, practice) -> str:
    return patient["lang"] if patient["lang"] in ("de", "fr") else (practice["default_lang"] if practice["default_lang"] in ("de", "fr") else "de")


def _pdf(request: web.Request, inv, reminder_row=None) -> bytes:
    from . import pdf as invoice_pdf

    db = request.app[DB]
    practice = repo.practice_row(db)
    patient = core_patients.get(db, inv["patient_id"])
    insurer = core_patients.insurer(db, inv["insurer_id"]) if inv["insurer_id"] else None
    therapists = {u["id"]: u for u in db.query("SELECT id, name, gln, zsr FROM users WHERE practice_id = ?", (current_practice_id(),))}
    lang = _pdf_lang(patient, practice)
    reminder, open_amount = None, None
    if reminder_row is not None:
        fees_before = round(db.value("SELECT COALESCE(SUM(fee_chf), 0) FROM invoice_reminders WHERE practice_id = ? AND invoice_id = ?"
                                     " AND level < ?", (current_practice_id(), inv["id"], reminder_row["level"]), 0.0), 2)
        reminder = {"level": reminder_row["level"], "fee": reminder_row["fee_chf"], "due_on": reminder_row["due_on"],
                    "open_before": reminder_row["open_chf"], "fees_before": fees_before}
        open_amount = round(reminder_row["open_chf"] + reminder_row["fee_chf"], 2)
    return invoice_pdf.build(inv=inv, lines=repo.lines(db, inv["id"]), practice=practice, patient=patient, insurer=insurer,
                             therapists=therapists, tl=_tl(request, lang), lang=lang, reminder=reminder, open_amount=open_amount,
                             demo=bool(practice["is_demo"]) or request.app[SETTINGS].demo)


async def invoice_pdf(request: web.Request) -> web.StreamResponse:
    inv = _load(request)
    data = await asyncio.to_thread(_pdf, request, inv)
    audit(request.app[DB], "export", "invoice", inv["id"], inv["patient_id"], {"format": "pdf"})
    return web.Response(body=data, content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="rechnung-{inv["number"] or inv["id"]}.pdf"'})


async def reminder_pdf(request: web.Request) -> web.StreamResponse:
    inv = _load(request)
    level = int_param(request, "level")
    row = next((r for r in repo.reminders_for(request.app[DB], inv["id"]) if r["level"] == level), None)
    if row is None:
        raise web.HTTPNotFound()
    data = await asyncio.to_thread(_pdf, request, inv, row)
    audit(request.app[DB], "export", "reminder", row["id"], inv["patient_id"], {"format": "pdf", "level": level})
    return web.Response(body=data, content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="mahnung-{level}-{inv["number"]}.pdf"'})


async def xml_download(request: web.Request) -> web.StreamResponse:
    inv = _load(request)
    t = repo.transmission(request.app[DB], int_param(request, "tid"))
    if t is None or t["invoice_id"] != inv["id"]:
        raise web.HTTPNotFound()
    audit(request.app[DB], "export", "invoice", inv["id"], inv["patient_id"], {"format": "xml", "transmission": t["id"]})
    return web.Response(text=t["xml"], content_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="{inv["number"] or inv["id"]}_{t["kind"]}.xml"'})


async def payments_upload(request: web.Request) -> web.StreamResponse:
    """Bankdatei (camt.054 / camt.053) hochladen → Zahlungen zuordnen."""
    S = prefix(request)
    form = await request.post()
    field = form.get("file")
    data, filename = b"", "camt.xml"
    if field is not None and hasattr(field, "file"):
        filename = field.filename or filename
        data = field.file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.pay.too_big")
    if not data:
        raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.invalid")
    try:
        result = repo.import_payments(request.app[DB], filename, bytes(data))
    except camt.CamtError:
        raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.pay.bad_file")
    if result["duplicate"]:
        raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.pay.duplicate")
    raise redirect_with_flash(f"{S}/abrechnung?ansicht=zahlungen", "billing.pay.imported")


async def responses_upload(request: web.Request) -> web.StreamResponse:
    """Antworten der Versicherer (XML oder ZIP mit XML) hochladen – z. B. aus dem MediData-Portal."""
    S = prefix(request)
    back = f"{S}/abrechnung?ansicht=antworten"
    form = await request.post()
    field = form.get("file")
    data, filename = b"", "antwort.xml"
    if field is not None and hasattr(field, "file"):
        filename = field.filename or filename
        data = field.file.read(response.MAX_TOTAL + 1)
    if not data or len(data) > response.MAX_TOTAL:
        raise redirect_with_flash(back, "billing.resp.err_empty" if not data else "billing.resp.err_too_big")
    try:
        result = followup.import_responses(request.app[DB], filename, bytes(data))
    except response.ResponseError as exc:
        raise redirect_with_flash(back, f"billing.resp.err_{exc}")
    if result["errors"] and not result["imported"]:
        raise redirect_with_flash(back, f"billing.resp.err_{result['errors'][0][1]}")
    if not result["imported"] and result["duplicates"]:
        raise redirect_with_flash(back, "billing.resp.duplicate")
    raise redirect_with_flash(back, "billing.resp.imported_unmatched" if result["unmatched"] else "billing.resp.imported")


async def response_xml(request: web.Request) -> web.StreamResponse:
    r = followup.get_response(request.app[DB], int_param(request, "rid"))
    if r is None:
        raise web.HTTPNotFound()
    audit(request.app[DB], "export", "invoice_response", r["id"], None, {"format": "xml"})
    return web.Response(text=r["xml"], content_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="antwort-{r["id"]}.xml"'})


async def approvals_post(request: web.Request) -> web.StreamResponse:
    """Kostengutsprache erfassen oder Entscheid eintragen (aus dem Stammblatt)."""
    db = request.app[DB]
    S = prefix(request)
    form = await request.post()
    patient = core_patients.get(db, _id(form.get("patient_id"))) if _id(form.get("patient_id")) else None
    if patient is None:
        raise web.HTTPNotFound()
    back = f"{S}/patienten/{patient['id']}#kostengutsprache"

    def _int(name):                       # Anzahl Sitzungen 0–500; riesige Zahlen → leer (kein int() auf 5000 Ziffern)
        v = str(form.get(name) or "").strip()
        return int(v) if v.isdigit() and len(v) <= 3 and int(v) <= 500 else None

    def _date(name):
        v = str(form.get(name) or "").strip()[:10]
        try:
            return date.fromisoformat(v).isoformat() if v else None
        except ValueError:
            return None

    text = lambda name: str(form.get(name) or "").strip()[:200] or None  # noqa: E731
    if form.get("do") == "add":
        followup.add_approval(db, patient, {"requested_on": _date("requested_on") or zurich_today().isoformat(),
                                            "sessions_requested": _int("sessions_requested") or None, "reference": text("reference"),
                                            "note": text("note")})
        raise redirect_with_flash(back, "billing.kogu.saved")
    if form.get("do") == "decide" and _id(form.get("approval_id")):
        row = followup.approval(db, _id(form.get("approval_id")))
        if row is None or row["patient_id"] != patient["id"]:
            raise web.HTTPNotFound()
        followup.decide_approval(db, row["id"], {"status": str(form.get("status") or ""), "sessions_approved": _int("sessions_approved"),
                                                 "valid_until": _date("valid_until"), "reference": text("reference"), "note": text("note")})
        raise redirect_with_flash(back, "billing.kogu.saved")
    raise redirect_with_flash(back, "billing.invalid")


# --------------------------------------------------------------------------- Leitung: Taxpunktwerte
async def tariffs_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/tarife"
    if request.method == "POST":
        form = await request.post()
        value = _float(form.get("value"))
        valid_from = _day_or_today(form.get("valid_from"))
        group = str(form.get("group") or "")
        if value and 0 < value < 10 and form.get("tariff") in catalog.TARIFFS and (group == "" or group in repo.TP_GROUPS):
            repo.set_tp_value(db, str(form["tariff"]), value, valid_from, str(form.get("note") or "").strip()[:200] or None, group=group)
            repo.reprice_drafts(db, str(form["tariff"]))
            raise redirect_with_flash(back, "billing.tp_saved")
        raise redirect_with_flash(back, "billing.invalid")
    own = [tr for tr in catalog.TARIFFS.values() if tr.tp_value is None]          # Taxpunktwert trägt die Praxis ein (KVG)
    current = catalog.tariff_for_law("KVG", today().isoformat())
    return await settings_page(request, "tarife", "billing/s_tariffs.html", {
        "own_tariffs": own, "own_keys": [tr.key for tr in own], "current_kvg": current.key if current else "kvg312",
        "tp_values": repo.tp_values(db), "tariffs": catalog.TARIFFS,
        "today": today().isoformat(), "groups": repo.TP_GROUPS,
        "group_insurers": {g: [i["name"] for i in core_patients.all_insurers(db) if i["tp_group"] == g] for g in repo.TP_GROUPS}})


async def payment_settings_page(request: web.Request) -> web.StreamResponse:
    """Leitung: IBAN / QR-IBAN, Zahlungsfrist, Mahnwesen, elektronische Rechnung (MediData)."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/zahlung"
    if request.method == "POST":
        form = await request.post()
        iban = payment.clean_iban(str(form.get("iban") or ""))
        if iban and not payment.valid_iban(iban):
            raise redirect_with_flash(back, "billing.set.bad_iban")
        uid_raw = str(form.get("vat_number") or "").strip()
        uid = private.clean_uid(uid_raw) if uid_raw else None
        if uid_raw and uid is None:
            raise redirect_with_flash(back, "billing.set.bad_uid")
        try:
            values = {"iban": iban or None, "payment_days": int(form.get("payment_days") or 30),
                      "reminder_days": int(form.get("reminder_days") or 10),
                      "reminder_fee_2": _float(form.get("reminder_fee_2"), 0) or 0.0, "reminder_fee_3": _float(form.get("reminder_fee_3"), 0) or 0.0,
                      "xml_modus": "production" if form.get("xml_modus") == "production" else "test",
                      "copy_to_insurer": 1 if form.get("copy_to_insurer") == "1" else 0, "vat_number": uid}
        except ValueError:
            raise redirect_with_flash(back, "billing.invalid")
        if not (5 <= values["payment_days"] <= 120 and 5 <= values["reminder_days"] <= 60 and 0 <= values["reminder_fee_2"] <= 100
                and 0 <= values["reminder_fee_3"] <= 100):
            raise redirect_with_flash(back, "billing.invalid")
        vat_changed = (payment.settings(db)["vat_number"] or None) != uid
        payment.save_settings(db, values)
        if vat_changed:
            repo.reprice_private_drafts(db)
        audit(db, "update", "billing_settings", current_practice_id(), None, {k: v for k, v in values.items() if k != "iban"})
        raise redirect_with_flash(back, "common.saved")
    st = payment.settings(db)
    md = MediDataClient(request.app[SETTINGS])
    return await settings_page(request, "zahlung", "billing/s_payment.html", {
        "st": st, "qr": payment.is_qr_iban(st["iban"] or ""), "problems": repo.payment_problems(db), "md_mode": md.mode,
        "md_configured": md.configured, "iban_fmt": " ".join((st["iban"] or "")[i:i + 4] for i in range(0, len(st["iban"] or ""), 4))})


async def medidata_page(request: web.Request) -> web.StreamResponse:
    """Leitung: MediData – Stand des Client-Zertifikats (ohne Pfade) und Testsendung an die Simulatoren (nur im Testmodus)."""
    db = request.app[DB]
    settings = request.app[SETTINGS]
    back = f"{prefix(request)}/einstellungen/medidata"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "sync":                                # «Jetzt abrufen» – nur was seit 2 min (Verzeichnis: 1 h) nicht lief
            if not medidata_sync.active_here(db, settings):
                raise redirect_with_flash(back, "billing.md.not_configured")
            result = await medidata_sync.run(request.app, manual=True)
            key = ("billing.md.sync_wait" if result.get("skipped") in ("not_due", "busy") else
                   "billing.md.sync_failed" if "error" in result or "error" in result.values() else "billing.md.sync_done")
            raise redirect_with_flash(back, key)
        if do in ("seen_note", "seen_doc") and str(form.get("id", "")).isdigit():
            table = "medidata_notifications" if do == "seen_note" else "medidata_downloads"
            medidata_sync.mark_seen(db, table, int(form["id"]), current_user_id())
            raise redirect_with_flash(back, "common.saved")
        if do != "simulator":
            raise redirect_with_flash(back, "billing.invalid")
        try:
            result, _number = await simulator.send(settings, db, str(form.get("target") or ""), str(form.get("flag") or ""))
        except simulator.SimulatorRefused as exc:
            if exc.key == "missing":
                return await _medidata_render(request, missing=exc.missing)
            raise redirect_with_flash(back, {"not_test": "billing.md.refused_mode", "not_configured": "billing.md.not_configured"}
                                      .get(exc.key, "billing.invalid"))
        except xml500.XmlInvalid:
            raise redirect_with_flash(back, "billing.xml.invalid")
        raise redirect_with_flash(back, {"sent": "billing.md.sim_sent", "unknown": "billing.md.sim_unclear"}.get(
            result.status, "billing.md.sim_failed"))
    return await _medidata_render(request)


def _cert_ui_state(cert) -> str:
    """off | ok | warn (läuft in ≤ 60 Tagen ab) | check (gültig, aber Einstellung prüfen) | expired | error"""
    if cert.state == "warn" and not (cert.days_left is not None and 0 <= cert.days_left <= medidata_cert.RENEWAL_DAYS):
        return "check"
    return cert.state


async def _medidata_render(request: web.Request, missing: list[str] | None = None) -> web.Response:
    settings = request.app[SETTINGS]
    md = MediDataClient(settings)
    cert = medidata_cert.status(settings)
    db = request.app[DB]
    active = medidata_sync.active_here(db, settings)
    sync_state = medidata_sync.state(db)
    return await settings_page(request, "zahlung", "billing/s_medidata.html", {
        "md_mode": md.mode, "md_configured": active,
        "md_other_practice": md.configured and not active,
        "cert": {"state": _cert_ui_state(cert), "valid_to": cert.valid_to.date().isoformat() if cert.valid_to else None,
                 "days_left": cert.days_left},
        "sim_allowed": simulator.allowed(settings), "targets": simulator.TARGETS, "flags": simulator.FLAGS,
        "recent": simulator.recent(db), "missing": missing or [],
        "sync": [{"kind": k, **sync_state.get(k, {})} for k in medidata_sync.KINDS],
        "intervals": {k: v // 60 for k, v in medidata_sync.INTERVALS.items()},
        "directory": medidata_sync.directory_info(db), "notes": medidata_sync.notifications(db, limit=30),
        "docs": medidata_sync.unhandled_downloads(db), "md_codes": _code_texts(request)})


async def medidata_document(request: web.Request) -> web.StreamResponse:
    """Nicht übernommenes MediData-Dokument herunterladen (Original, nur Leitung, nur eigene Praxis)."""
    db = request.app[DB]
    row = db.one("SELECT id, transmission_reference, content FROM medidata_downloads WHERE id = ? AND practice_id = ?",
                 (int_param(request, "did"), current_practice_id()))
    if row is None or row["content"] is None:
        raise web.HTTPNotFound()
    audit(db, "export", "medidata_download", row["id"], None, {"format": "original"})
    return web.Response(body=bytes(row["content"]), content_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="medidata-{row["transmission_reference"]}.xml"',
                                 "Cache-Control": "no-store"})


def work_medidata(request: web.Request) -> dict:
    return {"label_key": "billing.md.work", "icon": "notifications", "n": medidata_sync.open_count(request.app[DB]),
            "href": f"{prefix(request)}/einstellungen/medidata"}


async def prices_page(request: web.Request) -> web.StreamResponse:
    """Leitung: Preisliste für Selbstzahler/VVG (Leistungen), Produkte und Abos (Kasse)."""
    db = request.app[DB]
    back = f"{prefix(request)}/einstellungen/preise"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "toggle" and str(form.get("item_id", "")).isdigit():
            private.toggle_item(db, int(form["item_id"]))
            repo.reprice_private_drafts(db)
            raise redirect_with_flash(back, "common.saved")
        if do == "save":
            values = _price_values(form)
            errs = private.validate(values)
            item_id = form_int(form, "item_id")
            if errs:
                return await _prices_render(request, values=values, errors=errs, edit_id=item_id)
            if private.save_item(db, values, item_id) is None:
                return await _prices_render(request, values=values, errors=["code_taken"], edit_id=item_id)
            repo.reprice_private_drafts(db)
            raise redirect_with_flash(back, "common.saved")
        raise redirect_with_flash(back, "billing.invalid")
    edit_id = int(request.query["bearbeiten"]) if request.query.get("bearbeiten", "").isdigit() else None
    item = private.item(db, edit_id)
    values = dict(item) if item else {"kind": request.query.get("art") if request.query.get("art") in private.KINDS else "service"}
    if not item:
        values["vat_rate"] = private.DEFAULT_VAT[values["kind"]]
    return await _prices_render(request, values=values, errors=[], edit_id=item["id"] if item else None)


def _price_values(form) -> dict:
    def _int(name):
        v = str(form.get(name) or "").strip()
        return int(v) if v.isdigit() else None

    return {"kind": str(form.get("kind") or ""), "code": str(form.get("code") or "").strip()[:20],
            "name_de": str(form.get("name_de") or "").strip()[:120], "name_fr": str(form.get("name_fr") or "").strip()[:120],
            "price_chf": _float(form.get("price_chf")), "vat_rate": _float(form.get("vat_rate"), -1),
            "treatment_type_id": _int("treatment_type_id"), "abo_sessions": _int("abo_sessions"), "abo_days": _int("abo_days")}


async def _prices_render(request: web.Request, *, values: dict, errors: list[str], edit_id: int | None):
    from ..agenda import api as agenda_api

    db = request.app[DB]
    values = {**dict.fromkeys(("kind", "code", "name_de", "name_fr", "price_chf", "vat_rate", "treatment_type_id", "abo_sessions",
                               "abo_days")), **values}
    types = agenda_api.lookup_tables(db)["types"]
    return await settings_page(request, "preise", "billing/s_prices.html", {
        "items": private.items(db, active_only=False), "values": values, "errors": errors, "edit_id": edit_id,
        "types": types, "type_names": {t["id"]: (t["name_fr"] if request["lang"] == "fr" else t["name_de"]) for t in types},
        "kinds": private.KINDS, "rates": private.VAT_RATES, "vat_number": private.vat_number(db)})


# --------------------------------------------------------------------------- Steckplätze
async def card_patient(request: web.Request, patient) -> dict:
    db = request.app[DB]
    return {"patient": patient, "invoices": repo.listing(db, None, patient["id"], limit=5),
            "unbilled": len(repo.unbilled_for(db, patient["id"])), "tariff": repo.tariff_key(patient["law"]),
            "approvals": followup.approvals_for(db, patient["id"]), "kvg_sessions": followup.kvg_sessions(db, patient["id"]),
            "kvg_free": followup.KVG_FREE_SESSIONS, "today": zurich_today().isoformat()}


def tile_open(request: web.Request) -> dict:
    o = repo.open_total(request.app[DB])
    return {"label_key": "billing.tile_drafts", "value": o["n"], "icon": "receipt_long",
            "sub_key": "billing.tile_drafts_sub", "sub_args": {"chf": formats.money(o['chf'])},
            "href": f"{prefix(request)}/abrechnung?ansicht=entwuerfe"}


def badge_drafts(db, practice_id: int, user) -> int:
    with practice_scope(practice_id):
        return repo.open_total(db)["n"]


def work_unbilled(request: web.Request) -> dict:
    return {"label_key": "stats.open.unbilled", "icon": "receipt_long", "n": len(repo.unbilled_for(request.app[DB])),
            "href": f"{prefix(request)}/abrechnung?ansicht=bereit"}


def work_overdue(request: web.Request) -> dict:
    items = [x for x in repo.open_items(request.app[DB]) if x["b"]["state"] == "overdue"]
    return {"label_key": "billing.work_overdue", "icon": "assignment_late", "n": len(items),
            "href": f"{prefix(request)}/abrechnung?ansicht=offen"}


def work_responses(request: web.Request) -> dict:
    return {"label_key": "billing.resp.work", "icon": "assignment_return", "n": followup.count_open_rejections(request.app[DB]),
            "href": f"{prefix(request)}/abrechnung?ansicht=antworten"}


def tile_month(request: web.Request) -> dict:
    from ...core.timeutil import day_bounds_utc, today

    db = request.app[DB]
    lo, _ = day_bounds_utc(today().replace(day=1))
    chf = db.value("SELECT COALESCE(SUM(total_chf), 0) FROM invoices WHERE practice_id = ? AND status = 'final' AND finalized_at >= ?",
                   (current_practice_id(), lo), 0)
    return {"label_key": "stats.billed_month", "value": formats.chf(chf, 0), "sub_key": "stats.this_month"}


def setup(ctx: ModuleContext) -> None:
    I = "/rechnungen/{iid:\\d+}"
    for screen, order in (("empfang", 40), ("leitung", 30)):
        ctx.page(screen, "/abrechnung", overview, ("GET", "POST"))
        ctx.page(screen, I, invoice_page, ("GET", "POST"))
        ctx.page(screen, I + "/pdf", invoice_pdf)
        ctx.page(screen, I + "/mahnung/{level:[123]}/pdf", reminder_pdf)
        ctx.page(screen, I + "/xml/{tid:\\d+}", xml_download)
        ctx.page(screen, "/abrechnung/zahlungen", payments_upload, ("POST",))
        ctx.page(screen, "/abrechnung/antworten", responses_upload, ("POST",))
        ctx.page(screen, "/abrechnung/antworten/{rid:\\d+}/xml", response_xml)
        ctx.page(screen, "/abrechnung/kostengutsprache", approvals_post, ("POST",))
        ctx.nav(screen, NavItem("nav.billing", "receipt_long", "/abrechnung", order=order, badge="invoices_draft", short_key="nav.billing_short"))
    ctx.badge("invoices_draft", badge_drafts)
    ctx.page("leitung", "/einstellungen/tarife", tariffs_page, ("GET", "POST"))
    ctx.slot("leitung.settings", 60, lambda request: {"key": "tarife", "label_key": "billing.sec_tariffs", "path": "tarife"})
    ctx.page("leitung", "/einstellungen/zahlung", payment_settings_page, ("GET", "POST"))
    ctx.slot("leitung.settings", 61, lambda request: {"key": "zahlung", "label_key": "billing.sec_payment", "path": "zahlung"})
    ctx.page("leitung", "/einstellungen/medidata", medidata_page, ("GET", "POST"))   # Reiter «Zahlung»
    ctx.page("leitung", "/einstellungen/medidata/dokument/{did:\\d+}", medidata_document)
    ctx.slot("leitung.overview.work", 57, work_medidata)
    ctx.set(medidata_sync.LOCK, asyncio.Lock())
    ctx.on_startup(medidata_sync.start)          # Abfragen alle 30 min / 24 h (nur wenn MediData eingerichtet ist)
    ctx.on_cleanup(medidata_sync.stop)
    ctx.page("leitung", "/einstellungen/preise", prices_page, ("GET", "POST"))
    ctx.slot("leitung.settings", 62, lambda request: {"key": "preise", "label_key": "billing.sec_prices", "path": "preise"})
    ctx.slot("leitung.overview.tile", 30, tile_open)
    ctx.slot("leitung.overview.tile", 35, tile_month)
    ctx.slot("leitung.overview.work", 50, work_unbilled)
    ctx.slot("leitung.overview.work", 55, work_overdue)
    ctx.slot("leitung.overview.work", 56, work_responses)
    ctx.slot("empfang.patient.card", 30, card_patient, "billing/card_patient.html")
    # Doppelte Einträge (Leitung): eigene Tabellen mit Patientenbezug zählen und beim Zusammenführen überführen
    ctx.patient_tables(('invoices', 'cost_approvals'), (
        "UPDATE invoices SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
        "UPDATE cost_approvals SET patient_id = :keep WHERE practice_id = :pid AND patient_id = :drop",
    ))
