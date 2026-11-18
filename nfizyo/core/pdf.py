"""PDF-Bausteine (reportlab): Schrift, Briefseite, Swiss-QR-Zahlteil.

Schrift: Liberation Sans (SIL OFL 1.1, liegt unverändert in knowledge/fonts) – deckt Umlaute, ç, ş, ğ … ab und ist
für den QR-Zahlteil zugelassen (SIX Style Guide QR-Rechnung: Arial, Frutiger, Helvetica oder Liberation Sans).
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Callable

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate

FONT_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "fonts"
FONT, FONT_BOLD = "NF-Sans", "NF-Sans-Bold"
_registered = False


def fonts() -> None:
    global _registered
    if _registered:
        return
    pdfmetrics.registerFont(TTFont(FONT, str(FONT_DIR / "LiberationSans-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "LiberationSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
    _registered = True


def styles() -> dict[str, ParagraphStyle]:
    fonts()
    base = ParagraphStyle("base", fontName=FONT, fontSize=10, leading=13.5)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5),
        "tiny": ParagraphStyle("tiny", parent=base, fontSize=7, leading=9, textColor=colors.HexColor("#555555")),
        "h1": ParagraphStyle("h1", parent=base, fontName=FONT_BOLD, fontSize=15, leading=19, spaceAfter=4),
        "h2": ParagraphStyle("h2", parent=base, fontName=FONT_BOLD, fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=2),
        "bold": ParagraphStyle("bold", parent=base, fontName=FONT_BOLD),
        "right": ParagraphStyle("right", parent=base, alignment=TA_RIGHT),
        "right_bold": ParagraphStyle("right_bold", parent=base, alignment=TA_RIGHT, fontName=FONT_BOLD),
    }


def esc(text) -> str:
    """Text für reportlab-Paragraph (kleines XML): Sonderzeichen maskieren, Zeilenumbrüche erhalten."""
    s = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace("\n", "<br/>")


class _Doc(SimpleDocTemplate):
    """Fusszeile erst am Seitenende zeichnen – so kann eine Seite sie abschalten (z. B. Seite mit QR-Zahlteil)."""

    footer: Callable | None = None

    def afterPage(self):  # noqa: N802 - reportlab-Name
        canvas = self.canv
        if self.footer and not getattr(canvas, "_nf_no_footer", False):
            canvas.saveState()
            self.footer(canvas, self)
            canvas.restoreState()
        canvas._nf_no_footer = False


def build(story: list, *, title: str, footer: Callable | None = None, top: float = 18 * mm, bottom: float = 20 * mm) -> bytes:
    """story → PDF (A4). footer(canvas, doc) zeichnet die Fusszeile jeder Seite."""
    fonts()
    buf = BytesIO()
    doc = _Doc(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=18 * mm, topMargin=top, bottomMargin=bottom,
               title=title, author="N-Fizyo", creator="N-Fizyo")
    doc.footer = footer
    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- Swiss QR-Zahlteil (210 × 105 mm)
QR_LABELS = {
    "de": {"receipt": "Empfangsschein", "payment": "Zahlteil", "account": "Konto / Zahlbar an", "reference": "Referenz",
           "info": "Zusätzliche Informationen", "payable_by": "Zahlbar durch", "payable_by_blank": "Zahlbar durch (Name/Adresse)",
           "currency": "Währung", "amount": "Betrag", "acceptance": "Annahmestelle", "separate": "Vor der Einzahlung abzutrennen"},
    "fr": {"receipt": "Récépissé", "payment": "Section paiement", "account": "Compte / Payable à", "reference": "Référence",
           "info": "Informations supplémentaires", "payable_by": "Payable par", "payable_by_blank": "Payable par (nom/adresse)",
           "currency": "Monnaie", "amount": "Montant", "acceptance": "Point de dépôt", "separate": "A détacher avant le versement"},
    "it": {"receipt": "Ricevuta", "payment": "Sezione pagamento", "account": "Conto / Pagabile a", "reference": "Riferimento",
           "info": "Informazioni supplementari", "payable_by": "Pagabile da", "payable_by_blank": "Pagabile da (nome/indirizzo)",
           "currency": "Valuta", "amount": "Importo", "acceptance": "Punto di accettazione", "separate": "Da staccare prima del versamento"},
}


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(trial, font, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_qr_bill(canvas, *, qr_data: str, account: str, creditor_lines: list[str], reference: str | None,
                 info: str | None, debtor_lines: list[str] | None, amount: str | None, currency: str = "CHF",
                 lang: str = "de", y0: float = 0) -> None:
    """Zahlteil mit Empfangsschein unten auf der Seite (y0 = Unterkante). Masse nach SIX Implementation Guidelines."""
    import segno

    fonts()
    lab = QR_LABELS.get(lang, QR_LABELS["de"])
    top = y0 + 105 * mm
    c = canvas
    c.saveState()
    # Trennlinien (gestrichelt) + Hinweis zum Abtrennen
    c.setDash(2, 2)
    c.setLineWidth(0.4)
    c.line(0, top, 210 * mm, top)
    c.line(62 * mm, y0, 62 * mm, top)
    c.setDash()
    c.setFont(FONT, 7)
    c.drawCentredString(105 * mm, top + 1.5 * mm, lab["separate"])

    # ---------------- Empfangsschein (62 mm)
    x = 5 * mm
    y = top - 5 * mm - 11
    c.setFont(FONT_BOLD, 11)
    c.drawString(x, y, lab["receipt"])
    y -= 7 * mm

    def head(text, size, xx, yy):
        c.setFont(FONT_BOLD, size)
        c.drawString(xx, yy, text)
        return yy - size * 1.35

    def lines(items, size, xx, yy, width=None):
        """Zeilen, zu lange (Praxisname, Doppelname, lange Strasse) umbrochen – sonst liefen sie über den QR-Code
        bzw. über den Seitenrand (SIX: nichts darf in den QR-Code oder über den Zahlteil hinaus ragen)."""
        c.setFont(FONT, size)
        for t in items:
            for part in (_wrap(t, FONT, size, width)[:2] if width else [t]):
                c.drawString(xx, yy, part)
                yy -= size * 1.3
        return yy

    amount_y = y0 + (105 - 72) * mm

    def rows(items, size):
        return sum(len(_wrap(t, FONT, size, 52 * mm)[:2]) for t in items)

    # Empfangsschein: lange Namen und Adressen in 8 pt, wenn es passt, sonst kleiner (SIX: mind. 6 pt) – nie in den Betrag hinein
    for rs in (8, 7, 6):
        need = 3 * 6 * 1.35 + 2 * 2.5 * mm + rs * 1.3 * (rows([account, *creditor_lines], rs) + (1 if reference else 0)
                                                         + rows(debtor_lines or [], rs))
        if y - need >= amount_y + 9:
            break
    y = head(lab["account"], 6, x, y)
    y = lines([account, *creditor_lines], rs, x, y, 52 * mm) - 2.5 * mm
    if reference:
        y = head(lab["reference"], 6, x, y)
        y = lines([reference], rs, x, y) - 2.5 * mm
    y = head(lab["payable_by"] if debtor_lines else lab["payable_by_blank"], 6, x, y)
    if debtor_lines:
        lines(debtor_lines, rs, x, y, 52 * mm)
    c.setFont(FONT_BOLD, 6)
    c.drawString(x, amount_y, lab["currency"])
    c.drawString(x + 12 * mm, amount_y, lab["amount"])
    c.setFont(FONT, 8)
    c.drawString(x, amount_y - 3.5 * mm, currency)
    if amount:
        c.drawString(x + 12 * mm, amount_y - 3.5 * mm, amount)
    c.setFont(FONT_BOLD, 6)
    c.drawRightString(57 * mm, y0 + 18 * mm, lab["acceptance"])

    # ---------------- Zahlteil (148 mm)
    px = 67 * mm
    c.setFont(FONT_BOLD, 11)
    c.drawString(px, top - 5 * mm - 11, lab["payment"])
    # QR-Code 46 × 46 mm, Fehlerkorrektur M, Schweizerkreuz 7 × 7 mm in der Mitte
    qr = segno.make(qr_data, error="m", boost_error=False, micro=False, encoding="utf-8")
    matrix = [list(row) for row in qr.matrix_iter(border=0)]
    n = len(matrix)
    size = 46 * mm
    cell = size / n
    qx, qy = px, top - 17 * mm - size
    c.setFillColor(colors.black)
    for r, row in enumerate(matrix):
        for col, v in enumerate(row):
            if v:
                c.rect(qx + col * cell, qy + (n - 1 - r) * cell, cell + 0.01, cell + 0.01, stroke=0, fill=1)
    cs = 7 * mm
    cx, cy = qx + size / 2 - cs / 2, qy + size / 2 - cs / 2
    c.setFillColor(colors.white)
    c.rect(cx, cy, cs, cs, stroke=0, fill=1)
    inner = cs * 18.4 / 20
    c.setFillColor(colors.black)
    c.rect(cx + (cs - inner) / 2, cy + (cs - inner) / 2, inner, inner, stroke=0, fill=1)
    c.setFillColor(colors.white)
    arm_l, arm_w = cs * 11 / 20, cs * 3.3 / 20
    c.rect(cx + (cs - arm_w) / 2, cy + (cs - arm_l) / 2, arm_w, arm_l, stroke=0, fill=1)
    c.rect(cx + (cs - arm_l) / 2, cy + (cs - arm_w) / 2, arm_l, arm_w, stroke=0, fill=1)
    c.setFillColor(colors.black)
    # Betrag unter dem QR-Code
    c.setFont(FONT_BOLD, 8)
    c.drawString(px, amount_y, lab["currency"])
    c.drawString(px + 15 * mm, amount_y, lab["amount"])
    c.setFont(FONT, 10)
    c.drawString(px, amount_y - 4.5 * mm, currency)
    if amount:
        c.drawString(px + 15 * mm, amount_y - 4.5 * mm, amount)
    # Angaben rechts
    dx = 118 * mm
    y = top - 5 * mm - 8
    y = head(lab["account"], 8, dx, y)
    y = lines([account, *creditor_lines], 10, dx, y, 87 * mm) - 2.5 * mm
    if reference:
        y = head(lab["reference"], 8, dx, y)
        y = lines([reference], 10, dx, y) - 2.5 * mm
    if info:
        y = head(lab["info"], 8, dx, y)
        y = lines(_wrap(info, FONT, 10, 87 * mm)[:2], 10, dx, y) - 2.5 * mm
    y = head(lab["payable_by"] if debtor_lines else lab["payable_by_blank"], 8, dx, y)
    if debtor_lines:
        lines(debtor_lines, 10, dx, y, 87 * mm)
    c.restoreState()


def format_reference(ref: str) -> str:
    """QR-Referenz (27 Ziffern) in Blöcken: 2 + 5×5; SCOR (RF…) in Vierergruppen."""
    if ref.isdigit() and len(ref) == 27:
        return ref[:2] + " " + " ".join(ref[i:i + 5] for i in range(2, 27, 5))
    return " ".join(ref[i:i + 4] for i in range(0, len(ref), 4))


def format_iban(iban: str) -> str:
    s = iban.replace(" ", "")
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))
