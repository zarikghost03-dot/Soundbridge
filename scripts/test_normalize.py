"""Быстрая проверка нормализации: запускать из корня проекта.

    python scripts/test_normalize.py
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from soundbridge.normalize import TrackText  # noqa: E402

PAIRS = [
    ("Формейшн (Formation) [Official Video]", "iqosphere", "Formation", "IQOSPHERE"),
    ("Выхода нет", "Сплин", "Vyhoda Net", "Spleen"),
    ("Lose Yourself (2014 Remaster)", "Eminem", "Lose Yourself - 2014 Remastered", "Eminem"),
    ("Лесник (Live)", "Король и Шут", "Lesnik", "Korol i Shut"),
    ("Родной (feat. Тбили для двоих)", "Грот", "Родной", "Грот"),
    ("Меланья", "Мельница", "Меланья", "Мельница"),
    ("Video Games", "Lana Del Rey", "Video Games", "Lana Del Rey"),
    ("Город, которого нет", "Агата Кристи", "Город которого нет", "Agatha Kristie"),
    ("Интро", "Кино", "Intro", "Kino"),
    ("Shoes", "IOWA", "Обувки", "Ай-Уэ"),
    ("Пачка сигарет", "Кино", "Pachka Sigaret", "Kino"),
    ("Boom Boom Pow", "Black Eyed Peas", "Boom Boom Pow", "will.i.am,apl.de.ap,Fergie,Taboo"),
]


def main() -> int:
    fails = 0
    for ym_title, ym_artist, sp_title, sp_artist in PAIRS:
        ym = TrackText.build(ym_title, ym_artist, "", 200000, "RU-R1")
        sp = TrackText.build(sp_title, sp_artist, "", 201000, "RU-R1")
        title_hit = bool(set(ym.title_keys) & set(sp.title_keys))
        artist_hit = bool(set(ym.artist_keys) & set(sp.artist_keys))
        pair_hit = bool(set(ym.pair_keys) & set(sp.pair_keys))
        ok = title_hit or pair_hit
        fails += 0 if ok else 1
        flag = "ok " if ok else "MISS"
        print(
            f"{flag} {ym.display[:38]:40} | {ym.title_key!r} ~ {sp.title_key!r} "
            f"| a:{ym.artist_key!r}~{sp.artist_key!r} T{int(title_hit)} A{int(artist_hit)} P{int(pair_hit)}"
        )
    print("\nпромахов:", fails)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
