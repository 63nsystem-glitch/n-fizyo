"""Bericht als PDF (für den Versand über HIN). Inhalt wie die Druckansicht, unveränderbar abgeschlossen."""
from __future__ import annotations

from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from ...core import pdf
from ...core.formats import phone
from ...core.timeutil import fmt_date
from .repo import SECTIONS


def build(r, practice, author, tl, *, demo: bool = False) -> bytes:
    st = pdf.styles()
    lang = r["lang"]
    contact = " · ".join(x for x in (phone(practice["phone"]), practice["email"]) if x)
    sender = "<br/>".join(["<b>" + pdf.esc(practice["name"]) + "</b>"] + [pdf.esc(x) for x in (practice["address"], contact) if x])
    recipient = "<br/>".join(pdf.esc(x) for x in (r["referrer_name"], r["referrer_practice"], r["referrer_address"]) if x)
    head = Table([[Paragraph(sender, st["small"]),
                   Paragraph(recipient, st["base"])]], colWidths=[85 * mm, 87 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story = [head, Spacer(1, 14 * mm),
             Paragraph(pdf.esc(fmt_date(r["finalized_at"] or r["updated_at"], lang)), st["base"]), Spacer(1, 6 * mm),
             Paragraph(pdf.esc(tl("reports.kind." + r["kind"])), st["h1"]),
             Paragraph("<b>" + pdf.esc(r["subject"]) + "</b>", st["base"]), Spacer(1, 4 * mm),
             Paragraph(pdf.esc(r["salutation"]), st["base"])]
    for s in SECTIONS:
        if r[s]:
            story += [Paragraph(pdf.esc(tl("reports.sec." + s)), st["h2"]), Paragraph(pdf.esc(r[s]), st["base"])]
    story += [Spacer(1, 8 * mm), Paragraph(pdf.esc(tl("reports.closing")), st["base"]), Spacer(1, 10 * mm),
              Paragraph(pdf.esc(author["name"]) + "<br/>" + pdf.esc(tl("reports.signature_role")), st["base"])]

    foot_bits = [practice["name"]]
    if practice["gln"]:
        foot_bits.append(f"GLN {practice['gln']}")
    if practice["zsr"]:
        foot_bits.append(f"ZSR {practice['zsr']}")
    if author["gln"]:
        foot_bits.append(f"{tl('reports.therapist_gln')} {author['gln']}")
    foot = " · ".join(foot_bits)

    def footer(canvas, doc):
        canvas.setFont(pdf.FONT, 7.5)
        canvas.drawString(20 * mm, 10 * mm, foot)
        canvas.drawRightString(192 * mm, 10 * mm, str(doc.page))
        if demo:
            canvas.setFont(pdf.FONT_BOLD, 9)
            canvas.drawString(20 * mm, 14 * mm, tl("reports.demo_footer"))

    return pdf.build(story, title=f"{tl('reports.kind.' + r['kind'])} {r['first_name']} {r['last_name']}", footer=footer)
