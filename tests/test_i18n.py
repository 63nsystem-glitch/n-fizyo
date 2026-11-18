"""Sprachen: Deutsch und Französisch vollständig; Patiententexte in allen 11 App-Sprachen; Platzhalter überall gleich."""
from __future__ import annotations

import re

from nfizyo.core.i18n import ALL_LANGS, PATIENT_LANGS, PATIENT_PREFIXES
from nfizyo.core.keys import I18N

PH = re.compile(r"\{(\w+)\}")


def test_no_missing_keys(app):
    missing = app[I18N].missing()
    assert all(not v for v in missing.values()), {k: v[:5] for k, v in missing.items() if v}


def test_patient_keys_in_all_app_languages(app):
    cat = app[I18N].catalog
    patient_keys = [k for k in cat["de"] if k.startswith(PATIENT_PREFIXES)]
    assert len(patient_keys) > 100
    for lang in PATIENT_LANGS:
        absent = [k for k in patient_keys if k not in cat[lang]]
        assert not absent, (lang, absent[:5])


def test_placeholders_match(app):
    cat = app[I18N].catalog
    for lang in ALL_LANGS:
        for key, text in cat[lang].items():
            if key in cat["de"]:
                assert set(PH.findall(text)) == set(PH.findall(cat["de"][key])), (lang, key)


def test_emergency_number_in_patient_app(app):
    cat = app[I18N].catalog
    for lang in PATIENT_LANGS:
        assert any("144" in v for k, v in cat[lang].items() if k.startswith("p.")), lang
