"""Daten der Körperfigur (Muskelkarte und Skelett) – gemeinsamer Baustein für alle Bildschirme.

bodymap.json: Muskelregionen (aus react-body-highlighter, MIT, siehe third_party/).
skeleton.json: eigene Zeichnung (tools/gen_skeleton.py).
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
BODYMAP = json.loads((DATA_DIR / "bodymap.json").read_text(encoding="utf-8"))
SKELETON = json.loads((DATA_DIR / "skeleton.json").read_text(encoding="utf-8"))
