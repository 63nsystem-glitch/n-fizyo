"""Firmenkunden: Firmen erfassen, Rechnungen an Firmen schreiben (Preisliste oder freie Positionen), abschliessen,
PDF mit QR-Zahlteil, per E-Mail senden, Zahlungen, Mahnungen, Storno.

Empfang (/empfang): Firmen und Entwürfe, PDF, E-Mail, Zahlungen von Hand, Mahnungen.
Leitung (/leitung): zusätzlich abschliessen (Nummer, unveränderbar) und stornieren.
Bankzahlungen kommen über die Bankdatei der Abrechnung (camt.054) automatisch zur richtigen Firmenrechnung.
"""
from __future__ import annotations

import asyncio
from aiohttp import web

from ...core.audit import audit
from ...core.keys import DB, I18N, MAIL, SETTINGS
from ...core.modules import ModuleContext, NavItem
from ...core.screens import prefix
from ...core.tenant import current_practice_id, practice_scope
from ...core.timeutil import fmt_date, today
from ...core.web import int_param, redirect_with_flash, render, form_int
from ..billing import api as billing_api
from . import repo


def _float(v, default: float | None = None) -> float | None:
    return repo._float(v, default)


def is_leitung(request: web.Request) -> bool:
    return request["screen"].key == "leitung"


def _firm(request: web.Request):
    f = repo.get_firm(request.app[DB], int_param(request, "fid"))
    if f is None:
        raise web.HTTPNotFound()
    return f


def _inv(request: web.Request):
    inv = repo.get(request.app[DB], int_param(request, "iid"))
    if inv is None:
        raise web.HTTPNotFound()
    return inv


# --------------------------------------------------------------------------- Liste
async def overview(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    view = request.query.get("ansicht", "firmen")
    if request.method == "POST":
        form = await request.post()
        if form.get("do") == "create":
            values, errs = repo.clean_firm(form)
            if errs:
                raise redirect_with_flash(f"{S}/firmen?neu=1", "firms.err." + errs[0])
            fid = repo.save_firm(db, values)
            raise redirect_with_flash(f"{S}/firmen/{fid}", "firms.saved")
        raise web.HTTPBadRequest()
    q = request.query.get("q", "").strip()[:60]
    ctx = {"view": view, "q": q, "new": request.query.get("neu") == "1", "totals": repo.totals(db)}
    if view == "rechnungen":
        status = request.query.get("status") or None
        status = status if status in ("draft", "final", "cancelled") else None
        rows = repo.listing(db, status=status)
        ctx.update(status=status, invoices=[{"inv": r, "b": repo.balance(db, r)} for r in rows])
    elif view == "offen":
        ctx.update(items=repo.open_items(db))
    else:
        ctx.update(firms=repo.firms(db, active_only=request.query.get("alle") != "1", q=q or None), all=request.query.get("alle") == "1")
    return await render(request, "firms/list.html", ctx)


# --------------------------------------------------------------------------- Firma
async def firm_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    firm = _firm(request)
    back = f"{S}/firmen/{firm['id']}"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if do == "save":
            values, errs = repo.clean_firm(form)
            if errs:
                raise redirect_with_flash(back, "firms.err." + errs[0])
            repo.save_firm(db, values, firm["id"])
            raise redirect_with_flash(back, "firms.saved")
        if do in ("archive", "activate"):
            repo.set_active(db, firm["id"], do == "activate")
            raise redirect_with_flash(back, "firms.saved")
        if do == "invoice" and firm["active"]:
            iid = repo.create(db, firm, subject=str(form.get("subject") or ""))
            raise web.HTTPFound(f"{S}/firmen/rechnungen/{iid}")
        raise web.HTTPBadRequest()
    invoices = [{"inv": r, "b": repo.balance(db, r)} for r in repo.listing(db, firm_id=firm["id"])]
    return await render(request, "firms/firm.html", {
        "firm": firm, "invoices": invoices, "open_chf": round(sum(x["b"]["open"] for x in invoices), 2),
        "default_days": int(billing_api.payment_settings(db)["payment_days"]),
    })


# --------------------------------------------------------------------------- Rechnung
async def invoice_page(request: web.Request) -> web.StreamResponse:
    db = request.app[DB]
    S = prefix(request)
    inv = _inv(request)
    back = f"{S}/firmen/rechnungen/{inv['id']}"
    if request.method == "POST":
        form = await request.post()
        do = form.get("do")
        if inv["status"] == "draft":
            if do == "head":
                repo.update_head(db, inv, subject=form.get("subject"), their_reference=form.get("their_reference"),
                                 period_from=form.get("period_from"), period_to=form.get("period_to"), lang=form.get("lang"))
            elif do == "item":
                item = billing_api.price_item(db, form_int(form, "item_id")) if form_int(form, "item_id") is not None else None
                if repo.add_item_line(db, inv, item, _float(form.get("quantity"), 1) or 0, inv["lang"],
                                      str(form.get("service_date") or "")) is None:
                    raise redirect_with_flash(back, "firms.invalid")
            elif do == "free":
                if repo.add_free_line(db, inv, label=str(form.get("label") or ""), quantity=_float(form.get("quantity"), 1) or 0,
                                      unit_price=_float(form.get("unit_price"), -1), vat_rate=_float(form.get("vat_rate"), 0.0),
                                      kind=str(form.get("kind") or "service"), service_date=str(form.get("service_date") or "")) is None:
                    raise redirect_with_flash(back, "firms.invalid")
            elif do == "line" and str(form.get("line_id", "")).isdigit():
                if not repo.update_line(db, inv, int(form["line_id"]), quantity=_float(form.get("quantity"), 0) or 0,
                                        unit_price=_float(form.get("unit_price"))):
                    raise redirect_with_flash(back, "firms.invalid")
            elif do == "remove" and str(form.get("line_id", "")).isdigit():
                repo.remove_line(db, inv, int(form["line_id"]))
            elif do == "finalize" and is_leitung(request):
                if repo.finalize(db, repo.get(db, inv["id"])) is None:
                    raise redirect_with_flash(back, "firms.fix_first")
                raise redirect_with_flash(back, "firms.finalized")
            elif do == "delete":
                firm_id = inv["firm_id"]
                repo.delete_draft(db, inv)
                raise redirect_with_flash(f"{S}/firmen/{firm_id}", "firms.deleted")
            else:
                raise web.HTTPBadRequest()
        elif inv["status"] == "final":
            if do == "payment":
                amount = _float(form.get("amount"))
                booked = str(form.get("booked_on") or today().isoformat())[:10]
                if not amount or repo.add_payment(db, inv, amount=amount, booked_on=booked, method=str(form.get("method") or "bank"),
                                                  note=str(form.get("note") or "").strip()) is None:
                    raise redirect_with_flash(back, "firms.invalid")
                raise redirect_with_flash(back, "firms.pay_saved")
            if do == "reminder" and str(form.get("level", "")).isdigit():
                if repo.create_reminder(db, inv, int(form["level"])) is None:
                    raise redirect_with_flash(back, "firms.rem_not_due")
                raise redirect_with_flash(back, "firms.rem_created")
            if do == "email":
                ok = await _send_by_email(request, inv)
                raise redirect_with_flash(back, "firms.mail_sent" if ok else "firms.mail_missing")
            if do == "cancel" and is_leitung(request):
                if not repo.cancel(db, inv, str(form.get("reason") or "").strip()):
                    raise redirect_with_flash(back, "firms.invalid")
                raise redirect_with_flash(back, "firms.cancelled")
            raise web.HTTPBadRequest()
        else:
            raise web.HTTPBadRequest()
        raise web.HTTPFound(back)
    lns = repo.lines(db, inv["id"])
    firm = repo.get_firm(db, inv["firm_id"])
    return await render(request, "firms/invoice.html", {
        "inv": inv, "firm": firm, "lines": lns, "vat": billing_api.vat_summary(lns),
        "problems": repo.problems(db, inv, lns) if inv["status"] == "draft" else [],
        "items": billing_api.price_items(db, ("service", "product", "abo")), "vat_rates": billing_api.VAT_RATES,
        "vat_allowed": repo.vat_allowed(db), "can_finalize": is_leitung(request), "today": today().isoformat(),
        "b": repo.balance(db, inv), "payments": repo.payments_for(db, inv["id"]), "reminders": repo.reminders_for(db, inv["id"]),
        "next_reminder": repo.next_reminder(db, inv), "methods": repo.METHODS,
        "ref": _fmt_ref(inv["qr_reference"]),
        "emailed": db.value("SELECT MAX(at) FROM audit_log WHERE practice_id = ? AND action = 'email' AND entity = 'firm_invoice'"
                            " AND entity_id = ?", (current_practice_id(), inv["id"])),
    })


def _fmt_ref(ref: str | None) -> str:
    from ...core.pdf import format_reference

    return format_reference(ref) if ref else ""


def _tl(request: web.Request, lang: str):
    i18n = request.app[I18N]
    return lambda key, **kw: i18n.t(lang, key, **kw)


def _pdf(request: web.Request, inv, reminder_row=None) -> bytes:
    from . import pdf as firm_pdf

    db = request.app[DB]
    practice = billing_api.practice(db)
    firm = repo.get_firm(db, inv["firm_id"])
    lang = inv["lang"] if inv["lang"] in ("de", "fr") else "de"
    reminder = open_amount = None
    paid = fees_before = 0.0
    if reminder_row is not None:
        fees_before = round(db.value("SELECT COALESCE(SUM(fee_chf), 0) FROM firm_reminders WHERE practice_id = ? AND invoice_id = ?"
                                     " AND level < ?", (current_practice_id(), inv["id"], reminder_row["level"]), 0.0), 2)
        reminder = {"level": reminder_row["level"], "fee": reminder_row["fee_chf"], "due_on": reminder_row["due_on"]}
        open_amount = round(reminder_row["open_chf"] + reminder_row["fee_chf"], 2)
        paid = round(inv["total_chf"] + fees_before - reminder_row["open_chf"], 2)
    return firm_pdf.build(inv=inv, lines=repo.lines(db, inv["id"]), firm=firm, practice=practice, tl=_tl(request, lang), lang=lang,
                          reminder=reminder, open_amount=open_amount, paid=paid, fees_before=fees_before,
                          demo=bool(practice["is_demo"]) or request.app[SETTINGS].demo)


async def invoice_pdf(request: web.Request) -> web.StreamResponse:
    inv = _inv(request)
    data = await asyncio.to_thread(_pdf, request, inv)
    audit(request.app[DB], "export", "firm_invoice", inv["id"], None, {"format": "pdf"})
    return web.Response(body=data, content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="rechnung-{inv["number"] or inv["id"]}.pdf"'})


async def reminder_pdf(request: web.Request) -> web.StreamResponse:
    inv = _inv(request)
    level = int_param(request, "level")
    row = next((r for r in repo.reminders_for(request.app[DB], inv["id"]) if r["level"] == level), None)
    if row is None:
        raise web.HTTPNotFound()
    data = await asyncio.to_thread(_pdf, request, inv, row)
    audit(request.app[DB], "export", "firm_reminder", row["id"], None, {"format": "pdf", "level": level})
    return web.Response(body=data, content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="mahnung-{level}-{inv["number"]}.pdf"'})


async def _send_by_email(request: web.Request, inv) -> bool:
    """Rechnung als PDF an die E-Mail-Adresse der Firma (Geschäftskunde, keine Patientendaten)."""
    db = request.app[DB]
    firm = repo.get_firm(db, inv["firm_id"])
    if firm is None or not firm["email"]:
        return False
    practice = billing_api.practice(db)
    lang = inv["lang"] if inv["lang"] in ("de", "fr") else "de"
    data = await asyncio.to_thread(_pdf, request, inv)
    i18n = request.app[I18N]
    params = {"number": inv["number"], "amount": billing_api.chf(inv["total_chf"]), "due": fmt_date(inv["due_on"], lang) if inv["due_on"] else "",
              "practice": practice["name"], "phone": practice["phone"] or "", "contact": firm["contact"] or ""}
    body = i18n.t(lang, "firms.mail.body", **params)
    request.app[MAIL].send(practice["id"], firm["email"], i18n.t(lang, "firms.mail.subject", **params), body,
                           attachment=(f"rechnung-{inv['number']}.pdf", data))
    audit(db, "email", "firm_invoice", inv["id"], None, {"number": inv["number"]})
    return True


# --------------------------------------------------------------------------- Übersicht der Leitung, Menü
def badge_drafts(db, practice_id: int, user) -> int:
    with practice_scope(practice_id):
        return db.value("SELECT COUNT(*) FROM firm_invoices WHERE practice_id = ? AND status = 'draft'", (practice_id,), 0)


def work_overdue(request: web.Request) -> dict:
    n = repo.totals(request.app[DB])["overdue"]
    return {"label_key": "firms.work_overdue", "icon": "domain", "n": n, "href": f"{prefix(request)}/firmen?ansicht=offen"}


def setup(ctx: ModuleContext) -> None:
    for screen, order in (("empfang", 42), ("leitung", 32)):
        ctx.page(screen, "/firmen", overview, ("GET", "POST"))
        ctx.page(screen, "/firmen/{fid:\\d+}", firm_page, ("GET", "POST"))
        ctx.page(screen, "/firmen/rechnungen/{iid:\\d+}", invoice_page, ("GET", "POST"))
        ctx.page(screen, "/firmen/rechnungen/{iid:\\d+}/pdf", invoice_pdf)
        ctx.page(screen, "/firmen/rechnungen/{iid:\\d+}/mahnung/{level:[123]}/pdf", reminder_pdf)
        ctx.nav(screen, NavItem("nav.firms", "domain", "/firmen", order=order, badge="firms_draft", short_key="nav.firms_short"))
    ctx.badge("firms_draft", badge_drafts)
    ctx.slot("leitung.overview.work", 58, work_overdue)
