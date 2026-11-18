"""Rechnung und Mahnung als PDF (A4) mit Swiss-QR-Zahlteil.

Tiers garant: Patient/in bezahlt → Rechnung + QR-Zahlteil; die Rechnung dient auch als Rückforderungsbeleg.
Tiers payant: Versicherer bezahlt (elektronisch) → Kopie für die Patientin / den Patienten, ohne Zahlteil.
"""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, PageBreak, Paragraph, Spacer, Table, TableStyle

from ...core import pdf
from ...core.formats import date_range, money, percent, phone
from ...core.patients import split_street
from ...core.timeutil import fmt_date
from ...knowledge import tariffs as catalog
from . import payment


def _chf(v: float) -> str:
    return money(v)


LAWS = ("KVG", "UVG", "IVG", "MVG", "VVG", "privat")


def law_label(law: str | None, tl) -> str:
    """Gesetz in der Sprache des Dokuments: KVG/UVG/IVG/MVG – französisch LAMal/LAA/LAI/LAM (billing.law.*)."""
    return tl("billing.law." + law) if law in LAWS else (law or "–")


def _addr_lines(name: str, street: str | None, house: str | None, zip_code: str | None, city: str | None) -> list[str]:
    lines = [name]
    if street:
        lines.append(f"{street} {house or ''}".strip())
    if zip_code or city:
        lines.append(f"{zip_code or ''} {city or ''}".strip())
    return lines


def build(*, inv, lines, practice, patient, insurer, therapists: dict, tl, lang: str, reminder: dict | None = None,
          open_amount: float | None = None, demo: bool = False) -> bytes:
    """reminder = {level, fee, due_on, created_at} → Mahnung; sonst Rechnung."""
    st = pdf.styles()
    priv = inv["tariff"] == "private"
    garant = inv["tiers"] == "garant"
    pay_part = garant or reminder is not None
    sender = "<br/>".join(["<b>" + pdf.esc(practice["name"]) + "</b>"] + [pdf.esc(x) for x in (
        f"{practice['street'] or ''} {practice['house_no'] or ''}".strip() or practice["address"],
        f"{practice['zip'] or ''} {practice['city'] or ''}".strip(),
        " · ".join(x for x in (phone(practice["phone"]), practice["email"]) if x)) if x])
    p_street, p_house = split_street(patient["address"])
    pat_name = f"{patient['first_name']} {patient['last_name']}"
    pat_lines = _addr_lines(pat_name, p_street, p_house, patient["zip"], patient["city"])
    # Mahnung zu einer Tiers-payant-Rechnung geht an den Versicherer (er schuldet), nicht an die Patientin / den Patienten
    ins_debtor = bool(reminder and not garant and not priv and insurer is not None and insurer["name"])
    if ins_debtor:
        i_street, i_house = split_street(insurer["street"])
        to_lines = _addr_lines(insurer["name"], i_street, i_house, insurer["zip"], insurer["city"])
    else:
        to_lines = pat_lines
    head = Table([[Paragraph(sender, st["small"]), Paragraph("<br/>".join(pdf.esc(x) for x in to_lines), st["base"])]],
                 colWidths=[95 * mm, 77 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    if reminder:
        title = tl(f"billing.pdf.reminder_{reminder['level']}")
    elif priv:
        title = tl("billing.pdf.title_private")
    elif garant:
        title = tl("billing.pdf.title_garant")
    else:
        title = tl("billing.pdf.title_payant")
    story = [head, Spacer(1, 12 * mm), Paragraph(pdf.esc(title), st["h1"])]
    if inv["status"] == "cancelled":
        story.append(Paragraph("<b>" + pdf.esc(tl("billing.pdf.cancelled")) + "</b>", st["base"]))
    if inv["status"] == "draft":
        story.append(Paragraph("<b>" + pdf.esc(tl("billing.pdf.draft")) + "</b>", st["base"]))

    def row(label, value):
        return [Paragraph(pdf.esc(label), st["small"]), Paragraph(pdf.esc(value or "–"), st["small"])]

    main_t = therapists.get(lines[0]["therapist_id"]) if lines else None
    facts_left = [
        row(tl("billing.pdf.number"), inv["number"] or "–"),
        row(tl("billing.pdf.date"), fmt_date(inv["finalized_at"] or inv["created_at"], lang)),
        # Patientenkopie Tiers payant: die Person zahlt nichts – also kein «Zahlbar bis» (sonst widerspricht es dem Hinweis unten)
        row(tl("billing.pdf.due"), "–" if (not garant and not priv and not reminder)
            else fmt_date(reminder["due_on"] if reminder else inv["due_on"], lang) if (reminder or inv["due_on"]) else "–"),
        row(tl("billing.pdf.period"), date_range(inv['period_from'], inv['period_to'], lang) if inv["period_from"] else "–"),
        row(tl("billing.pdf.law"), law_label(inv["law"], tl)),
        row(tl("billing.pdf.tariff"), tl("billing.tariff_private")) if priv else row(tl("billing.pdf.tiers"), tl("billing.tiers." + inv["tiers"])),
    ]
    facts_right = [
        row(tl("billing.pdf.patient"), f"{patient['first_name']} {patient['last_name']}"),
        row(tl("billing.pdf.birthdate"), fmt_date(patient["birthdate"], lang) if patient["birthdate"] else "–"),
        row(tl("billing.pdf.ahv"), patient["ahv_number"]),
        row(tl("billing.pdf.insurer"), insurer["name"] if insurer else "–"),
        row(tl("billing.pdf.insured_no"), patient["insured_number"]),
        row(tl("billing.pdf.case"), " / ".join(x for x in (inv["case_number"], fmt_date(inv["accident_date"], lang) if inv["accident_date"] else None) if x)),
    ]
    facts = Table([[a[0], a[1], b[0], b[1]] for a, b in zip(facts_left, facts_right)],
                  colWidths=[28 * mm, 52 * mm, 30 * mm, 62 * mm])
    facts.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                               ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#555555"))]))
    story += [Spacer(1, 3 * mm), facts, Spacer(1, 3 * mm)]
    ids = [f"{tl('billing.pdf.biller')}: {practice['name']}"]
    if practice["gln"]:
        ids.append(f"GLN {practice['gln']}")
    if practice["zsr"]:
        ids.append(f"ZSR {practice['zsr']}")
    if main_t:
        ids.append(f"{tl('billing.pdf.provider')}: {main_t['name']}" + (f", GLN {main_t['gln']}" if main_t["gln"] else "")
                   + (f", ZSR {main_t['zsr']}" if main_t["zsr"] else ""))
    story.append(Paragraph(pdf.esc(" · ".join(ids)), st["tiny"]))
    story.append(Spacer(1, 4 * mm))

    if reminder:
        story.append(Paragraph(pdf.esc(tl(f"billing.pdf.reminder_text_{reminder['level']}", number=inv["number"],
                                           date=fmt_date(inv["finalized_at"], lang))), st["base"]))
        story.append(Spacer(1, 4 * mm))
        rows = [[tl("billing.pdf.invoice_total"), _chf(inv["total_chf"])]]
        paid = round(inv["total_chf"] + reminder.get("fees_before", 0.0) - reminder["open_before"], 2)
        if paid:
            rows.append([tl("billing.pdf.paid"), "−" + _chf(paid)])
        if reminder.get("fees_before"):
            rows.append([tl("billing.pdf.fees_before"), _chf(reminder["fees_before"])])
        if reminder["fee"]:
            rows.append([tl("billing.pdf.fee"), _chf(reminder["fee"])])
        rows.append([tl("billing.pdf.open"), "CHF " + _chf(open_amount or 0)])
        t = Table([[Paragraph(pdf.esc(a), st["base"]), Paragraph(pdf.esc(b), st["right"])] for a, b in rows], colWidths=[120 * mm, 52 * mm])
        t.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(t)
    elif priv:
        _private_lines(story, st, inv, lines, tl, lang)
    else:
        hdr = [tl("billing.pdf.col_date"), tl("billing.pdf.col_tariff"), tl("billing.pdf.col_code"), tl("billing.pdf.col_label"),
               tl("billing.pdf.col_qty"), tl("billing.pdf.col_tp"), tl("billing.pdf.col_tpw"), tl("billing.pdf.col_ef"), "CHF"]
        data = [[Paragraph("<b>" + pdf.esc(h) + "</b>", st["small"]) for h in hdr]]
        code = catalog.TARIFFS[inv["tariff"]].code if inv["tariff"] in catalog.TARIFFS else ""
        for ln in lines:
            data.append([Paragraph(pdf.esc(fmt_date(ln["service_date"], lang)), st["small"]), Paragraph(code, st["small"]),
                         Paragraph(pdf.esc(ln["code"] or "–"), st["small"]), Paragraph(pdf.esc(ln["label"]), st["small"]),
                         Paragraph(f"{ln['quantity']:g}", st["small"]), Paragraph(f"{(ln['tp'] or 0):g}", st["small"]),
                         Paragraph(f"{(ln['tp_value'] or 0):.2f}", st["small"]), Paragraph(f"{(ln['external_factor'] or 1):g}", st["small"]),
                         Paragraph(_chf(ln["amount_chf"]), st["small"])])
        table = Table(data, colWidths=[18 * mm, 11 * mm, 15 * mm, 62 * mm, 12 * mm, 12 * mm, 12 * mm, 10 * mm, 20 * mm], repeatRows=1)
        table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("ALIGN", (4, 1), (-1, -1), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")])]))
        story.append(table)
        total = Table([[Paragraph("<b>" + pdf.esc(tl("billing.pdf.total")) + "</b>", st["base"]),
                        Paragraph("<b>CHF " + _chf(inv["total_chf"]) + "</b>", st["right"])]], colWidths=[120 * mm, 52 * mm])
        total.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story += [total, Spacer(1, 4 * mm),
                  Paragraph(pdf.esc(tl("billing.pdf.vat")), st["tiny"])]
        # Kostenbeteiligung (Franchise, Selbstbehalt) gibt es nur im KVG – nicht bei UVG/IVG/MVG
        note = tl("billing.pdf.note_garant") if garant else tl("billing.pdf.note_payant_kvg" if inv["law"] == "KVG" else "billing.pdf.note_payant")
        story += [Spacer(1, 3 * mm), Paragraph(pdf.esc(note), st["small"])]

    footer_text = f"{practice['name']}" + (f" · {inv['number']}" if inv["number"] else "")

    def footer(canvas, doc):
        canvas.setFont(pdf.FONT, 7.5)
        canvas.drawString(20 * mm, 10 * mm, footer_text)
        canvas.drawRightString(192 * mm, 10 * mm, str(doc.page))
        if demo:
            canvas.setFont(pdf.FONT_BOLD, 9)
            canvas.drawString(20 * mm, 14 * mm, tl("billing.pdf.demo"))

    qr = None
    if pay_part and inv["iban"] and inv["status"] == "final":
        amount = open_amount if reminder else inv["total_chf"]
        creditor = payment.address(practice["name"], practice["street"], practice["house_no"], practice["zip"], practice["city"])
        if ins_debtor:
            debtor = payment.address(insurer["name"], i_street, i_house, insurer["zip"], insurer["city"])
        else:
            debtor = payment.address(pat_name, p_street, p_house, patient["zip"], patient["city"]) if garant else None
        if creditor:
            info = tl("billing.pdf.qr_info", number=inv["number"]) + (f" / M{reminder['level']}" if reminder else "")
            bill = payment.qr_bill(iban=inv["iban"], creditor=creditor, debtor=debtor, amount=amount, reference=inv["qr_reference"],
                                   info=info, lang=lang)
            qr = {"qr_data": bill.qr_data(), "account": pdf.format_iban(inv["iban"]),
                  "creditor_lines": _addr_lines(practice["name"], practice["street"], practice["house_no"], practice["zip"], practice["city"]),
                  "reference": pdf.format_reference(inv["qr_reference"]), "info": info,
                  "debtor_lines": (to_lines if ins_debtor else pat_lines) if debtor else None,
                  "amount": _chf(amount).replace("’", " "), "lang": lang if lang in ("de", "fr", "it") else "de"}

    if qr:
        story += [PageBreak(), Paragraph(pdf.esc(tl("billing.pdf.qr_page", number=inv["number"])), st["small"]), QRBillFlowable(qr)]
    return pdf.build(story, title=f"{title} {inv['number'] or ''}", footer=footer)


def _private_lines(story: list, st, inv, lines, tl, lang: str) -> None:
    """Selbstzahler: Preisliste statt Taxpunkte, MWST nur mit MWST-Nummer der Praxis."""
    from .private import vat_summary

    hdr = [tl("billing.pdf.col_date"), tl("billing.pdf.col_code"), tl("billing.pdf.col_label"), tl("billing.pdf.col_qty"),
           tl("billing.pdf.col_price"), tl("billing.pdf.col_vat"), "CHF"]
    data = [[Paragraph("<b>" + pdf.esc(h) + "</b>", st["small"]) for h in hdr]]
    for ln in lines:
        data.append([Paragraph(pdf.esc(fmt_date(ln["service_date"], lang)), st["small"]), Paragraph(pdf.esc(ln["code"] or "–"), st["small"]),
                     Paragraph(pdf.esc(ln["label"]), st["small"]), Paragraph(f"{ln['quantity']:g}", st["small"]),
                     Paragraph(_chf(ln["unit_price"] or 0), st["small"]),
                     Paragraph(f"{ln['vat_rate']:g} %" if ln["vat_rate"] else "–", st["small"]), Paragraph(_chf(ln["amount_chf"]), st["small"])])
    table = Table(data, colWidths=[18 * mm, 20 * mm, 72 * mm, 12 * mm, 18 * mm, 13 * mm, 19 * mm], repeatRows=1)
    table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("ALIGN", (3, 1), (-1, -1), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")])]))
    story.append(table)
    rows = [[Paragraph("<b>" + pdf.esc(tl("billing.pdf.total")) + "</b>", st["base"]),
             Paragraph("<b>CHF " + _chf(inv["total_chf"]) + "</b>", st["right"])]]
    for v in vat_summary(lines):
        rows.append([Paragraph(pdf.esc(tl("billing.pdf.vat_incl", rate=percent(v['rate'], lang), gross=_chf(v["gross"]))), st["small"]),
                     Paragraph(_chf(v["vat"]), st["right"])])
    total = Table(rows, colWidths=[120 * mm, 52 * mm])
    total.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [total, Spacer(1, 4 * mm)]
    if inv["vat_number"]:
        story.append(Paragraph(pdf.esc(tl("billing.pdf.vat_number", uid=inv["vat_number"])), st["tiny"]))
    story += [Spacer(1, 3 * mm), Paragraph(pdf.esc(tl("billing.pdf.note_private")), st["small"])]


class QRBillFlowable(Flowable):
    """Zeichnet den QR-Zahlteil unten auf die aktuelle Seite (absolute Lage, braucht keinen Platz im Text)."""

    def __init__(self, qr: dict):
        super().__init__()
        self.qr = qr

    def wrap(self, avail_width, avail_height):
        return 0, 0

    def drawOn(self, canvas, x, y, _sW=0):  # noqa: N802 - reportlab-Name
        pdf.draw_qr_bill(canvas, **self.qr)
        canvas._nf_no_footer = True  # im Zahlteil darf nichts anderes stehen

    def draw(self):  # pragma: no cover - drawOn übernimmt
        pass
