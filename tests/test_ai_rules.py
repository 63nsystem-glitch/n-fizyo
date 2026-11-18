"""KI-Regeln ohne Modell: Berichte lesen, Notizen entwerfen, Sicherheitsprüfungen."""
from __future__ import annotations

import re
from datetime import date

from nfizyo.ai import safety
from nfizyo.ai.extract import extract_rules
from nfizyo.ai.notes import draft_rules
from nfizyo.demo.seed_modules import OP_REPORT, REPORT_YVES
from nfizyo.knowledge import anatomy


def _fill(text: str) -> str:
    return text.format(**{k: "01.09.2026" for k in set(re.findall(r"\{(\w+)\}", text))})


def test_op_report_de():
    r = extract_rules(_fill(OP_REPORT), today=date(2026, 10, 28))
    kinds = [s.kind for s in r.suggestions]
    assert "diagnosis" in kinds and "surgery" in kinds and kinds.count("restriction") >= 2
    acl = [s for s in r.suggestions if s.kind == "surgery"][0]
    assert acl.structure_id == "acl" and acl.side == "r"
    open_chain = [s for s in r.suggestions if "offener Kette" in s.text][0]
    assert open_chain.until == date(2026, 11, 24)  # 12 Wochen nach OP (01.09.)
    assert any(s.kind == "prescription" and s.sessions == 9 for s in r.suggestions)
    for s in r.suggestions:  # jeder Vorschlag ist im Original belegt
        assert s.excerpt and s.excerpt in _fill(OP_REPORT)


def test_report_fr_complications_and_clearance():
    r = extract_rules(_fill(REPORT_YVES), today=date(2026, 10, 28))
    kinds = [s.kind for s in r.suggestions]
    assert "complication" in kinds and "clearance" in kinds and "restriction" in kinds
    assert any(s.structure_id == "supraspinatus" for s in r.suggestions)


def test_negation_is_respected():
    r = extract_rules("Befund: Nachtschmerz verneint. Kein Fieber. Keine Blasenstörung.", today=date(2026, 10, 28))
    assert not [s for s in r.suggestions if s.kind in ("red_flag", "complication")]


def test_note_draft_measurements_only_from_findings():
    d = draft_rules("Flexion 105 Grad, Extension minus 5 Grad. Schmerz VAS 3 von 10. Plan: Ziel Flexion 120 Grad.")
    values = sorted(m.value for m in d.measurements)
    assert 105 in values and -5 in values and 3 in values and 120 not in values


def test_invented_numbers_are_detected():
    assert safety.invented_numbers("Flexion 105°", "Flexion 110°")
    assert not safety.invented_numbers("Flexion 105°", "Flexion 105°")


def test_only_local_ai_urls():
    assert safety.is_local_url("http://127.0.0.1:11434")
    assert not safety.is_local_url("https://api.example.com")


def test_anatomy_lookup_de_fr_latin():
    for text in ("vorderes Kreuzband", "ligament croisé antérieur", "ligamentum cruciatum anterius", "VKB"):
        assert "acl" in [m.structure.id for m in anatomy.find_structures(text)], text
