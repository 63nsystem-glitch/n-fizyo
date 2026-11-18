"""Firmenrechnung und Mahnung als PDF (A4) mit Swiss-QR-Zahlteil – Sprache der Firma (Deutsch oder Französisch)."""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, Spacer, Table, TableStyle

from ...core import pdf
from ...core.formats import date_range, percent, phone
from ...core.timeutil import fmt_date
from ..billing import api as billing_api
from . import repo


def build(*, inv, lines, firm, practice, tl, lang: str, reminder: dict | None = None, open_amount: float | None = None,
          paid: float = 0.0, fees_before: float = 0.0, demo: bool = False) -> bytes:
    """reminder = {level, fee, due_on} → Mahnung; sonst Rechnung."""
    st = pdf.styles()
    chf = billing_api.chf
    sender = "<br/>".join(["<b>" + pdf.esc(practice["name"]) + "</b>"] + [pdf.esc(x) for x in (
        f"{practice['street'] or ''} {practice['house_no'] or ''}".strip() or practice["address"],
        f"{practice['zip'] or ''} {practice['city'] or ''}".strip(),
        " · ".join(x for x in (phone(practice["phone"]), practice["email"]) if x)) if x])
    recipient = "<br/>".join(pdf.esc(x) for x in repo.address_lines(firm))
    head = Table([[Paragraph(sender, st["small"]), Paragraph(recipient, st["base"])]], colWidths=[95 * mm, 77 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    title = tl(f"firms.pdf.reminder_{reminder['level']}") if reminder else tl("firms.pdf.title")
    story = [head, Spacer(1, 12 * mm), Paragraph(pdf.esc(title), st["h1"])]
    if inv["status"] == "cancelled":
        story.append(Paragraph("<b>" + pdf.esc(tl("firms.pdf.cancelled")) + "</b>", st["base"]))
    if inv["status"] == "draft":
        story.append(Paragraph("<b>" + pdf.esc(tl("firms.pdf.draft")) + "</b>", st["base"]))

    def row(label, value):
        return [Paragraph(pdf.esc(label), st["small"]), Paragraph(pdf.esc(value or "–"), st["small"])]

    left = [row(tl("firms.pdf.number"), inv["number"] or "–"),
            row(tl("firms.pdf.date"), fmt_date(inv["finalized_at"] or inv["created_at"], lang)),
            row(tl("firms.pdf.due"), fmt_date(reminder["due_on"] if reminder else inv["due_on"], lang) if (reminder or inv["due_on"]) else "–")]
    right = [row(tl("firms.pdf.their_reference"), inv["their_reference"]),
             row(tl("firms.pdf.period"), date_range(inv['period_from'], inv['period_to'], lang) if inv["period_from"] else None),
             row(tl("firms.pdf.firm_uid"), firm["uid"])]
    facts = Table([[a[0], a[1], b[0], b[1]] for a, b in zip(left, right)], colWidths=[28 * mm, 52 * mm, 30 * mm, 62 * mm])
    facts.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                               ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#555555"))]))
    story += [Spacer(1, 3 * mm), facts, Spacer(1, 5 * mm)]
    if inv["subject"]:
        story += [Paragraph("<b>" + pdf.esc(inv["subject"]) + "</b>", st["base"]), Spacer(1, 3 * mm)]

    if reminder:
        story.append(Paragraph(pdf.esc(tl(f"firms.pdf.reminder_text_{reminder['level']}", number=inv["number"],
                                           date=fmt_date(inv["finalized_at"], lang))), st["base"]))
        story.append(Spacer(1, 4 * mm))
        rows = [[tl("firms.pdf.invoice_total"), chf(inv["total_chf"])]]
        if paid:
            rows.append([tl("firms.pdf.paid"), "−" + chf(paid)])
        if fees_before:
            rows.append([tl("firms.pdf.fees_before"), chf(fees_before)])
        if reminder["fee"]:
            rows.append([tl("firms.pdf.fee"), chf(reminder["fee"])])
        rows.append([tl("firms.pdf.open"), "CHF " + chf(open_amount or 0)])
        t = Table([[Paragraph(pdf.esc(a), st["base"]), Paragraph(pdf.esc(b), st["right"])] for a, b in rows], colWidths=[120 * mm, 52 * mm])
        t.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(t)
    else:
        hdr = [tl("firms.pdf.col_date"), tl("firms.pdf.col_label"), tl("firms.pdf.col_qty"), tl("firms.pdf.col_price"),
               tl("firms.pdf.col_vat"), "CHF"]
        data = [[Paragraph("<b>" + pdf.esc(h) + "</b>", st["small"]) for h in hdr]]
        for ln in lines:
            label = pdf.esc(ln["label"]) + (f" <font color='#666666'>({pdf.esc(ln['code'])})</font>" if ln["code"] else "")
            data.append([Paragraph(pdf.esc(fmt_date(ln["service_date"], lang)) if ln["service_date"] else "–", st["small"]),
                         Paragraph(label, st["small"]), Paragraph(f"{ln['quantity']:g}", st["small"]),
                         Paragraph(chf(ln["unit_price"]), st["small"]),
                         Paragraph(f"{ln['vat_rate']:g} %" if ln["vat_rate"] else "–", st["small"]), Paragraph(chf(ln["amount_chf"]), st["small"])])
        table = Table(data, colWidths=[20 * mm, 84 * mm, 14 * mm, 20 * mm, 14 * mm, 20 * mm], repeatRows=1)
        table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("ALIGN", (2, 1), (-1, -1), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")])]))
        story.append(table)
        rows = [[Paragraph("<b>" + pdf.esc(tl("firms.pdf.total")) + "</b>", st["base"]),
                 Paragraph("<b>CHF " + chf(inv["total_chf"]) + "</b>", st["right"])]]
        for v in billing_api.vat_summary(lines):
            rows.append([Paragraph(pdf.esc(tl("firms.pdf.vat_incl", rate=percent(v['rate'], lang), gross=chf(v["gross"]))), st["small"]),
                         Paragraph(chf(v["vat"]), st["right"])])
        total = Table(rows, colWidths=[120 * mm, 52 * mm])
        total.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story += [total, Spacer(1, 4 * mm)]
        if inv["vat_number"]:
            story.append(Paragraph(pdf.esc(tl("firms.pdf.vat_number", uid=inv["vat_number"])), st["tiny"]))
        elif not inv["vat_chf"]:
            story.append(Paragraph(pdf.esc(tl("firms.pdf.no_vat")), st["tiny"]))
        story += [Spacer(1, 3 * mm), Paragraph(pdf.esc(tl("firms.pdf.note", due=fmt_date(inv["due_on"], lang) if inv["due_on"] else "–")), st["small"])]

    footer_text = f"{practice['name']}" + (f" · {inv['number']}" if inv["number"] else "")

    def footer(canvas, doc):
        canvas.setFont(pdf.FONT, 7.5)
        canvas.drawString(20 * mm, 10 * mm, footer_text)
        canvas.drawRightString(192 * mm, 10 * mm, str(doc.page))
        if demo:
            canvas.setFont(pdf.FONT_BOLD, 9)
            canvas.drawString(20 * mm, 14 * mm, tl("firms.pdf.demo"))

    if inv["status"] == "final" and inv["iban"] and inv["qr_reference"]:
        amount = open_amount if reminder else inv["total_chf"]
        info = tl("firms.pdf.qr_info", number=inv["number"]) + (f" / M{reminder['level']}" if reminder else "")
        qr = billing_api.qr_flowable(practice=practice, iban=inv["iban"], amount=amount, reference=inv["qr_reference"],
                                     debtor=repo.debtor(firm), info=info, lang=lang)
        if qr is not None:
            story += [PageBreak(), Paragraph(pdf.esc(tl("firms.pdf.qr_page", number=inv["number"])), st["small"]), qr]
    return pdf.build(story, title=f"{title} {inv['number'] or ''}", footer=footer)
