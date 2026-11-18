"""Kern: offensichtliche Patientendaten in einem freien Text finden (z. B. Support-Anfrage an NSystem).

Gefunden werden (Kategorie → Beispiel):
  name       voller Name einer Patientin / eines Patienten DIESER Praxis («Lea Muster», «Muster Lea», «Muster, Lea»),
             Anrede + Nachname («Frau Muster», «Herr Örnek», «Mme Exemple», «M. Exemple», «Patientin Muster») und
             Initiale + Nachname («L. Muster»)
  number     Patientennummer der Praxis («P00004», «Dossier 1002») – ohne Datenbank nur das Muster P + 5 Ziffern
  birthdate  Datum, das wie ein Geburtsdatum aussieht (Jahr 1900 … vorletztes Jahr; 12.03.1985, 12-03-1985, 12. März 1985,
             Jg. 1985) oder ein Geburtsdatum der Praxis
  ahv        AHV-Nummer 756.xxxx.xxxx.xx (auch ohne Punkte oder mit Bindestrichen)
  insured    Versichertenkarte 80756… (20 Ziffern) oder eine Versicherten-/Schadennummer der Praxis
  phone      Telefonnummer (+41 / 0041 / 079 000 00 04 / 79 000 00 04 …)
  email      E-Mail-Adresse (auch «name(at)domain.ch»)
Schreibvarianten: unsichtbare Zeichen (weiches Trennzeichen, Breite-null-Leerzeichen) werden entfernt, alle Striche gelten als
«-», Umlaute auch umschrieben (ö = oe = o, ü = ue = u, ä = ae = a), Akzente zählen nicht («Ayse Oernek» = «Ayşe Örnek»).
Ein Vorname allein wird bewusst nicht blockiert (zu viele Fehlalarme – Entscheid der Praxisinhaberin / des Praxisinhabers).
Ohne Datenbank (NSystem-Übersicht) werden nur die Muster geprüft. Der Text selbst wird nirgends gespeichert oder protokolliert.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from .timeutil import today as zurich_today

CATEGORIES = ("name", "number", "birthdate", "ahv", "insured", "phone", "email")

EMAIL_RX = re.compile(r"[\w.+-]+(?:@|\s*[(\[{]\s*at\s*[)\]}]\s*)[\w-]+(?:\.[\w-]+)+", re.U | re.I)
AHV_RX = re.compile(r"(?<!\d)756[.\s-]?\d{4}[.\s-]?\d{4}[.\s-]?\d{2}(?!\d)")
CARD_RX = re.compile(r"(?<!\d)80756(?:[\s.-]?\d){15}(?!\d)")
DATE_DMY_RX = re.compile(r"(?<!\d)(\d{1,2})\s?([./-])\s?(\d{1,2})\s?\2\s?(\d{4}|\d{2})(?![\d.])")
DATE_ISO_RX = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
NUMBER_RUN_RX = re.compile(r"(?:\+|(?<![\w.]))[\d(][\d\s./()\-]{5,}\d")
PATIENT_NO_RX = re.compile(r"(?<![\w])P\d{5}(?![\w])")       # Standard-Nummern von N-Fizyo (core/patients.next_number)

_MONTHS = {
    1: ("januar", "janner", "jan", "janvier", "janv", "gennaio", "january"),
    2: ("februar", "feber", "feb", "fevrier", "fevr", "fev", "febbraio", "february"),
    3: ("marz", "maerz", "mrz", "mar", "mars", "marzo", "march"),
    4: ("april", "apr", "avril", "avr", "aprile"),
    5: ("mai", "maggio", "may"),
    6: ("juni", "jun", "juin", "giugno", "june"),
    7: ("juli", "jul", "juillet", "juil", "luglio", "july"),
    8: ("august", "aug", "aout", "agosto"),
    9: ("september", "sept", "sep", "septembre", "settembre"),
    10: ("oktober", "okt", "oct", "octobre", "ottobre", "october"),
    11: ("november", "nov", "novembre"),
    12: ("dezember", "dez", "dec", "decembre", "dicembre", "december"),
}
_MONTH_OF = {name: n for n, names in _MONTHS.items() for name in names}
DATE_MONTH_RX = re.compile(
    r"(?<!\d)(\d{1,2})\s*\.?\s*(?:er\s+)?(" + "|".join(sorted(_MONTH_OF, key=len, reverse=True)) + r")\b\.?\s*(\d{4})(?!\d)")
YEAR_OF_BIRTH_RX = re.compile(r"(?<![\w])(?:jg|jahrgang|jahrg|geb|geboren|nee?\s+en)\b\.?\s*(?:am\s+|im\s+jahr\s+)?(\d{4}|\d{2})(?!\d)")
MOBILE_NO_ZERO_RX = re.compile(r"(?<![\w.+])7[5-9](?:[\s./-]?\d){7}(?![\d])")   # 79 123 45 67 (ohne 0)

# Anreden vor einem Nachnamen (nach dem Vereinfachen: Kleinbuchstaben, ohne Punkte)
SALUTATIONS = ("frau", "fr", "herr", "herrn", "hr", "hrn", "mme", "madame", "mlle", "mademoiselle", "monsieur", "m", "mr", "mrs",
               "ms", "signora", "signor", "sig", "sigra", "patient", "patientin", "patienten", "pat", "patiente", "familie", "fam",
               "famille", "dossier")
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\ufe58\ufe63\uff0d"), "-")
_LETTERS = str.maketrans({"ı": "i", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l", "ð": "d", "þ": "th"})


def _clean(text: str) -> str:
    """Unsichtbare Formatzeichen weg (weiches Trennzeichen, Breite-null-Leerzeichen …), alle Striche als «-», Leerzeichen als « »."""
    out = []
    for c in text.translate(_DASHES):
        cat = unicodedata.category(c)
        if cat == "Cf":
            continue
        out.append(" " if cat == "Zs" else c)
    return "".join(out)


def _ascii(text: str) -> str:
    """Kleinbuchstaben ohne Akzente; Satzzeichen bleiben («12. März» → «12. marz»)."""
    t = unicodedata.normalize("NFKD", _clean(text).casefold().replace("ß", "ss").translate(_LETTERS))
    return "".join(c for c in t if not unicodedata.combining(c))


def _umlauts(text: str) -> str:
    """Umschriebene Umlaute gleich behandeln: ae → a, oe → o, ue → u (auf beiden Seiten des Vergleichs)."""
    return text.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def _fold(text: str) -> str:
    """Kleinbuchstaben, ohne Akzente, Umlaute vereinheitlicht, nur Buchstaben/Ziffern mit einfachen Leerzeichen
    («Ayşe Örnek» → «ayse ornek», «Mueller» → «muller»)."""
    t = _umlauts(_ascii(text))
    return " ".join(re.sub(r"[^\w]+", " ", t, flags=re.U).replace("_", " ").split())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _year(y: str) -> int:
    if len(y) == 4:
        return int(y)
    yy, now = int(y), zurich_today().year % 100
    return 2000 + yy if yy <= now else 1900 + yy


def _dates(text: str) -> list[date]:
    out = []
    for m in DATE_DMY_RX.finditer(text):
        try:
            out.append(date(_year(m.group(4)), int(m.group(3)), int(m.group(1))))
        except ValueError:
            continue
    for m in DATE_ISO_RX.finditer(text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    for m in DATE_MONTH_RX.finditer(_ascii(text)):
        try:
            out.append(date(int(m.group(3)), _MONTH_OF[m.group(2)], int(m.group(1))))
        except ValueError:
            continue
    return out


def _phone(run: str) -> bool:
    d = _digits(run)
    s = run.lstrip("(")
    if s.startswith("+") or s.startswith("00"):
        return 9 <= len(d) <= 15
    return s.startswith("0") and len(d) == 10


def _number_rx(number: str) -> re.Pattern | None:
    """Suchmuster für eine Patientennummer der Praxis. «P00004» auch als «p 00004»/«P-00004»; reine Ziffern («1002») nur mit
    Hinweiswort davor (Dossier, Akte, Nr., Patient …), sonst wären Fehlernummern und Beträge betroffen."""
    n = (number or "").strip()
    m = re.fullmatch(r"([A-Za-z]{1,4})[\s.-]?(\d{2,})", n)
    if m:
        return re.compile(rf"(?<![\w]){re.escape(m.group(1))}[\s.-]?{m.group(2)}(?![\w])", re.I)
    if re.fullmatch(r"\d{3,}", n):
        return re.compile(rf"(?:dossier|akte|kartei|patient\w*|pat|nr|nummer|no|n°|num(?:e|é)ro|fall|id)\.?\s*[:#]?\s*0*{n}(?!\d)",
                          re.I)
    if len(n) >= 4 and re.search(r"\d", n):
        return re.compile(rf"(?<![\w]){re.escape(n)}(?![\w])", re.I)
    return None


def scan(text: str, db=None, practice_id: int | None = None, today: date | None = None) -> list[str]:
    """Kategorien gefundener Patientendaten (leere Liste = nichts gefunden)."""
    found: list[str] = []
    if not text or not text.strip():
        return found
    today = today or zurich_today()
    text = _clean(text)

    def add(cat: str) -> None:
        if cat not in found:
            found.append(cat)

    rest = text
    if EMAIL_RX.search(rest):
        add("email")
        rest = EMAIL_RX.sub(" ", rest)
    if AHV_RX.search(rest):
        add("ahv")
        rest = AHV_RX.sub(" ", rest)
    if CARD_RX.search(rest):
        add("insured")
        rest = CARD_RX.sub(" ", rest)
    if PATIENT_NO_RX.search(rest):
        add("number")
    patients = []
    if db is not None and practice_id is not None:
        patients = db.query("SELECT first_name, last_name, birthdate, insured_number, case_number, number FROM patients"
                            " WHERE practice_id = ?", (practice_id,))
    birthdays = {p["birthdate"] for p in patients if p["birthdate"]}
    for d in _dates(rest):
        if 1900 <= d.year <= today.year - 2 or d.isoformat() in birthdays:
            add("birthdate")
            break
    for m in YEAR_OF_BIRTH_RX.finditer(_ascii(rest)):
        if 1900 <= _year(m.group(1)) <= today.year - 2:
            add("birthdate")
            break
    no_dates = DATE_ISO_RX.sub(" ", DATE_DMY_RX.sub(" ", rest))
    if any(_phone(m.group(0)) for m in NUMBER_RUN_RX.finditer(no_dates)) or MOBILE_NO_ZERO_RX.search(no_dates):
        add("phone")
    if patients:
        folded = f" {_fold(text)} "
        dotted = _umlauts(_ascii(text))
        digits_only = _digits(text)
        compact = re.sub(r"[\s./-]", "", text.casefold())
        salutations = "|".join(sorted({_fold(s) for s in SALUTATIONS}, key=len, reverse=True))
        for p in patients:
            first, last = _fold(p["first_name"] or ""), _fold(p["last_name"] or "")
            if first and last and (f" {first} {last} " in folded or f" {last} {first} " in folded):
                add("name")
            if last and re.search(rf" (?:{salutations}) {re.escape(last)} ", folded):
                add("name")
            if first and last:
                last_rx = r"[^\w]+".join(re.escape(part) for part in last.split())
                if re.search(rf"(?<![\w.]){re.escape(first[0])}\.\s*{last_rx}(?![\w])", dotted):
                    add("name")
            for num in (p["insured_number"], p["case_number"]):
                n = re.sub(r"[\s./-]", "", (num or "").casefold())
                if len(n) >= 6 and (n in compact or (n.isdigit() and n in digits_only)):
                    add("insured")
            rx = _number_rx(p["number"] if "number" in p.keys() else None)
            if rx is not None and rx.search(text):
                add("number")
    return [c for c in CATEGORIES if c in found]
