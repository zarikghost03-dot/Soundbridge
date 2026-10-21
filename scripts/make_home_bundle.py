# -*- coding: utf-8 -*-
"""Собирает переносимый архив проекта для другого компьютера.

Берёт всё, кроме ``.venv``/``.git``/``__pycache__``, но сохраняет базу с
уже сделанным экспортом (``data/``), токены (``secrets/``) и выгрузку
(``out/yandex_export``). В архив кладётся инструкция ``КАК_ПРОДОЛЖИТЬ_ДОМА.txt``,
а ``SPOTIFY_PROXY`` в скопированном ``.env`` комментируется, чтобы на другом
компьютере программа не спотыкалась о несуществующий локальный прокси.

Запуск из корня проекта::

    .venv\\Scripts\\python.exe -X utf8 scripts/make_home_bundle.py
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "build", "dist"}

MEMO = """КАК ПРОДОЛЖИТЬ НА ДРУГОМ КОМПЬЮТЕРЕ
=====================================

В этом архиве уже есть: выгруженная библиотека Яндекса (5900 треков),
токены входа (Яндекс — рабочий, Spotify — пока нет) и все программы.

Шаг 1. Python
    Если Python 3.11+ ещё не установлен: скачайте с python.org и при
    установке отметьте галочку "Add python.exe to PATH".

Шаг 2. Распаковать
    Извлеките архив, например, в C:\\Soundbridge.

Шаг 3. Подготовить окружение (один раз)
    В папке Soundbridge откройте окно PowerShell (в проводнике: адресная
    строка -> наберите powershell -> Enter) и выполните:

        py -m venv .venv
        .venv\\Scripts\\python.exe -m pip install -r requirements.txt

Шаг 4. Вход в Spotify
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge auth --spotify-only --relogin

    Откроется браузер — подтвердите доступ. Если Spotify ответит
    "Spotify is unavailable in this country" — включите на этом компьютере
    VPN (например, Psiphon 3) в настройку .env добавьте строку:

        SPOTIFY_PROXY=http://127.0.0.1:10809

    (10808 — SOCKS-порт Psiphon, если вдруг понадобится).

Шаг 5. Дальше по порядку (VPN держите включённым)
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge export     # уже сделано, повтор безопасен
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge snapshot   # снимок вашего Spotify
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge match      # подбор пар (долгий, можно с --limit)
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge report     # отчёт для ручного ревью (CSV)
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge review     # интерактивное ревью
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge apply-overrides
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge plan
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge import --apply
        .venv\\Scripts\\python.exe -X utf8 -m soundbridge verify

    Прогресс сохраняется в data\\soundbridge.db — можно прерывать и
    продолжать, всё продолжится с того же места.

Безопасность
    Архив содержит ВАШИ токены доступа к Яндекс и Spotify. Храните его
    лично (флешка/личное облако), не пересылайте третьим лицам. Когда
    перенос закончится — удалите архив, а токены можно отозвать:
    Яндекс — id.yandex.ru/security/devices, Spotify — spotify.com/account/apps.
"""


def iter_files(base: Path):
    for p in sorted(base.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(base)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        yield p, rel


def main() -> int:
    stamp = date.today().isoformat()
    zip_path = ROOT.parent / f"Soundbridge_home_{stamp}.zip"
    with tempfile.TemporaryDirectory(prefix="sbbundle_") as tmp:
        stage = Path(tmp) / "Soundbridge"
        stage.mkdir()
        n = 0
        for src, rel in iter_files(ROOT):
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        env = stage / ".env"
        if env.exists():
            text = env.read_text(encoding="utf-8")
            text = re.sub(r"(?m)^SPOTIFY_PROXY=", "# SPOTIFY_PROXY=", text)
            env.write_text(text, encoding="utf-8")
        (stage / "КАК_ПРОДОЛЖИТЬ_ДОМА.txt").write_text(MEMO, encoding="utf-8")
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for p in stage.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(stage.parent))
    print(f"файлов упаковано: {n}")
    print(f"архив: {zip_path}  ({zip_path.stat().st_size / 1e6:.1f} МБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
