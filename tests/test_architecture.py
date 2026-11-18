"""Bauregeln der Module (siehe core/modules.py). Diese Tests halten das Projekt «in Teilen»:

1. Ein Modul importiert nur Kern, ai/, knowledge/, sich selbst und die api.py von Modulen aus requires/uses.
2. Ein Modul schreibt nur in eigene Tabellen und liest nur eigene und Kern-Tabellen.
3. Jede Tabelle gehört genau einem Modul (Ausweis «tables» = CREATE TABLE in den eigenen Migrationen).
4. Ausweis vollständig: Version, Abhängigkeiten existieren, keine Kreise, Reihenfolge passt zu den Migrationen.
5. Vorlagen liegen unter templates/<modulname>/, Steckplätze und Ereignisse sind dokumentiert.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from nfizyo.core import events, slots
from nfizyo.core.modules import CORE_TABLES
from nfizyo.modules import MODULE_ORDER, load_modules

ROOT = Path(__file__).resolve().parent.parent / "nfizyo"
MODULES = {m.name: m for m in load_modules()}
ALLOWED_TOP = {"core", "ai", "knowledge"}

WRITE_RX = re.compile(r"\b(?:INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM)\s+([a-z_]+)\b")
READ_RX = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_]+)\b")
CREATE_RX = re.compile(r"\bCREATE TABLE\s+(?:IF NOT EXISTS\s+)?([a-z_]+)", re.I)


def module_files(name: str):
    return [p for p in (ROOT / "modules" / name).rglob("*.py") if "__pycache__" not in p.parts]


def imported_modules(path: Path) -> list[tuple[str, str]]:
    """(Ziel, Text) aller Importe, relativ zum Paket nfizyo aufgelöst."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = path.relative_to(ROOT.parent).with_suffix("").parts[:-1]  # z. B. ('nfizyo', 'modules', 'agenda')
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(pkg[: len(pkg) - node.level + 1])
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            for alias in node.names:
                out.append((f"{mod}.{alias.name}" if mod.startswith("nfizyo") else mod, ast.unparse(node)))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, ast.unparse(node)))
    return out


@pytest.mark.parametrize("name", MODULE_ORDER)
def test_imports_follow_the_rules(name):
    module = MODULES[name]
    allowed_apis = set(module.requires) | set(module.uses)
    problems = []
    for path in module_files(name):
        for target, text in imported_modules(path):
            if not target.startswith("nfizyo."):
                continue
            parts = target.split(".")
            if parts[1] in ALLOWED_TOP:
                continue
            if parts[1] == "modules" and len(parts) >= 3:
                other = parts[2]
                if other == name:
                    continue
                if other in allowed_apis and len(parts) >= 4 and parts[3] == "api":
                    continue
            problems.append(f"{path.relative_to(ROOT)}: {text}")
    assert not problems, "Unerlaubte Importe (nur api.py von requires/uses):\n" + "\n".join(problems)


@pytest.mark.parametrize("name", MODULE_ORDER)
def test_sql_touches_only_own_and_core_tables(name):
    own = set(MODULES[name].tables)
    problems = []
    for path in module_files(name):
        src = path.read_text(encoding="utf-8")
        for table in WRITE_RX.findall(src):
            if table not in own:
                problems.append(f"{path.relative_to(ROOT)}: schreibt in «{table}»")
        for table in READ_RX.findall(src):
            if table not in own and table not in CORE_TABLES:
                problems.append(f"{path.relative_to(ROOT)}: liest «{table}»")
    assert not problems, "Fremde Tabellen – bitte über api.py des Besitzers:\n" + "\n".join(sorted(set(problems)))


def test_every_table_belongs_to_exactly_one_module():
    owner: dict[str, str] = {}
    for name, module in MODULES.items():
        created = set()
        for sql in sorted(module.migrations_dir.glob("*.sql")) if module.migrations_dir.exists() else []:
            created |= {t.lower() for t in CREATE_RX.findall(sql.read_text(encoding="utf-8"))}
        assert created == set(module.tables), f"{name}: Ausweis tables={sorted(module.tables)}, Migrationen={sorted(created)}"
        for t in created:
            assert t not in owner, f"Tabelle {t} gehört {owner[t]} und {name}"
            assert t not in CORE_TABLES, f"Tabelle {t} ist eine Kern-Tabelle"
            owner[t] = name
    core_created = set()
    for sql in (ROOT / "core" / "migrations").glob("*.sql"):
        core_created |= {t.lower() for t in CREATE_RX.findall(sql.read_text(encoding="utf-8"))}
    assert core_created <= CORE_TABLES | {"schema_migrations"}, sorted(core_created - CORE_TABLES)


def test_manifests_are_complete_and_ordered():
    for i, name in enumerate(MODULE_ORDER):
        m = MODULES[name]
        assert re.fullmatch(r"\d+\.\d+\.\d+", m.version), f"{name}: Version {m.version!r}"
        for dep in (*m.requires, *m.uses):
            assert dep in MODULES, f"{name}: unbekanntes Modul {dep}"
            assert dep != name
        for dep in m.requires:
            assert MODULE_ORDER.index(dep) < i, f"{name} braucht {dep} – {dep} muss vorher kommen (Migrationen)"
        assert (m.package_dir / "__init__.py").exists()

    def visit(name, seen=()):
        assert name not in seen, f"Kreis in requires: {' → '.join(seen + (name,))}"
        for dep in MODULES[name].requires:
            visit(dep, seen + (name,))

    for name in MODULES:
        visit(name)


@pytest.mark.parametrize("name", MODULE_ORDER)
def test_templates_live_in_own_folder(name):
    tdir = MODULES[name].templates_dir
    if not tdir.exists():
        return
    for item in tdir.iterdir():
        assert item.name == name, f"{name}: Vorlagen gehören nach templates/{name}/ (gefunden: {item.name})"


def test_slots_and_events_are_documented():
    documented_slots = set(re.findall(r"^\s+([a-z]+\.[a-z.|]+)\s{2,}", slots.__doc__, re.M))
    expanded = set()
    for s in documented_slots:
        head, _, tail = s.rpartition(".")
        expanded |= {f"{head}.{t}" for t in tail.split("|")} if "|" in tail else {s}
    used = set()
    for path in (ROOT / "modules").rglob("*.py"):
        used |= set(re.findall(r'ctx\.slot\(\s*"([^"]+)"', path.read_text(encoding="utf-8")))
    assert used <= expanded, f"Undokumentierte Steckplätze (core/slots.py): {sorted(used - expanded)}"
    published = set()
    for path in ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        published |= set(re.findall(r'publish\(\s*[^,]+,\s*"([^"]+)"', src)) | set(re.findall(r'ctx\.on\(\s*"([^"]+)"', src))
    assert published <= events.KNOWN, sorted(published - events.KNOWN)


def test_switching_off_a_module_switches_off_dependents(app):
    from nfizyo.core.keys import DB
    from nfizyo.core.keys import MODULES as MODS
    from nfizyo.core.modules import module_enabled, set_module_enabled

    db = app[DB]
    set_module_enabled(db, 1, "dictation", False)
    assert not module_enabled(db, 1, "dictation", app[MODS])
    assert not module_enabled(db, 1, "patientinfo", app[MODS])  # braucht das Diktat
    assert module_enabled(db, 1, "reports", app[MODS])
    set_module_enabled(db, 1, "dictation", True)
    assert module_enabled(db, 1, "patientinfo", app[MODS])


async def test_disabled_module_disappears_from_screens(client, app):
    from conftest import login, pid_of

    from nfizyo.core.keys import DB
    from nfizyo.core.modules import set_module_enabled

    set_module_enabled(app[DB], 1, "messages", False)
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.get("/therapie/nachrichten", allow_redirects=False)
    assert r.status == 404
    r = await client.get(f"/therapie/patienten/{pid_of(app, 'Lea')}")
    assert r.status == 200 and "/therapie/nachrichten" not in await r.text()
