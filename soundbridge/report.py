"""Отчёты: CSV для ручного ревью, план переноса, статус конвейера.

`out/review.csv` открывается в Excel (UTF-8 BOM). Колонки `decision_uri` и
`decision_note` пользователь заполняет сам — файл сдаётся обратно командой
`review --apply`.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import Settings
from .storage import Database

CSV_COLUMNS = [
    "ym_id",
    "problem",
    "artists",
    "title",
    "version",
    "album",
    "duration_ms",
    "isrc",
    "times_used",
    "method",
    "confidence",
    "current_uri",
    "spotify_artist",
    "spotify_title",
    "spotify_album",
    "spotify_duration_ms",
    "candidate_1",
    "candidate_2",
    "candidate_3",
    "note",
    "decision_uri",
    "decision_note",
]

PROBLEM_LABELS = {
    "no_result": "не найдено",
    "review": "сомнительно",
    "fuzzy": "проверить (нечёткое совпадение)",
    None: "не сопоставлен",
}


def review_file(settings: Settings) -> Path:
    return settings.root / "out" / "review.csv"


def plan_file(settings: Settings) -> Path:
    return settings.root / "out" / "plan.md"


def _cand_text(row: sqlite3.Row, index: int) -> str:
    try:
        cands = json.loads(row["candidates"] or "[]")
    except ValueError:
        return ""
    if index >= len(cands):
        return ""
    c = cands[index]
    return "{} | {} — {} | {} мс | score {}".format(
        c.get("uri", ""), c.get("artists", ""), c.get("title", ""),
        c.get("duration_ms", ""), round(float(c.get("score", 0) or 0), 3),
    )


def write_review(settings: Settings, limit: int = 0) -> dict[str, Any]:
    """Выгружает всё, что требует решения человека."""
    db = Database(settings.db_path)
    try:
        rows = db.list_review()
        out = review_file(settings)
        out.parent.mkdir(parents=True, exist_ok=True)
        shown = rows[:limit] if limit else rows
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(CSV_COLUMNS)
            for row in shown:
                problem = PROBLEM_LABELS.get(row["method"], row["method"] or "?")
                writer.writerow([
                    row["ym_id"],
                    problem,
                    row["artists"],
                    row["title"],
                    row["version"] or "",
                    row["album"] or "",
                    row["duration_ms"] or 0,
                    row["isrc"] or "",
                    row["n_refs"] or 0,
                    row["method"] or "none",
                    round(float(row["confidence"] or 0), 3),
                    row["current_uri"] or row["uri"] or "",
                    row["sp_artists"] or "",
                    row["sp_name"] or "",
                    row["sp_album"] or "",
                    row["sp_duration_ms"] or "",
                    _cand_text(row, 0),
                    _cand_text(row, 1),
                    _cand_text(row, 2),
                    row["note"] or "",
                    "",
                    "",
                ])
        counts = db.review_counts()
        refs_pending = sum(int(r["n_refs"] or 0) for r in rows)
        return {"path": str(out), "rows": len(shown), "total": len(rows),
                "counts": counts, "refs_pending": refs_pending}
    finally:
        db.close()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"{path}: пустой файл (нет строки заголовков)")
        missing = [c for c in ("ym_id", "decision_uri") if c not in reader.fieldnames]
        if missing:
            raise SystemExit(
                f"{path}: нет колонок {missing}. Возьми свежий out/review.csv из команды `review`"
            )
        return [dict(r) for r in reader]


def apply_decisions(settings: Settings, path: Path | None = None,
                    dry_run: bool = False) -> dict[str, Any]:
    """Читает заполненный review.csv и переносит решения в таблицу `override`.

    `decision_uri` пустой → помечаем «не переносим» (снимает запись из match,
    чтобы импорт не подтянул старый ненадёжный вариант).
    """
    source = Path(path) if path else review_file(settings)
    if not source.exists():
        raise SystemExit(f"{source} не найден. Сначала выполни: python -m soundbridge review")

    db = Database(settings.db_path)
    try:
        known = {r["ym_id"] for r in db.conn.execute("SELECT ym_id FROM ym_track")}
        filled: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        for row in _read_csv(source):
            ym_id = (row.get("ym_id") or "").strip()
            if not ym_id:
                continue
            if ym_id not in known:
                skipped.append((ym_id, "нет такого трека в базе"))
                continue
            uri = (row.get("decision_uri") or "").strip()
            note = (row.get("decision_note") or "").strip()
            if uri and not uri.startswith("spotify:track:"):
                skipped.append((ym_id, f"неверный формат uri: {uri}"))
                continue
            filled.append((ym_id, uri, note))

        if dry_run:
            return {"would_apply": len(filled), "skipped": len(skipped), "dry_run": True,
                    "sample": filled[:10], "errors": skipped[:10]}

        result = db.apply_overrides([(x[0], x[1]) for x in filled])
        for ym_id, _uri, note in filled:
            if note:
                db.conn.execute(
                    "UPDATE override SET note=? WHERE ym_id=?", (note, ym_id)
                )
        db.commit()
        result["skipped"] = len(skipped)
        result["errors"] = skipped[:20]
        db.log_event("review_applied", result)
        return result
    finally:
        db.close()


def write_plan(settings: Settings) -> dict[str, Any]:
    """План переноса в Markdown: объёмы, плейлисты, риски."""
    db = Database(settings.db_path)
    try:
        counts = db.counts()
        resolved = db.resolved()
        methods = db.method_breakdown()
        playlists = db.playlists()
        vols = _volumes(db, settings)

        lines: list[str] = []
        lines.append("# План переноса Яндекса в Spotify\n")
        lines.append(f"Треков в библиотеке Яндекса: **{counts.get('ym_tracks', 0)}**")
        lines.append(f"Сопоставлено (будет перенесено): **{len(resolved)}**")
        lines.append(f"Ждут ревью: **{sum(methods.get(k, 0) for k in ('review', 'no_result'))}")
        lines.append("")
        lines.append("## Как найдены соответствия")
        for method, n in sorted(methods.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{method}`: {n}")
        lines.append("")
        lines.append("## Что создадим в Spotify")
        lines.append("")
        lines.append("| Источник (Яндекс) | Треков | Переносим | volumes |")
        lines.append("| --- | --- | --- | --- |")
        by_pl: dict[str, int] = {}
        for r in db.conn.execute(
            "SELECT pt.playlist_id, COUNT(*) AS n FROM playlist_track pt "
            "JOIN ym_track t ON t.ym_id = pt.ym_id GROUP BY pt.playlist_id"
        ):
            by_pl[r["playlist_id"]] = int(r["n"])
        res_ids = set(resolved.keys())
        for pl in playlists:
            items = db.playlist_items(pl["kind"]) if pl["kind"] == "likes" else None
            if items is None:
                ids = [r["ym_id"] for r in db.conn.execute(
                    "SELECT ym_id FROM playlist_track WHERE playlist_id=? ORDER BY ord",
                    (pl["id"],))]
            else:
                ids = []
                for uri in items:
                    row = db.track_of_uri(uri)
                    ids.append(row["ym_id"] if row else "")
            hit = sum(1 for i in ids if i in res_ids)
            n_vol = vols.get(pl["kind"], 1)
            lines.append(f"| {pl['kind']}: {pl['title']} | {len(ids)} | {hit} | {n_vol} |")
        lines.append("")
        lines.append(f"Плейлистов в Spotify будет создано: **{sum(vols.values())}**")
        lines.append("")
        lines.append("## Риски")
        lines.append("- Лимит Spotify — 10 000 треков в плейлисте, поэтому большие "
                      "плейлисты нарезаются на части.")
        lines.append("- Яндекс-коллабы и UGC-релизы часто отсутствуют в Spotify: "
                      "соответствующая доля треков останется в `no_result`.")
        lines.append("- Проверь `out/review.csv` перед `import --apply`.")
        path = plan_file(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"path": str(path), "resolved": len(resolved), "counts": counts}
    finally:
        db.close()


def _volumes(db: Database, settings: Settings) -> dict[str, int]:
    """Сколько плейлистов-частей понадобится на каждый источник."""
    import math

    from .importer import planned_volumes

    return planned_volumes(db, settings)


def status(settings: Settings) -> dict[str, Any]:
    """Сводка по стадиям конвейера — для `cli status`."""
    db = Database(settings.db_path)
    try:
        stages: dict[str, Any] = {}
        for row in db.conn.execute("SELECT job, state, updated_at FROM job"):
            try:
                parsed = json.loads(row["state"])
            except ValueError:
                parsed = {}
            stages[row["job"]] = {"state": parsed, "updated_at": row["updated_at"]}

        def done(job: str) -> bool:
            return bool(stages.get(job, {}).get("state", {}).get("done"))

        return {
            "counts": db.counts(),
            "methods": db.method_breakdown(),
            "review": db.review_counts(),
            "stages": stages,
            "stages_done": {
                "export": done("export"),
                "snapshot": done("snapshot"),
                "match": done("match"),
                "import": done("import"),
            },
            "meta": {
                "sp_snapshot_at": db.get_meta("sp_snapshot_at"),
                "sp_liked_total": db.get_meta_int("sp_liked_total"),
                "likes_revision": db.get_meta_int("ym_likes_revision"),
                "overrides": db.count_overrides(),
                "overrides_pending": len(db.pending_overrides()),
            },
            "logs": [
                {"at": r["created_at"], "kind": r["kind"], "state": r["state"]}
                for r in db.logs(12)
            ],
        }
    finally:
        db.close()


def format_status(settings: Settings) -> str:
    data = status(settings)
    out = ["— Состояние —"]
    out.append("  База: " + ", ".join(f"{k}={v}" for k, v in data["counts"].items()))
    out.append("  Методы: " + (", ".join(f"{k}={v}" for k, v in
                                        sorted(data["methods"].items(), key=lambda kv: -kv[1])) or "—"))
    rv = data["review"]
    out.append(f"  Ревью: всего {rv.get('total', 0)} "
               f"(не найдено {rv.get('no_result', 0)}, сомнительно {rv.get('review', 0)}, "
               f"fuzzy {rv.get('fuzzy', 0)})")
    out.append("  Стадии: " + ", ".join(f"{k}={'✓' if v else '—'}" for k, v in
                                       data["stages_done"].items()))
    meta = data["meta"]
    out.append(f"  Снимок Spotify: {meta['sp_snapshot_at'] or 'нет'}, "
               f"лайков в Spotify: {meta['sp_liked_total']}, "
               f"override: {meta['overrides']} (применено ждёт {meta['overrides_pending']})")
    if data["logs"]:
        out.append("  Последние события:")
        for row in data["logs"]:
            out.append(f"    {row['at']}  {row['kind']}  {row['state']}")
    return "\n".join(out)
