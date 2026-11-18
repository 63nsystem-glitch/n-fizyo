"""Quittung als PDF (A4, schlicht): Positionen, Total, Rundung auf 5 Rappen (bar), MWST (nur mit MWST-Nummer), Zahlungsart,
Storno vor dem Abschluss und Korrekturen nach dem Tagesabschluss (Storno, Zahlungsart) – wie auf der Seite der Quittung."""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from ...core import money as core_money
from ...core import pdf
from ...core.formats import money, percent, phone
from ...core.timeutil import fmt_date, fmt_time
from ..billing import api as billing_api


def _chf(v: float) -> str:
    return money(v)


def build(*, sale, lines, practice, tl, lang: str, demo: bool = False, corrections=(), current_method: str | None = None) -> bytes:
    st = pdf.styles()
    sender = "<br/>".join(["<b>" + pdf.esc(practice["name"]) + "</b>"] + [pdf.esc(x) for x in (
        f"{practice['street'] or ''} {practice['house_no'] or ''}".strip() or practice["address"],
        f"{practice['zip'] or ''} {practice['city'] or ''}".strip(),
        " · ".join(x for x in (phone(practice["phone"]), practice["email"]) if x)) if x])
    title = tl("cashdesk.pdf.title_invoice") if sale["method"] == "invoice" else tl("cashdesk.pdf.title")
    story = [Paragraph(sender, st["small"]), Spacer(1, 10 * mm), Paragraph(pdf.esc(f"{title} {sale['number']}"), st["h1"])]
    if sale["status"] == "cancelled":
        story.append(Paragraph("<b>" + pdf.esc(tl("cashdesk.pdf.cancelled", reason=sale["cancel_reason"] or "")) + "</b>", st["base"]))
    for c in corrections:                          # Korrektur nach dem Tagesabschluss (die Quittung selbst bleibt unverändert)
        if c["kind"] == "storno":
            text = tl("cashdesk.pdf.corr_storno", date=fmt_date(c["created_at"], lang), reason=c["reason"])
            if c["refund_day"]:
                text += " " + tl("cashdesk.pdf.corr_refund", date=fmt_date(c["refund_day"], lang), method=tl("cashdesk.method." + c["method"]))
        else:
            text = tl("cashdesk.pdf.corr_method", date=fmt_date(c["created_at"], lang), old=tl("cashdesk.method." + c["method"]),
                      new=tl("cashdesk.method." + c["to_method"]), reason=c["reason"])
        story.append(Paragraph("<b>" + pdf.esc(text) + "</b>", st["base"]))
    facts = [(tl("cashdesk.pdf.date"), f"{fmt_date(sale['day'], lang)} {fmt_time(sale['created_at'])}"),
             (tl("cashdesk.pdf.staff"), sale["creator_name"])]
    if sale["first_name"]:
        facts.append((tl("cashdesk.pdf.patient"), f"{sale['first_name']} {sale['last_name']}"))
    t = Table([[Paragraph(pdf.esc(a), st["small"]), Paragraph(pdf.esc(b or "–"), st["small"])] for a, b in facts], colWidths=[35 * mm, 137 * mm])
    t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                           ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    story += [Spacer(1, 3 * mm), t, Spacer(1, 5 * mm)]

    hdr = [tl("cashdesk.pdf.col_item"), tl("cashdesk.pdf.col_qty"), tl("cashdesk.pdf.col_price"), tl("cashdesk.pdf.col_vat"), "CHF"]
    data = [[Paragraph("<b>" + pdf.esc(h) + "</b>", st["small"]) for h in hdr]]
    for ln in lines:
        data.append([Paragraph(pdf.esc(f"{ln['label']} ({ln['code']})"), st["small"]), Paragraph(f"{ln['quantity']:g}", st["small"]),
                     Paragraph(_chf(ln["unit_price"]), st["small"]), Paragraph(f"{ln['vat_rate']:g} %" if ln["vat_rate"] else "–", st["small"]),
                     Paragraph(_chf(ln["amount_chf"]), st["small"])])
    table = Table(data, colWidths=[100 * mm, 14 * mm, 22 * mm, 14 * mm, 22 * mm], repeatRows=1)
    table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black), ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(table)
    rounding = sale["rounding_chf"] if "rounding_chf" in sale.keys() else 0.0
    rows = []
    if rounding:                                   # bar auf 5 Rappen: Summe der Positionen, Rundung, bezahltes Total
        rows.append([Paragraph(pdf.esc(tl("cashdesk.pdf.subtotal")), st["small"]),
                     Paragraph(_chf(core_money.rp(sale["total_chf"] - rounding)), st["right"])])
        rows.append([Paragraph(pdf.esc(tl("cashdesk.pdf.rounding")), st["small"]), Paragraph(f"{rounding:+.2f}", st["right"])])
    rows.append([Paragraph("<b>" + pdf.esc(tl("cashdesk.pdf.total")) + "</b>", st["base"]), Paragraph("<b>CHF " + _chf(sale["total_chf"]) + "</b>", st["right"])])
    for v in billing_api.vat_summary(lines):
        rows.append([Paragraph(pdf.esc(tl("cashdesk.pdf.vat_incl", rate=percent(v['rate'], lang), gross=_chf(v["gross"]))), st["small"]),
                     Paragraph(_chf(v["vat"]), st["right"])])
    method_text = tl("cashdesk.method." + sale["method"])
    if current_method and current_method != sale["method"]:
        method_text = tl("cashdesk.pdf.method_corrected", old=method_text, new=tl("cashdesk.method." + current_method))
    rows.append([Paragraph(pdf.esc(tl("cashdesk.pdf.method")), st["small"]), Paragraph(pdf.esc(method_text), st["right"])])
    if sale["method"] == "cash" and sale["given_chf"]:
        rows.append([Paragraph(pdf.esc(tl("cashdesk.pdf.given")), st["small"]), Paragraph(_chf(sale["given_chf"]), st["right"])])
        rows.append([Paragraph(pdf.esc(tl("cashdesk.pdf.change")), st["small"]), Paragraph(_chf(sale["given_chf"] - sale["total_chf"]), st["right"])])
    tot = Table(rows, colWidths=[120 * mm, 52 * mm])
    tot.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [tot, Spacer(1, 5 * mm)]
    stornoed = sale["status"] == "cancelled" or any(c["kind"] == "storno" for c in corrections)
    if sale["vat_number"]:
        story.append(Paragraph(pdf.esc(tl("cashdesk.pdf.vat_number", uid=sale["vat_number"])), st["tiny"]))
    if not stornoed:                               # «Betrag dankend erhalten» nicht auf einer stornierten Quittung
        note = tl("cashdesk.pdf.note_invoice") if sale["method"] == "invoice" else tl("cashdesk.pdf.note_paid")
        story += [Spacer(1, 2 * mm), Paragraph(pdf.esc(note), st["small"])]

    def footer(canvas, doc):
        canvas.setFont(pdf.FONT, 7.5)
        canvas.drawString(20 * mm, 10 * mm, f"{practice['name']} · {sale['number']}")
        if demo:
            canvas.setFont(pdf.FONT_BOLD, 9)
            canvas.drawString(20 * mm, 14 * mm, tl("cashdesk.pdf.demo"))

    return pdf.build(story, title=f"{title} {sale['number']}", footer=footer)
