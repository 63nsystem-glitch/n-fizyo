"""Sicherheits-Kopfzeilen und «kein Inline-JavaScript» (Content-Security-Policy), damit eingeschleuster Text nie ausgeführt wird."""
from __future__ import annotations

import re
from pathlib import Path

from nfizyo.app import create_app
from nfizyo.core.config import load_settings

ROOT = Path(__file__).resolve().parent.parent / "nfizyo"


async def test_headers_on_staff_and_patient_pages(client):
    for url in ("/login", "/p/anmelden", "/p/datenschutz"):
        r = await client.get(url)
        csp = r.headers["Content-Security-Policy"]
        assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp, url
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
        assert "geolocation=()" in r.headers["Permissions-Policy"]
        assert "Strict-Transport-Security" not in r.headers                    # nur mit https


async def test_hsts_and_video_form_target_with_https(aiohttp_client, tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, base_url="https://physio.example.ch", secure_cookies=True,
                             video_url="https://video.example.ch", video_secret="s" * 40)
    client = await aiohttp_client(create_app(settings, start_workers=False))
    r = await client.get("/login")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")
    assert "form-action 'self' https://video.example.ch" in r.headers["Content-Security-Policy"]


def test_no_inline_javascript_in_templates():
    problems = []
    for f in ROOT.rglob("*.html"):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"<script(?P<attrs>[^>]*)>", src):
            attrs = m.group("attrs")
            if "src=" not in attrs and 'type="application/json"' not in attrs:
                problems.append(f"{f.relative_to(ROOT)}: Inline-<script>")
        if re.search(r"\son[a-z]+=\"", src):
            problems.append(f"{f.relative_to(ROOT)}: on…=-Attribut")
        if "javascript:" in src:
            problems.append(f"{f.relative_to(ROOT)}: javascript:-Link")
    assert not problems, "\n".join(problems)
