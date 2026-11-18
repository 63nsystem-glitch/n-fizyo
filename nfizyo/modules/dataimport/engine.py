"""Zuordnung der Spalten, Prüfung jeder Zeile (Probelauf) und Übernahme.

Jede Zeile wird zu: neu | doppelt (übersprungen) | Fehler (übersprungen) – mit Hinweisen (z. B. ungültige AHV-Nummer,
Feld leer gelassen). Geschrieben wird nur über die Kern-Funktionen (Patient/innen, Versicherer, Zuweisende) und die
Schnittstelle der Agenda (Termine) und der Abrechnung (offene Posten). Verordnungen gehen in die Kern-Tabelle, inkl. der
schon in der bisherigen Software durchgeführten Sitzungen (sessions_before).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ...core import formats
from ...core import patients as core_patients
from ...core.swissids import clean_zsr
from ...core import sms as core_sms
from ...core.tenant import current_practice_id
from ...core.timeutil import TZ
from ...core.timeutil import today as zurich_today

KINDS = ("patients", "insurers", "referrers", "appointments", "prescriptions", "balances")
PATIENT_KEYS = ("patient_number", "last_name", "first_name", "birthdate")


@dataclass(frozen=True)
class Field:
    key: str
    required: bool = False
    synonyms: tuple[str, ...] = ()


FIELDS: dict[str, tuple[Field, ...]] = {
    "patients": (
        Field("number", synonyms=("nr", "patientennr", "patientennummer", "patnr", "pid", "no patient", "numero", "dossier")),
        Field("last_name", True, ("name", "nachname", "familienname", "nom", "nom de famille", "last name", "surname")),
        Field("first_name", True, ("vorname", "prenom", "first name", "given name")),
        Field("birthdate", synonyms=("geburtsdatum", "geb datum", "gebdat", "geboren", "date de naissance", "naissance", "birthdate", "dob")),
        Field("sex", synonyms=("geschlecht", "sexe", "gender", "anrede", "civilite")),
        Field("phone", synonyms=("telefon", "tel", "mobile", "natel", "handy", "telephone", "portable", "phone")),
        Field("email", synonyms=("e-mail", "mail", "courriel")),
        Field("address", synonyms=("adresse", "strasse", "rue", "street", "address")),
        Field("zip", synonyms=("plz", "npa", "postleitzahl", "zip", "code postal")),
        Field("city", synonyms=("ort", "wohnort", "localite", "ville", "city")),
        Field("lang", synonyms=("sprache", "langue", "language", "korrespondenzsprache")),
        Field("law", synonyms=("gesetz", "loi", "versicherungsart", "kostentrager art")),
        Field("insurer", synonyms=("versicherer", "versicherung", "krankenkasse", "kasse", "assureur", "caisse", "assurance")),
        Field("insured_number", synonyms=("versichertennummer", "versicherten nr", "kartennummer", "no assure", "numero d assure", "karte")),
        Field("ahv_number", synonyms=("ahv", "ahv nr", "ahv nummer", "avs", "no avs", "sozialversicherungsnummer")),
        Field("case_number", synonyms=("schadennummer", "schaden nr", "fallnummer", "no sinistre", "sinistre")),
        Field("accident_date", synonyms=("unfalldatum", "date accident", "accident")),
        Field("referrer", synonyms=("zuweiser", "zuweisender arzt", "arzt", "medecin", "prescripteur", "medecin prescripteur")),
        Field("note", synonyms=("bemerkung", "notiz", "remarque", "note", "kommentar")),
    ),
    "insurers": (
        Field("name", True, ("name", "versicherer", "versicherung", "assureur", "kasse")),
        Field("law", synonyms=("gesetz", "loi", "art")),
        Field("gln", synonyms=("gln", "ean")),
        Field("tiers", synonyms=("tiers", "verguetung", "remuneration", "tiers garant payant")),
        Field("street", synonyms=("strasse", "adresse", "rue")),
        Field("zip", synonyms=("plz", "npa")),
        Field("city", synonyms=("ort", "localite", "ville")),
    ),
    "referrers": (
        Field("name", True, ("name", "arzt", "aerztin", "medecin", "zuweiser")),
        Field("practice_name", synonyms=("praxis", "klinik", "spital", "cabinet", "institution")),
        Field("specialty", synonyms=("fachrichtung", "fachgebiet", "specialite")),
        Field("address", synonyms=("adresse", "strasse", "rue")),
        Field("phone", synonyms=("telefon", "tel", "telephone")),
        Field("hin_email", synonyms=("hin", "hin mail", "hin adresse", "e-mail", "email", "mail")),
        Field("gln", synonyms=("gln", "ean")),
        Field("zsr", synonyms=("zsr", "rcc", "kk nr")),
        Field("lang", synonyms=("sprache", "langue")),
    ),
    "appointments": (
        Field("patient_number", synonyms=("patientennr", "patnr", "patient nr", "pid", "no patient")),
        Field("last_name", synonyms=("name", "nachname", "nom")),
        Field("first_name", synonyms=("vorname", "prenom")),
        Field("birthdate", synonyms=("geburtsdatum", "date de naissance")),
        Field("date", True, ("datum", "date", "termin", "tag", "jour")),
        Field("start", True, ("von", "beginn", "start", "zeit", "uhrzeit", "heure", "debut")),
        Field("end", synonyms=("bis", "ende", "fin")),
        Field("minutes", synonyms=("dauer", "minuten", "duree", "min")),
        Field("therapist", True, ("therapeut", "therapeutin", "behandler", "mitarbeiter", "physiotherapeute", "therapeute", "kuerzel")),
        Field("location", synonyms=("standort", "ort", "lieu", "site")),
        Field("type", synonyms=("behandlung", "behandlungsart", "art", "leistung", "traitement", "type")),
        Field("status", synonyms=("status", "statut", "erledigt", "zustand")),
        Field("note", synonyms=("bemerkung", "notiz", "remarque", "note")),
    ),
    "prescriptions": (
        Field("patient_number", synonyms=("patientennr", "patnr", "patient nr", "pid", "no patient", "n patient")),
        Field("last_name", synonyms=("name", "nachname", "nom")),
        Field("first_name", synonyms=("vorname", "prenom")),
        Field("birthdate", synonyms=("geburtsdatum", "date de naissance")),
        Field("issued_on", True, ("ausgestellt", "ausstellungsdatum", "verordnungsdatum", "datum", "date", "date de prescription",
                                  "date ordonnance")),
        Field("prescriber", synonyms=("verordner", "verordnende", "arzt", "aerztin", "zuweiser", "medecin", "prescripteur")),
        Field("sessions", True, ("sitzungen", "anzahl sitzungen", "anzahl", "verordnet", "seances", "nombre de seances", "nb seances")),
        Field("sessions_used", synonyms=("durchgefuehrt", "durchgefuhrt", "erledigt", "bisher", "verbraucht", "gemacht", "seances effectuees",
                                         "effectuees", "utilisees")),
        Field("diagnosis", synonyms=("diagnose", "diagnostic", "indikation", "indication")),
    ),
    "balances": (
        Field("patient_number", synonyms=("patientennr", "patnr", "patient nr", "pid", "no patient", "n patient")),
        Field("last_name", synonyms=("name", "nachname", "nom")),
        Field("first_name", synonyms=("vorname", "prenom")),
        Field("birthdate", synonyms=("geburtsdatum", "date de naissance")),
        Field("invoice_number", True, ("rechnungsnummer", "rechnung nr", "rechnungsnr", "rechnung", "beleg", "belegnummer",
                                       "numero de facture", "no facture", "n facture", "n de facture", "facture")),
        Field("invoice_date", True, ("rechnungsdatum", "datum", "date", "date de facture", "date facture")),
        Field("amount_open", True, ("offen", "offener betrag", "saldo", "restbetrag", "ausstehend", "solde", "montant ouvert",
                                    "ouvert", "du")),
        Field("amount_total", synonyms=("betrag", "rechnungsbetrag", "total", "montant", "montant facture")),
        Field("due_on", synonyms=("faellig", "fallig", "faellig am", "zahlbar bis", "echeance", "date d echeance")),
        Field("reminder_level", synonyms=("mahnstufe", "mahnung", "mahnungen", "rappel", "niveau de rappel", "rappels")),
    ),
}


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def guess_mapping(kind: str, headers: list[str]) -> dict[str, int]:
    """Spalte je Feld erraten (Spaltenname = Feldname oder Synonym). Jede Spalte höchstens einmal."""
    norm = [_norm(h) for h in headers]
    used: set[int] = set()
    out: dict[str, int] = {}
    for f in FIELDS[kind]:
        candidates = [_norm(f.key.replace("_", " "))] + [_norm(s) for s in f.synonyms]
        for exact in (True, False):
            hit = next((i for i, h in enumerate(norm) if i not in used and h and (
                h in candidates if exact else any(c and (h.startswith(c) or c == h.split(" ")[0]) for c in candidates))), None)
            if hit is not None:
                out[f.key] = hit
                used.add(hit)
                break
    return out


# --------------------------------------------------------------------------- Werte lesen
def parse_date(v: str) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    v = re.split(r"[ T]", v)[0]   # «2026-03-01 10:00» und «2026-03-01T10:00:00» (ISO aus vielen Programmen)
    for pattern, order in ((r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", "ymd"), (r"(\d{1,2})[./](\d{1,2})[./](\d{4})", "dmy"),
                           (r"(\d{1,2})[./](\d{1,2})[./](\d{2})", "dmy2")):
        m = re.fullmatch(pattern, v)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        if order == "ymd":
            y, mo, d = a, b, c
        else:
            d, mo, y = a, b, c
            if order == "dmy2":
                y += 2000 if y <= zurich_today().year % 100 else 1900
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{5}", v):  # Excel-Seriennummer
        try:
            return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
        except OverflowError:
            return None
    return None


def parse_time(v: str) -> str | None:
    v = (v or "").strip()
    if re.fullmatch(r"0?\.\d{3,}", v):   # Excel-Zeit als Tagesbruchteil (Zelle im Format «Standard»): 0.416666667 = 10:00
        mins = round(float(v) * 1440)
        return f"{mins // 60:02d}:{mins % 60:02d}" if mins < 1440 else None
    m = re.search(r"(\d{1,2})[:.h](\d{2})", v) or re.fullmatch(r"(\d{1,2})(\d{2})", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_sex(v: str) -> str | None:
    n = _norm(v)
    if n in ("m", "mann", "maennlich", "mannlich", "herr", "h", "homme", "male", "masculin", "monsieur", "hr"):
        return "m"
    if n in ("w", "f", "frau", "weiblich", "femme", "female", "feminin", "madame", "mme", "fr"):
        return "f"
    if n in ("x", "d", "divers", "diverse", "other", "autre"):
        return "x"
    return None


LAWS = {"kvg": "KVG", "lamal": "KVG", "uvg": "UVG", "laa": "UVG", "ivg": "IVG", "lai": "IVG", "mvg": "MVG", "lam": "MVG",
        "vvg": "VVG", "lca": "VVG", "privat": "privat", "prive": "privat"}
LANGS = {"de": "de", "d": "de", "deutsch": "de", "allemand": "de", "german": "de", "fr": "fr", "f": "fr", "franzosisch": "fr",
         "francais": "fr", "french": "fr", "it": "it", "i": "it", "italienisch": "it", "italien": "it", "italiano": "it",
         "en": "en", "e": "en", "englisch": "en", "anglais": "en", "english": "en"}
STATUS = {"erledigt": "done", "durchgefuhrt": "done", "durchgefuehrt": "done", "done": "done", "effectue": "done", "fait": "done",
          "ja": "done", "x": "done", "1": "done", "abgesagt": "cancelled", "storniert": "cancelled", "annule": "cancelled",
          "cancelled": "cancelled", "nicht erschienen": "no_show", "absent": "no_show", "no show": "no_show",
          "geplant": "booked", "gebucht": "booked", "offen": "booked", "prevu": "booked", "booked": "booked", "": None}


_STATUS_PARTS = (("nicht erschien", "no_show"), ("nicht wahrgenommen", "no_show"), ("no show", "no_show"), ("noshow", "no_show"),
                 ("pas present", "no_show"), ("absent", "no_show"), ("abgesagt", "cancelled"), ("absage", "cancelled"),
                 ("annul", "cancelled"), ("storn", "cancelled"), ("verschoben", "cancelled"), ("ausgefallen", "cancelled"),
                 ("entschuldigt", "cancelled"), ("excuse", "cancelled"), ("deplace", "cancelled"), ("cancel", "cancelled"),
                 ("erledigt", "done"), ("durchgef", "done"), ("effectu", "done"), ("abgerechnet", "done"), ("facture", "done"),
                 ("geplant", "booked"), ("gebucht", "booked"), ("bestatigt", "booked"), ("confirme", "booked"), ("reserv", "booked"))


def _status_guess(text: str) -> str | None:
    """Statustexte aus anderen Programmen («abgesagt (Patient)», «Termin verschoben», «facturé»). Zuerst die Absagen."""
    for part, status in _STATUS_PARTS:
        if part in text:
            return status
    return None


def parse_amount(v: str) -> float | None:
    """CHF-Betrag: «1'290.50», «1290,50», «CHF 120.–», «-12.00» → float (None = unklar)."""
    t = (v or "").strip().replace("CHF", "").replace("Fr.", "").replace("'", "").replace("’", "").replace(" ", "")
    t = t.replace(".–", ".00").replace(".-", ".00")
    if "," in t and "." in t:
        # beide Zeichen: das hintere trennt die Rappen – «1.290,50» (deutsches Format) wie «1,290.50»
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d{1,2}", t):
        t = t.replace(",", ".")
    elif re.fullmatch(r"-?[1-9]\d{0,2}(\.\d{3})+", t):
        t = t.replace(".", "")          # «1.290» = 1290 (Franken haben nie drei Nachkommastellen)
    t = t.replace(",", "")
    try:
        return round(float(t), 2) if t else None
    except ValueError:
        return None


def parse_int(v: str) -> int | None:
    t = re.sub(r"\s", "", v or "")
    return int(t) if re.fullmatch(r"\d{1,4}", t) else None


@dataclass
class Row:
    line: int                         # Zeilennummer in der Datei (mit Kopfzeile = 1)
    status: str = "new"               # new | duplicate | error
    values: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _get(raw: list[str], mapping: dict[str, int], key: str) -> str:
    i = mapping.get(key)
    return raw[i].strip() if i is not None and i < len(raw) else ""


def _ctx(db) -> dict:
    """Nachschlagetabellen der Praxis (einmal pro Lauf)."""
    all_ins = core_patients.all_insurers(db)
    insurers = {_norm(i["name"]): i["id"] for i in all_ins}
    insurer_law = {i["id"]: i["law"] for i in all_ins}
    referrers = {_norm(r["name"]): r["id"] for r in db.query("SELECT id, name FROM referrers WHERE practice_id = ?",
                                                              (current_practice_id(),))}
    therapists = {}
    for u in core_patients.therapists(db):
        for k in (u["name"], u["email"], u["initials"], u["email"].split("@")[0]):
            therapists[_norm(k)] = u["id"]
    return {"insurers": insurers, "insurer_law": insurer_law, "referrers": referrers, "therapists": therapists}


def _names(raw: list[str], m: dict[str, int]) -> tuple[str, str]:
    """(Vorname, Name). Viele Programme exportieren eine Spalte «Muster, Lea» (Name, Vorname): dann wird sie geteilt –
    wenn die Vorname-Spalte fehlt, leer ist oder dieselbe Spalte ist."""
    first, last = _get(raw, m, "first_name"), _get(raw, m, "last_name")
    if "," in last and (not first or m.get("first_name") == m.get("last_name")):
        a, b = (x.strip() for x in last.split(",", 1))
        if a and b:
            return b, a
    return first, last


def _find_patient(db, raw, m, r: Row) -> int | None:
    """Patient/in über Patientennummer, sonst Name + Vorname (+ Geburtsdatum). Setzt Fehler in r."""
    pid = None
    number = _get(raw, m, "patient_number")
    if number:
        pid = db.value("SELECT id FROM patients WHERE practice_id = ? AND number = ?", (current_practice_id(), number))
    if pid is None:
        first, last = _names(raw, m)
        bd = parse_date(_get(raw, m, "birthdate"))
        found = core_patients.find_exact(db, first, last, bd) if first and last else []
        if len(found) == 1:
            pid = found[0]["id"]
        elif len(found) > 1:
            r.status, r.notes = "error", ["ambiguous_patient"]
            return None
    if pid is None:
        r.status, r.notes = "error", ["unknown_patient"]
    return pid


def _patient_label(db, pid: int) -> str:
    p = db.one("SELECT first_name, last_name FROM patients WHERE id = ? AND practice_id = ?", (pid, current_practice_id()))
    return f"{p['last_name']}, {p['first_name']}" if p else ""


def check(db, kind: str, rows: list[list[str]], mapping: dict[str, int], agenda=None, billing=None) -> list[Row]:
    """Probelauf: nichts wird gespeichert."""
    ctx = _ctx(db)
    ctx["billing"] = billing
    if kind == "appointments" and agenda is not None:
        lt = agenda.lookup_tables(db)
        ctx["locations"] = {_norm(r["name"]): r["id"] for r in lt["locations"]}
        ctx["types"] = {}
        for r in lt["types"]:
            ctx["types"][_norm(r["name_de"])] = (r["id"], r["minutes"])
            ctx["types"][_norm(r["name_fr"])] = (r["id"], r["minutes"])
    out: list[Row] = []
    seen: set = set()
    for n, raw in enumerate(rows, start=2):
        r = Row(line=n)
        getattr(_Checks, kind)(db, raw, mapping, r, ctx)
        key = r.values.get("_key")
        if r.status == "new" and key is not None:
            if key in seen:
                r.status = "duplicate"
                r.notes.append("dup_in_file")
            seen.add(key)
        out.append(r)
    return out


class _Checks:
    @staticmethod
    def patients(db, raw, m, r: Row, ctx) -> None:
        first, last = _names(raw, m)
        if not first or not last:
            r.status, r.notes = "error", ["missing_name"]
            return
        bd_raw = _get(raw, m, "birthdate")
        bd = parse_date(bd_raw)
        if bd_raw and not bd:
            r.notes.append("bad_birthdate")
        v = {"first_name": first[:80], "last_name": last[:80], "birthdate": bd, "sex": parse_sex(_get(raw, m, "sex")),
             "phone": _get(raw, m, "phone")[:40] or None, "email": _get(raw, m, "email")[:200] or None,
             "address": _get(raw, m, "address")[:200] or None, "zip": _get(raw, m, "zip")[:9] or None,
             "city": _get(raw, m, "city")[:80] or None, "lang": LANGS.get(_norm(_get(raw, m, "lang")), "de"),
             "law": LAWS.get(_norm(_get(raw, m, "law"))), "insured_number": _get(raw, m, "insured_number")[:40] or None,
             "case_number": _get(raw, m, "case_number")[:40] or None, "accident_date": parse_date(_get(raw, m, "accident_date")),
             "admin_note": _get(raw, m, "note")[:500] or None}
        if v["email"] and "@" not in v["email"]:
            v["email"] = None
            r.notes.append("bad_email")
        if v["phone"]:
            # Excel lässt die führende 0 weg (791234567): als Schweizer Nummer lesen und melden; Unmögliches nicht als
            # Telefonnummer speichern (sonst ginge die SMS ins Ausland) – die Angabe bleibt in der Bemerkung erhalten.
            number, how = core_sms.classify_phone(v["phone"])
            if number is None:
                r.notes.append("bad_phone")
                v["admin_note"] = ((v["admin_note"] + " · ") if v["admin_note"] else "") + f"Tel. {v['phone']} (?)"
                v["phone"] = None
            elif how == "guessed":
                r.notes.append("phone_guessed")
                v["phone"] = formats.phone(number)
            elif how == "foreign":
                r.notes.append("phone_foreign")
        ahv = _get(raw, m, "ahv_number")
        try:
            v["ahv_number"] = core_patients.clean_ahv(ahv)
        except ValueError:
            v["ahv_number"] = None
            r.notes.append("bad_ahv")
        ins = _get(raw, m, "insurer")
        if ins:
            v["insurer_id"] = ctx["insurers"].get(_norm(ins))
            if v["insurer_id"] is None:
                r.notes.append("unknown_insurer")
            elif not v["law"] and ctx["insurer_law"].get(v["insurer_id"]) in ("KVG", "UVG", "IVG", "MVG"):
                # Viele Exporte haben nur die Versicherung, kein Gesetz: ohne Gesetz liesse sich später nichts abrechnen
                v["law"] = ctx["insurer_law"][v["insurer_id"]]
                r.notes.append("law_from_insurer")
        ref = _get(raw, m, "referrer")
        if ref:
            v["referrer_id"] = ctx["referrers"].get(_norm(ref))
            if v["referrer_id"] is None:
                r.notes.append("unknown_referrer")
        number = _get(raw, m, "number")[:20]
        if number:
            if db.value("SELECT 1 FROM patients WHERE practice_id = ? AND number = ?", (current_practice_id(), number)):
                r.status = "duplicate"
                r.notes.append("dup_number")
            v["number"] = number
        if r.status == "new" and bd and core_patients.find_exact(db, first, last, bd):
            r.status = "duplicate"
            r.notes.append("dup_patient")
        elif r.status == "new" and not bd and not number and core_patients.find_exact(db, first, last):
            # Ohne Geburtsdatum und Nummer: gleicher Name schon da → nicht doppelt anlegen (zweiter Lauf derselben Datei)
            r.status = "duplicate"
            r.notes.append("dup_name")
        v["_key"] = (_norm(first), _norm(last), bd)
        r.values = v

    @staticmethod
    def insurers(db, raw, m, r: Row, ctx) -> None:
        name = _get(raw, m, "name")
        if not name:
            r.status, r.notes = "error", ["missing_name"]
            return
        law = LAWS.get(_norm(_get(raw, m, "law")))
        tiers_raw = _norm(_get(raw, m, "tiers"))
        tiers = "payant" if "payant" in tiers_raw or tiers_raw in ("tp",) or law in ("UVG", "IVG", "MVG") else "garant"
        gln = re.sub(r"\D", "", _get(raw, m, "gln"))
        if gln and len(gln) != 13:
            r.notes.append("bad_gln")
            gln = ""
        r.values = {"name": name[:120], "law": law if law != "privat" else None, "tiers": tiers, "gln": gln or None,
                    "street": _get(raw, m, "street")[:120] or None, "zip": _get(raw, m, "zip")[:9] or None,
                    "city": _get(raw, m, "city")[:80] or None, "_key": _norm(name)}
        if _norm(name) in ctx["insurers"]:
            r.status = "duplicate"
            r.notes.append("dup_insurer")

    @staticmethod
    def referrers(db, raw, m, r: Row, ctx) -> None:
        name = _get(raw, m, "name")
        if not name:
            r.status, r.notes = "error", ["missing_name"]
            return
        gln = re.sub(r"\D", "", _get(raw, m, "gln"))
        if gln and len(gln) != 13:
            r.notes.append("bad_gln")
            gln = ""
        hin = _get(raw, m, "hin_email")
        if hin and "@" not in hin:
            hin = ""
            r.notes.append("bad_email")
        r.values = {"name": name[:120], "practice_name": _get(raw, m, "practice_name")[:200] or None,
                    "specialty": _get(raw, m, "specialty")[:120] or None, "address": _get(raw, m, "address")[:500] or None,
                    "phone": _get(raw, m, "phone")[:40] or None, "hin_email": hin[:200] or None, "gln": gln or None,
                    "zsr": clean_zsr(_get(raw, m, "zsr")), "lang": LANGS.get(_norm(_get(raw, m, "lang")), "de"), "_key": _norm(name)}
        if _get(raw, m, "zsr") and r.values["zsr"] is None:
            r.notes.append("bad_zsr")
        if _norm(name) in ctx["referrers"]:
            r.status = "duplicate"
            r.notes.append("dup_referrer")

    @staticmethod
    def prescriptions(db, raw, m, r: Row, ctx) -> None:
        pid = _find_patient(db, raw, m, r)
        if pid is None:
            return
        issued = parse_date(_get(raw, m, "issued_on"))
        if not issued:
            r.status, r.notes = "error", ["bad_date"]
            return
        sessions = parse_int(_get(raw, m, "sessions"))
        if not sessions or sessions > 99:
            r.status, r.notes = "error", ["bad_sessions"]
            return
        used_raw = _get(raw, m, "sessions_used")
        used = parse_int(used_raw) if used_raw else 0
        if used is None:
            used = 0
            r.notes.append("bad_used")
        if used > sessions:
            used = sessions
            r.notes.append("used_more")
        ref = _get(raw, m, "prescriber")
        ref_id = ctx["referrers"].get(_norm(ref)) if ref else None
        if ref and ref_id is None:
            r.notes.append("unknown_referrer")
        r.values = {"patient_id": pid, "issued_on": issued, "sessions": sessions, "sessions_before": used, "referrer_id": ref_id,
                    "diagnosis": _get(raw, m, "diagnosis")[:300] or None, "status": "used" if used >= sessions else "active",
                    "_key": (pid, issued, sessions, ref_id),
                    "_label": f"{_patient_label(db, pid)}, {issued}, {used}/{sessions}"}
        if core_patients.prescription_exists(db, pid, issued, sessions, ref_id):
            r.status = "duplicate"
            r.notes.append("dup_prescription")

    @staticmethod
    def balances(db, raw, m, r: Row, ctx) -> None:
        pid = _find_patient(db, raw, m, r)
        if pid is None:
            return
        number = _get(raw, m, "invoice_number")[:30]
        if not number:
            r.status, r.notes = "error", ["missing_number"]
            return
        day = parse_date(_get(raw, m, "invoice_date"))
        if not day:
            r.status, r.notes = "error", ["bad_date"]
            return
        amount = parse_amount(_get(raw, m, "amount_open"))
        if amount is None or amount <= 0:
            r.status, r.notes = "error", ["bad_amount"]
            return
        total = parse_amount(_get(raw, m, "amount_total"))
        due_raw = _get(raw, m, "due_on")
        due = parse_date(due_raw)
        if due_raw and not due:
            r.notes.append("bad_due")
        level = parse_int(_get(raw, m, "reminder_level")) or 0
        if level > 3:
            level = 3
            r.notes.append("reminder_capped")
        r.values = {"patient_id": pid, "old_number": number, "invoice_date": day, "amount": amount, "original_total": total,
                    "due_on": due, "reminder_level": level, "_key": number,
                    "_label": f"{_patient_label(db, pid)}, {number}, CHF {amount:.2f}"}
        billing = ctx.get("billing")
        if billing is not None and billing.imported_exists(db, number):
            r.status = "duplicate"
            r.notes.append("dup_invoice")

    @staticmethod
    def appointments(db, raw, m, r: Row, ctx) -> None:
        pid = _find_patient(db, raw, m, r)
        if pid is None:
            return
        day, start = parse_date(_get(raw, m, "date")), parse_time(_get(raw, m, "start"))
        if not day or not start:
            r.status, r.notes = "error", ["bad_datetime"]
            return
        tid = ctx["therapists"].get(_norm(_get(raw, m, "therapist")))
        if tid is None:
            r.status, r.notes = "error", ["unknown_therapist"]
            return
        type_id, type_min = ctx.get("types", {}).get(_norm(_get(raw, m, "type")), (None, None))
        minutes = None
        end = parse_time(_get(raw, m, "end"))
        if end:
            h1, m1 = map(int, start.split(":"))
            h2, m2 = map(int, end.split(":"))
            minutes = (h2 * 60 + m2) - (h1 * 60 + m1)
        if not minutes or minutes <= 0:
            mins_raw = re.sub(r"\D", "", _get(raw, m, "minutes"))
            minutes = int(mins_raw) if mins_raw and len(mins_raw) <= 3 else None
            if mins_raw and not (minutes and 5 <= minutes <= 600):
                r.notes.append("bad_minutes")
                minutes = None
            minutes = minutes or type_min or 30
        status_raw = _norm(_get(raw, m, "status"))
        status = STATUS.get(status_raw) or _status_guess(status_raw)
        if status_raw and status is None:
            # Unbekannter Status: nicht raten – ein abgesagter Termin würde sonst als gebucht übernommen (mit Erinnerungs-SMS)
            r.status, r.notes = "error", ["unknown_status"]
            return
        start_dt = datetime.fromisoformat(f"{day}T{start}").replace(tzinfo=TZ)
        if status is None:
            status = "done" if start_dt < datetime.now(TZ) else "booked"
        loc = _get(raw, m, "location")
        loc_id = ctx.get("locations", {}).get(_norm(loc)) if loc else None
        if loc and loc_id is None:
            r.notes.append("unknown_location")
        r.values = {"patient_id": pid, "therapist_id": tid, "start": start_dt, "minutes": minutes, "status": status,
                    "location_id": loc_id, "treatment_type_id": type_id, "note": _get(raw, m, "note")[:500] or None,
                    "_key": (pid, start_dt.isoformat())}
        r.values["_utc"] = start_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def summary(rows: list[Row]) -> dict:
    out = {"new": 0, "duplicate": 0, "error": 0, "notes": {}}
    for r in rows:
        out[r.status] += 1
        for n in r.notes:
            out["notes"][n] = out["notes"].get(n, 0) + 1
    return out


def apply(db, kind: str, rows: list[Row], agenda=None, billing=None, options: dict | None = None) -> dict:
    """Übernahme der Zeilen mit Status «neu» in einer Transaktion.
    options["billed_externally"] (Termine, Standard ja): vergangene erledigte Termine sind im alten Programm schon verrechnet."""
    options = options or {}
    created = 0
    if kind == "prescriptions":  # älteste zuerst: die neueste Verordnung einer Person ist danach die aktuelle
        rows = sorted(rows, key=lambda r: r.values.get("issued_on") or "")
    lang = db.value("SELECT default_lang FROM practices WHERE id = ?", (current_practice_id(),), "de")
    linked = 0
    touched: set[int] = set()
    with db.tx():
        for r in rows:
            if r.status != "new":
                continue
            v = {k: val for k, val in r.values.items() if not k.startswith("_")}
            if kind == "patients":
                number = v.pop("number", None)
                if number and db.value("SELECT 1 FROM patients WHERE practice_id = ? AND number = ?",
                                       (current_practice_id(), number)):
                    number = None
                core_patients.create(db, {**v, **({"number": number} if number else {})})
            elif kind == "insurers":
                core_patients.save_insurer(db, v)
            elif kind == "referrers":
                core_patients.save_referrer(db, v)
            elif kind == "appointments":
                if agenda is None or agenda.exists_at(db, v["patient_id"], r.values["_utc"]):
                    r.status = "duplicate"
                    continue
                agenda.import_appointment(db, **v, billed_externally=bool(options.get("billed_externally", True)))
            elif kind == "prescriptions":
                if core_patients.prescription_exists(db, v["patient_id"], v["issued_on"], v["sessions"], v["referrer_id"]):
                    r.status = "duplicate"
                    continue
                touched.add(v["patient_id"])
                core_patients.add_prescription(db, v.pop("patient_id"), **v)
            elif kind == "balances":
                patient = core_patients.get(db, v.pop("patient_id"))
                if billing is None or patient is None or billing.import_open_item(db, patient, lang=lang, **v) is None:
                    r.status = "duplicate"
                    continue
            created += 1
        # Schon übernommene Termine mit der (neuesten) aktiven Verordnung verbinden – wie beim Buchen von Hand. So erscheinen
        # Sitzungsnummer und «ohne Verordnung» (z. B. 10/9) auch bei Terminen aus der bisherigen Software.
        if agenda is not None:
            for pid in sorted(touched):
                pr = core_patients.active_prescription(db, pid)
                if pr is not None:
                    linked += agenda.link_prescription(db, pid, pr["id"])
    return {"created": created, **({"linked": linked} if kind == "prescriptions" else {})}
