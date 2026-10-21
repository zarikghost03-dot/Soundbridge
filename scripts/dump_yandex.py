# -*- coding: utf-8 -*-
"""Полная читаемая выгрузка Яндекса из базы Soundbridge.

Создаёт в ``out/yandex_export/``:

* ``liked.csv``            — «Мне нравится» (все лайкнутые треки);
* ``playlists.csv``        — список плейлистов (название, сколько треков);
* ``playlist_NN_имя.csv``  — содержимое каждого плейлиста построчно;
* ``full.json``            — всё одним файлом (плейлисты + лайки + метаданные).

Запуск из корня проекта::

    .venv\\Scripts\\python.exe -X utf8 scripts/dump_yandex.py
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "soundbridge.db"
OUT = ROOT / "out" / "yandex_export"

TRACK_COLS = [
    "ym_id", "artists", "title", "version", "album",
    "duration_ms", "isrc", "explicit", "available",
]


def slugify(text: str, limit: int = 40) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text[:limit].strip("_") or "playlist"


def ms_to_mmss(ms: int | None) -> str:
    if not ms:
        return ""
    s = int(ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def fetch_tracks(con: sqlite3.Connection, kind: str) -> list[dict]:
    rows = con.execute(
        """select i.pos, t.ym_id, t.title, t.version, t.album, t.artists,
                  t.duration_ms, t.isrc, t.explicit, t.type, t.state,
                  t.available, t.track_source, t.n_refs, t.title_key,
                  t.artist_key, t.pair_key, t.raw
           from ym_playlist_item i
           left join ym_track t on t.ym_id = i.ym_id
           where i.kind = ? order by i.pos""",
        (kind,),
    ).fetchall()
    names = ["pos", "ym_id", "title", "version", "album", "artists",
             "duration_ms", "isrc", "explicit", "type", "state", "available",
             "track_source", "n_refs", "title_key", "artist_key", "pair_key",
             "raw"]
    out = []
    for r in rows:
        d = dict(zip(names, r))
        if d.get("title") is None:  # Яндекс не отдал детали — трек недоступен
            d["title"] = "(недоступен в Яндексе)"
            d["available"] = 0
        if isinstance(d.get("artists"), str) and d["artists"].startswith("["):
            try:  # в базе артисты лежат JSON-списком — людям показываем через запятую
                d["artists"] = ", ".join(json.loads(d["artists"]))
            except ValueError:
                pass
        d["duration"] = ms_to_mmss(d.get("duration_ms"))
        out.append(d)
    return out


def write_csv(path: Path, tracks: list[dict]) -> None:
    header = ["№", "ym_id", "Артист", "Трек", "Версия", "Альбом",
              "Длит.", "ISRC", "explicit", "доступен"]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(header)
        for t in tracks:
            w.writerow([
                t["pos"] + 1, t["ym_id"], t.get("artists") or "",
                t.get("title") or "", t.get("version") or "",
                t.get("album") or "", t.get("duration") or "",
                t.get("isrc") or "", "1" if t.get("explicit") else "",
                "да" if t.get("available") else "нет",
            ])


def main() -> int:
    if not DB.exists():
        print(f"нет базы {DB} — сначала выполните: python -m soundbridge export")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)

    playlists = con.execute(
        "select kind, title, n_items, is_likes, ordinal, description "
        "from ym_playlist order by ordinal"
    ).fetchall()

    bundle: dict = {
        "source": "yandex-music",
        "account_uid": None,
        "playlists": [],
        "liked": [],
    }
    liked_pair = [p for p in playlists if p[3]]
    regular = [p for p in playlists if not p[3]]

    with (OUT / "playlists.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["#", "Тип", "Название", "Треков"])
        n = 0
        for kind, title, n_items, is_likes, _ord, _desc in liked_pair + regular:
            tracks = fetch_tracks(con, kind)
            if is_likes:
                write_csv(OUT / "liked.csv", tracks)
                bundle["liked"] = [{k: t.get(k) for k in TRACK_COLS + ["duration"]}
                                   for t in tracks]
                w.writerow(["-", "Мне нравится", title, len(tracks)])
                continue
            n += 1
            fname = f"playlist_{n:02d}_{slugify(title)}.csv"
            write_csv(OUT / fname, tracks)
            w.writerow([n, "плейлист", title, len(tracks)])
            bundle["playlists"].append({
                "kind": kind, "title": title, "file": fname,
                "tracks": [{k: t.get(k) for k in TRACK_COLS + ["duration"]}
                           for t in tracks],
            })

    uid = con.execute("select value from meta where key='ym_uid'").fetchone()
    if uid:
        bundle["account_uid"] = uid[0]
    (OUT / "full.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"готово: {OUT}")
    print(f"  liked.csv        — {len(bundle['liked'])} треков")
    for pl in bundle["playlists"]:
        print(f"  {pl['file']:44} — {len(pl['tracks'])} треков")
    print(f"  full.json        — всё сразу")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
