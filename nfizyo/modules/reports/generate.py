"""Berichtsentwurf aus vorhandenen Daten – ohne KI, nur mit bestätigten Daten der Akte.

Quellen: Verordnung, bestätigte Diagnosen/Operationen/Komplikationen/Einschränkungen, Messwerte (erster → letzter Wert),
durchgeführte Sitzungen, letzte freigegebene Notiz (Beurteilung, Plan), Therapieziel.
Der Entwurf ist ein Vorschlag: die Therapeutin / der Therapeut prüft, ergänzt und schliesst ab.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...core.db import Database
from ...core.timeutil import fmt_date
from ...core import patients as core_patients
from ..documentation import api as notes_api
from ..patients import api as patients_api


@dataclass
class Draft:
    subject: str
    salutation: str
    sections: dict
    prescription_id: int | None
    warnings: list[str] = field(default_factory=list)


def _who(t, patient, case: str = "") -> str:
    """Name mit Anrede; case="_dat" für den Dativ ("von Herrn …")."""
    title = t(f"reports.gen.title_f{case}") if patient["sex"] == "f" else (t(f"reports.gen.title_m{case}") if patient["sex"] == "m" else "")
    return f"{title} {patient['first_name']} {patient['last_name']}".strip()


def _sessions(db: Database, patient_id: int, prescription_id: int | None, with_agenda: bool):
    if not with_agenda:
        return None
    from ..agenda import api as agenda_api

    return agenda_api.done_span(db, patient_id, prescription_id)


def _side(t, side: str | None) -> str:
    return f" {t('common.side.' + side)}" if side in ("l", "r") else ""


def build(db: Database, i18n, patient, referrer, kind: str, lang: str, *, with_agenda: bool = True) -> Draft:
    t = lambda key, **kw: i18n.t(lang, key, **kw)  # noqa: E731
    d = lambda v: fmt_date(v, lang)  # noqa: E731
    who = _who(t, patient)
    warnings: list[str] = []

    pr = core_patients.active_prescription(db, patient["id"]) or next(iter(core_patients.prescriptions(db, patient["id"])), None)

    # Betreff und Anrede
    subject = t("reports.gen.subject_" + ("f" if patient["sex"] == "f" else "m"), name=f"{patient['first_name']} {patient['last_name']}", birthdate=d(patient["birthdate"]))
    if patient["case_number"]:
        subject += " · " + t("reports.gen.case", number=patient["case_number"])
    salutation = t("reports.gen.salutation") if referrer else t("reports.gen.salutation_neutral")

    # Diagnose
    dx: list[str] = []
    if pr and pr["diagnosis"]:
        dx.append(t("reports.gen.prescription_line", date=d(pr["issued_on"]), diagnosis=pr["diagnosis"])
                  if pr["issued_on"] else pr["diagnosis"])
    findings = patients_api.findings(db, patient["id"])
    for f in findings:
        if f["kind"] in ("diagnosis", "surgery", "complication"):
            line = f"– {f['title']}"
            if f["on_date"]:
                line += f" ({d(f['on_date'])})"
            if f["kind"] == "complication":
                line = f"– {t('reports.gen.complication')}: {f['title']}"
            dx.append(line)

    # Verlauf
    s = _sessions(db, patient["id"], pr["id"] if pr else None, with_agenda)
    course = [t(f"reports.gen.intro_{kind}", who=_who(t, patient, "_dat"))]
    if s and s["n"]:
        key = "reports.gen.sessions" + ("_of" if pr else "") + ("_single" if s["n"] == 1 else "")
        course.append(t(key, n=s["n"], total=pr["sessions"] if pr else 0, first=d(s["first"]), last=d(s["last"])))
    else:
        course.append(t("reports.gen.no_sessions"))
    if patient["goal"]:
        course.append(t("reports.gen.goal_" + ("f" if patient["sex"] == "f" else "m"), goal=patient["goal"]))

    # Messwerte
    lines = []
    for ser in patients_api.measurement_series(db, patient["id"]):
        pts = ser["points"]
        label = t("measure." + ser["kind"]) + (f" {t('joint.' + ser['joint'])}" if ser["joint"] else "") + _side(t, ser["side"])
        first = f"{_num(pts[0][1])}{ser['unit']}"
        line = f"– {label}: {first} ({d(pts[0][0])})"
        if len(pts) > 1:
            line += f" → {_num(pts[-1][1])}{ser['unit']} ({d(pts[-1][0])})"
        if ser["goal"] is not None:
            line += ", " + t("reports.gen.goal_value", value=f"{_num(ser['goal'])}{ser['unit']}")
        lines.append(line)
    findings_text = "\n".join(lines) if lines else t("reports.gen.no_measurements")

    # Letzte freigegebene Notiz
    note = next(iter(notes_api.assessments(db, patient["id"], limit=1)), None)
    assessment = (note["beurteilung"] or "").strip() if note else ""
    plan = (note["plan"] or "").strip() if note else ""
    if note and (assessment or plan) and note["author_lang"] != lang:
        warnings.append("other_language")
    if not assessment:
        warnings.append("no_assessment")

    # Procedere
    rec = [plan] if plan else []
    rec.append(t(f"reports.gen.rec_{kind}", who=who, sessions=pr["sessions"] if pr else 9))
    restrictions = [f for f in findings if f["kind"] == "restriction"]
    if restrictions and kind != "discharge":
        rec.append(t("reports.gen.restrictions"))
        for f in restrictions:
            rec.append(f"– {f['title']}" + (f" ({t('reports.gen.until', date=d(f['until']))})" if f["until"] else ""))

    return Draft(subject=subject, salutation=salutation, prescription_id=pr["id"] if pr else None, warnings=warnings,
                 sections={"diagnosis": "\n".join(dx), "course": "\n".join(course), "findings": findings_text,
                           "assessment": assessment, "recommendation": "\n".join(rec)})


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"
