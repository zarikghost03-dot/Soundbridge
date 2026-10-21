# -*- coding: utf-8 -*-
"""Ручной вход Spotify без локального браузера (можно подтвердить с телефона).

Два шага, состояние (PKCE verifier + state) переживает между ними:

    python scripts/manual_auth.py new      # печатает ссылку в out/login_url.txt
    python scripts/manual_auth.py finish "<адрес из строки браузера>"

Пользователь открывает ссылку где угодно, жмёт Allow, браузер показывает
несуществующий адрес 127.0.0.1:8899/callback?code=... — этот адрес целиком
и нужно вставить на втором шаге.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundbridge.config import OUT_DIR, SECRETS_DIR, ensure_dirs, load_config  # noqa: E402
from soundbridge.spotify_client import (  # noqa: E402
    TokenBundle,
    authorize_url,
    _exchange,
    require_client_id,
)

PENDING = SECRETS_DIR / "pending_pkce.json"
URL_FILE = OUT_DIR / "login_url.txt"


def cmd_new() -> int:
    ensure_dirs()
    cfg = load_config()
    state = __import__("secrets").token_urlsafe(16)
    url, verifier = authorize_url(cfg, state)
    PENDING.write_text(json.dumps({"state": state, "verifier": verifier}), encoding="utf-8")
    URL_FILE.write_text(url + "\n", encoding="utf-8")
    print(url, flush=True)
    print(f"\n(сохранено в {URL_FILE}; code меняем в течение ~5 минут)")
    return 0


def cmd_finish(raw: str) -> int:
    from urllib.parse import parse_qs, urlparse

    cfg = load_config()
    client_id = require_client_id(cfg)
    if not PENDING.exists():
        print("сначала выполните: manual_auth.py new")
        return 1
    pending = json.loads(PENDING.read_text(encoding="utf-8"))

    query = {k: v[0] for k, v in parse_qs(urlparse(raw.strip()).query).items()}
    if query.get("error"):
        print(f"Spotify вернул ошибку: {query['error']} {query.get('error_description', '')}")
        return 1
    if query.get("state") != pending["state"]:
        print("state не совпал — берите адрес из той же сессии (manual_auth.py new заново)")
        return 1
    code = query.get("code", "")
    if not code:
        print("в адресе нет code — скопируйте СТРОКУ АДРЕСА целиком")
        return 1

    bundle = TokenBundle.from_response(_exchange(cfg, code, pending["verifier"]), client_id)
    bundle.save()
    PENDING.unlink(missing_ok=True)
    print(f"Готово: токен Spotify сохранён (uid={bundle.user_id})")
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "finish":
        return cmd_finish(sys.argv[2])
    if len(sys.argv) >= 2 and sys.argv[1] == "new":
        return cmd_new()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
