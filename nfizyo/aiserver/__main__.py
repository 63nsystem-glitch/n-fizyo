"""KI-Server bedienen.

  python -m nfizyo.aiserver --env /etc/n-fizyo-ai/ai.env run
  python -m nfizyo.aiserver --env … add-client --name "Physio Muster"   # Token wird EINMAL angezeigt
  python -m nfizyo.aiserver --env … list-clients
  python -m nfizyo.aiserver --env … disable-client --id 3
  python -m nfizyo.aiserver --env … usage --days 30
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from ..core.config import _read_env_file
from .store import Store


def _env(path: str | None) -> dict[str, str]:
    values = _read_env_file(Path(path)) if path else {}
    values.update({k: v for k, v in os.environ.items() if k.startswith(("NAI_", "NFIZYO_"))})
    return values


def _store(env: dict) -> Store:
    return Store(Path(env.get("NAI_DATA_DIR", "/var/lib/n-fizyo-ai")) / "ai-server.sqlite3")


def cmd_run(env: dict) -> None:
    from ..ai.llm import OllamaLLM
    from ..ai.stt import make_stt
    from ..ai.translate import OllamaTranslator
    from .app import Engines, create_ai_app

    ollama = env.get("NFIZYO_OLLAMA_URL", "http://127.0.0.1:11434")
    stt_model, llm_model = env.get("NFIZYO_STT_MODEL", "large-v3-turbo"), env.get("NFIZYO_LLM_MODEL", "qwen2.5:7b-instruct")
    tr_model = env.get("NFIZYO_TRANSLATE_MODEL", "translategemma:12b")
    engines = Engines(
        stt=make_stt("faster_whisper", stt_model, env.get("NFIZYO_STT_DEVICE", "cuda")),
        llm=OllamaLLM(ollama, llm_model),  # Ollama nur lokal auf dem KI-Server
        translator=OllamaTranslator(OllamaLLM(ollama, tr_model, timeout=60)),
        stt_model=stt_model, llm_model=llm_model, translate_model=tr_model,
    )
    data = Path(env.get("NAI_DATA_DIR", "/var/lib/n-fizyo-ai"))
    app = create_ai_app(_store(env), engines, data / "tmp")
    web.run_app(app, host=env.get("NAI_HOST", "127.0.0.1"), port=int(env.get("NAI_PORT", "8090")), access_log=None)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="nfizyo.aiserver", description="N-Fizyo KI-Server")
    p.add_argument("--env", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    a = sub.add_parser("add-client"); a.add_argument("--name", required=True)
    sub.add_parser("list-clients")
    d = sub.add_parser("disable-client"); d.add_argument("--id", type=int, required=True)
    e = sub.add_parser("enable-client"); e.add_argument("--id", type=int, required=True)
    u = sub.add_parser("usage"); u.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    env = _env(args.env)
    if args.cmd == "run":
        cmd_run(env)
    elif args.cmd == "add-client":
        cid, token = _store(env).add_client(args.name)
        print(f"Praxis #{cid} «{args.name}» angelegt.")
        print("Zugangs-Token (wird nur JETZT angezeigt – direkt in die .env der Praxis-Box eintragen,")
        print("nie per E-Mail oder Chat weitergeben):")
        print(token)
    elif args.cmd == "list-clients":
        for c in _store(env).clients():
            print(f"#{c['id']:>3}  {'aktiv ' if c['active'] else 'gesperrt'}  {c['created_at'][:10]}  {c['name']}")
    elif args.cmd in ("disable-client", "enable-client"):
        ok = _store(env).set_active(args.id, args.cmd == "enable-client")
        print("OK" if ok else "Nicht gefunden")
    elif args.cmd == "usage":
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        for r in _store(env).usage(since):
            print(f"{r['name']:<30} {r['op']:<10} {r['n']:>6}×  {r['ms'] / 1000 / 60:>7.1f} min Rechenzeit  {r['audio_min']:>7} min Audio")


if __name__ == "__main__":
    main()
