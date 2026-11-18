"""Kern: Daten der Praxis (Name, Adresse, Kanton, Sprache, GLN/ZSR, Hinweistexte)."""
from __future__ import annotations

from .audit import audit
from .db import Database
from .tenant import current_practice_id

EDITABLE = ("name", "address", "street", "house_no", "zip", "city", "phone", "email", "gln", "zsr", "canton", "default_lang",
            "reply_time_de", "reply_time_fr", "reply_time")
# Antwortzeit in der Patienten-App (Auswahl; Text in 11 Sprachen im Modul «messages», p.msg.reply.<wert>)
REPLY_TIMES = ("same_day", "next_day", "two_days", "three_days", "week")


def get(db: Database):
    return db.one("SELECT * FROM practices WHERE id = ?", (current_practice_id(),))


def update(db: Database, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k in EDITABLE}
    if not fields:
        return
    pid = current_practice_id()
    db.execute("UPDATE practices SET " + ", ".join(f"{k} = ?" for k in fields) + " WHERE id = ?", (*fields.values(), pid))
    audit(db, "update", "practice", pid)
