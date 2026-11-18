#!/usr/bin/env bash
# Python-Pakete: feste Versionen mit Prüfsummen (requirements.lock) und Prüfung auf bekannte Lücken.
#
#   bash tools/deps.sh audit   # VOR JEDER VERSION: pip-audit gegen requirements.lock (bricht bei bekannter Lücke ab)
#   bash tools/deps.sh lock    # requirements.lock neu erzeugen (nach Änderung an requirements.txt oder für Updates)
#
# Die Lock-Datei wird mit Python 3.12 erzeugt (= Ubuntu 24.04 der Praxis-Boxen); sie funktioniert auch mit 3.13.
# deploy/install.sh und deploy/update.sh installieren nur daraus (pip --require-hashes).
# Werkzeuge: python3.12 -m pip install pip-tools  ·  python3 -m pip install pip-audit
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

case "${1:-}" in
  audit)
    command -v pip-audit >/dev/null || { echo "pip-audit fehlt: python3 -m pip install pip-audit" >&2; exit 2; }
    pip-audit -r requirements.lock --require-hashes --disable-pip --strict
    echo "Hinweis: KI-Pakete (requirements-ai.txt, nur auf GPU-Boxen) zusätzlich: pip-audit -r requirements-ai.txt"
    ;;
  lock)
    PY=${PYTHON_LOCK:-python3.12}
    command -v "$PY" >/dev/null || { echo "$PY fehlt (Lock-Datei muss zur Python-Version der Boxen passen)." >&2; exit 2; }
    "$PY" -m piptools compile --quiet --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url \
      --upgrade --output-file requirements.lock requirements.txt
    echo "requirements.lock erneuert – danach: bash tools/deps.sh audit und alle Tests."
    ;;
  *)
    sed -n '2,9p' "$0"
    exit 1
    ;;
esac
