"""Отчёты Soundbridge: файл ревью, план импорта, сводка состояния.

Здесь живёт всё «для глаз»: CSV с несопоставленными треками, markdown-план
переноса по томам и короткий отчёт о том, на какой стадии проект.

Файлы кладём в :data:`soundbridge.config.OUT_DIR`, состояние берём из SQLite —
сами отчёты ничего не пишут в Spotify и не портят базу (кроме явного
``apply_decisions``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .config import OUT_DIR, Config, ensure_dirs
from .spotify_client import to_uri
from .storage import Database

REVIEW_CSV = "review.csv"
PLAN_MD = "plan.md"
VERIFY_MD = "verify.md"

#: машиночитаемые колонки — их имена совпадают и в CSV, и в чтении решений
COL_ID = "ym_id"
COL_DECISION = "decision_uri"
COL_NOTE = "заметка"
N_CANDIDATES = 4

#: что человек пишет в ``decision_uri``, если трек переносить не надо
SKIP_TOKENS = {"-", "--", "skip", "нет", "пропуск"}

_REVIEW_HEADER = [
    COL_ID,
    "трек",
    "исполнитель",
    "альбом",
    "длительность",
    "isrc",
    "в_плейлистах",
    "метод",
    "уверенность",
    "почему_сомнительно",
    COL_DECISION,
    COL_NOTE,
    *[f"кандидат_{i + 1}" for i in range(N_CANDIDATES)],
]


def review_path(cfg: Config | None = None) -> Path:
    """Кладёт CSV в ``out/`` и поднимает ошибку, если решение уже лежит в другом месте."""
    ensure_dirs()
    return OUT_DIR / REVIEW_CSV


def plan_path(cfg: Config | None = None) -> Path:
    ensure_dirs()
    return OUT_DIR / PLAN_MD


def verify_path(cfg: Config | None = None) -> Path:
    ensure_dirs()
    return OUT_DIR / VERIFY_MD


# --------------------------------------------------------------------------- #
# мелочи форматирования
# --------------------------------------------------------------------------- #


def _artists_of(raw: Any) -> str:
    """В ``ym_track.artists`` и ``sp_track.artists`` лежит JSON-список."""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(a) for a in raw if a)
    text = str(raw)
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(data, list):
        return ", ".join(str(a) for a in data if a)
    return text


def _mmss(ms: Any) -> str:
    try:
        total = int(ms or 0) // 1000
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    return f"{total // 60}:{total % 60:02d}"


def _candidates_of(row: Any) -> list[dict[str, Any]]:
    raw = row["candidates"] if "candidates" in row.keys() else None
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("candidates") or []
    return [c for c in data if isinstance(c, dict)]


def _candidate_text(cand: dict[str, Any]) -> str:
    uri = cand.get("uri") or ""
    name = cand.get("name") or cand.get("title") or ""
    artists = _artists_of(cand.get("artists"))
    album = cand.get("album") or ""
    score = cand.get("score") or cand.get("confidence")
    bits = [uri]
    if name:
        bits.append(f"{name} — {artists}" if artists else name)
    if album:
        bits.append(album)
    length = _mmss(cand.get("duration_ms"))
    if length:
        bits.append(length)
    if score:
        try:
            bits.append(f"{float(score):.2f}")
        except (TypeError, ValueError):
            pass
    return " | ".join(bits)


def _why(row: Any) -> str:
    """Короткое объяснение, почему трек попал в ревью."""
    method = (row["method"] if "method" in row.keys() else None) or "нет решения"
    note = (row["note"] if "note" in row.keys() else "") or ""
    if method == "no_result":
        return note or "поиск не дал результатов"
    if method == "review":
        return note or "слишком низкая уверенность"
    if method == "fuzzy":
        score = row["confidence"] if "confidence" in row.keys() else 0
        return f"нечёткое совпадение (score {float(score or 0):.2f}) — проверить"
    return note or "не сопоставлен"


# --------------------------------------------------------------------------- #
# CSV ревью
# --------------------------------------------------------------------------- #


def write_review(db: Database, limit: int = 0, path: Path | None = None) -> dict[str, Any]:
    """Выгружает всё спорное в CSV, который человек правит руками.

    В ``decision_uri`` нужно вписать ``spotify:track:...``, ссылку
    ``open.spotify.com`` или голый id; пустая строка = «не переносим».
    """
    rows = db.list_review()
    out = Path(path) if path else review_path()
    total = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]

    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(_REVIEW_HEADER)
        for row in rows:
            cands = _candidates_of(row)
            for i in range(len(cands), N_CANDIDATES):
                cands.append({})
            writer.writerow(
                [
                    row["ym_id"],
                    row["title"] or "",
                    _artists_of(row["artists"]),
                    row["album"] or "",
                    _mmss(row["duration_ms"]),
                    row["isrc"] or "",
                    row["n_refs"] if "n_refs" in row.keys() else "",
                    (row["method"] if "method" in row.keys() else "") or "нет решения",
                    f"{float(row['confidence'] or 0):.2f}" if row["confidence"] is not None else "",
                    _why(row),
                    "",  # сюда человек вписывает решение
                    "",
                    *[_candidate_text(c) for c in cands[:N_CANDIDATES]],
                ]
            )

    counts = db.review_counts()
    db.log_event("review", f"выгружено {len(rows)} из {total} спорных -> {out.name}")
    return {
        "path": str(out),
        "written": len(rows),
        "total_review": total,
        "review": counts.get("review", 0),
        "no_result": counts.get("no_result", 0),
        "uncertain": counts.get("uncertain", 0),
        "untouched": counts.get("untouched", 0),
    }


def read_review(path: Path) -> list[dict[str, str]]:
    """Читает CSV ревью в списке dict-строк (BOM уже снят)."""
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or COL_ID not in reader.fieldnames:
            raise ValueError(f"в {path} нет колонки {COL_ID} — это не файл ревью Soundbridge")
        return [dict(r) for r in reader]


def collect_decisions(rows: Iterable[dict[str, str]]) -> list[tuple[str, str]]:
    """Достаёт из строк CSV пары ``(ym_id, uri)`` для заполненных решений.

    Пустая ячейка — «ничего не менять»; чтобы пометить трек непереносимым,
    впишите в ``decision_uri`` любой из маркеров: ``-``, ``skip``, ``нет``.
    """
    decisions: list[tuple[str, str]] = []
    for row in rows:
        ym_id = (row.get(COL_ID) or "").strip()
        if not ym_id:
            continue
        raw = (row.get(COL_DECISION) or "").strip()
        if not raw or raw.lower() in SKIP_TOKENS:
            continue
        decisions.append((ym_id, to_uri(raw)))
    return decisions


def apply_decisions(
    db: Database,
    path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Забирает ручные решения из CSV в базу (таблица ``override`` + ``match``).

    После этого достаточно перезапустить матчинг/импорт: решения ручной работы
    имеют наивысший приоритет и больше не перезаписываются.
    """
    source = Path(path) if path else review_path()
    if not source.exists():
        raise FileNotFoundError(f"нет файла ревью: {source}. Сначала выполните `review`.")

    rows = read_review(source)
    decisions = collect_decisions(rows)
    skipped = [
        (r[COL_ID].strip(), "")
        for r in rows
        if (r.get(COL_ID) or "").strip() and (r.get(COL_DECISION) or "").strip().lower() in {"-", "--", "skip", "нет", "пропуск"}
    ]
    pairs = decisions + skipped

    if dry_run:
        return {"path": str(source), "dry_run": True, "would_apply": len(decisions), "would_skip": len(skipped)}
    if not pairs:
        return {"path": str(source), "dry_run": False, "applied": 0, "skipped": 0, "missing": 0}

    result = db.apply_overrides(pairs)
    db.log_event(
        "review",
        f"принято ручных решений: {len(result['applied'])}, помечено непереносимыми: {len(result['cleared'])}",
    )
    return {
        "path": str(source),
        "dry_run": False,
        "applied": len(result["applied"]),
        "skipped": len(result["cleared"]),
        "missing": len(result["missing"]),
        "missing_detail": result["missing"][:20],
    }


# --------------------------------------------------------------------------- #
# план импорта
# --------------------------------------------------------------------------- #


def planned_volumes(db: Database, cfg: Config) -> dict[str, int]:
    """Сколько тонов Spotify потребуется на каждый плейлист (лимит 10 000)."""
    per_volume = max(1, min(cfg.import_.max_playlist_tracks, cfg.limits.playlist_max_tracks))
    resolved = db.resolved()
    out: dict[str, int] = {}
    for pl in db.playlists():
        ids = db.playlist_items(pl["kind"])
        have = sum(1 for ym in ids if ym in resolved)
        out[pl["kind"]] = max(1, -(-have // per_volume)) if have else 0
    return out


def write_plan(db: Database, cfg: Config, path: Path | None = None) -> dict[str, Any]:
    """Markdown-план: что именно и в каком виде поедет в Spotify."""
    ensure_dirs()
    out = Path(path) if path else plan_path()
    counts = db.counts()
    resolved = db.resolved()
    per_volume = max(1, min(cfg.import_.max_playlist_tracks, cfg.limits.playlist_max_tracks))
    saved_now = counts.get("spotify_saved", 0)

    lines: list[str] = ["# План переноса", ""]
    lines += [
        f"- треков в Яндексе: **{counts['ym_tracks']}** (с ISRC {counts['ym_tracks_with_isrc']})",
        f"- решений матчинга: **{counts['matched']}** из них автоматически "
        f"(isrc {counts['match_isrc']}, точных {counts['match_exact']}, нечётких {counts['match_fuzzy']})",
        f"- требует ревью: **{counts['match_review']}**, без результата: **{counts['match_none']}**",
        f"- уже в медиатеке Spotify: **{saved_now}** (свободно мест: "
        f"{max(0, cfg.limits.library_max_tracks - saved_now)})",
        f"- лимит треков в одном плейлисте Spotify: **{per_volume}**",
        "",
        "## Плейлисты",
        "",
        "| Плейлист | Треков | Переносим | Тонов | Пропусков |",
        "| --- | --- | --- | --- | --- |",
    ]

    volumes_total = 0
    for pl in db.playlists():
        ids = db.playlist_items(pl["kind"])
        have = sum(1 for ym in ids if ym in resolved)
        vols = max(1, -(-have // per_volume)) if have else 0
        volumes_total += vols
        title = pl["title"] or pl["kind"]
        if pl["is_likes"]:
            title = f"{title} → «{cfg.import_.likes_playlist_title}»"
        lines.append(f"| {title} | {len(ids)} | {have} | {vols} | {len(ids) - have} |")

    lines += [
        "",
        f"Итого тонов к созданию: **{volumes_total}**",
        "",
        "## Режим",
        "",
        f"- dry_run: `{cfg.import_.dry_run}` (сменить: `--apply` или `import_.dry_run = false` в config.toml)",
        f"- создавать плейлисты: `{cfg.import_.create_playlists}`",
        f"- переносить лайки в медиатеку: `{cfg.import_.import_likes}`",
    ]
    if cfg.import_.skip_playlists:
        lines.append(f"- исключаемые плейлисты: {', '.join(cfg.import_.skip_playlists)}")
    if cfg.import_.only_playlists:
        lines.append(f"- только эти плейлисты: {', '.join(cfg.import_.only_playlists)}")
    if cfg.import_.prefix:
        lines.append(f"- префикс имени: `{cfg.import_.prefix}`")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.log_event("plan", f"tonov={volumes_total}")
    return {"path": str(out), "volumes": volumes_total, "per_volume": per_volume}


# --------------------------------------------------------------------------- #
# статус
# --------------------------------------------------------------------------- #


def status(db: Database, cfg: Config | None = None) -> dict[str, Any]:
    """Одна структура со всем текущим состоянием конвейера."""
    counts = db.counts()
    review = db.review_counts()
    methods = {row["method"]: row["n"] for row in db.method_breakdown()}
    created = [dict(row) for row in db.created()]
    events = [dict(row) for row in db.logs(12)]
    stages: dict[str, str] = {}
    stages["auth"] = "ok" if db.get_meta("sp_uid") else "нет Spotify-профиля"
    stages["export"] = "ok" if counts["ym_tracks"] else "пусто"
    stages["snapshot"] = "ok" if counts["spotify_saved"] else "нет снимка Spotify"
    if counts["matched"]:
        stages["match"] = "ok"
    elif counts["ym_tracks"]:
        stages["match"] = "не запущен"
    else:
        stages["match"] = "нужен экспорт"
    if counts["created_playlists"]:
        stages["import"] = "ok"
    elif counts["matched"]:
        stages["import"] = "не запущен"
    else:
        stages["import"] = "нужен match"
    return {
        "counts": counts,
        "review": review,
        "methods": methods,
        "created": created,
        "events": events,
        "stages": stages,
        "config": {
            "dry_run": cfg.import_.dry_run if cfg else None,
            "playlist_max_tracks": cfg.limits.playlist_max_tracks if cfg else None,
            "library_max_tracks": cfg.limits.library_max_tracks if cfg else None,
        },
    }


def format_status(data: dict[str, Any]) -> str:
    """Текстовая сводка для консоли."""
    counts = data["counts"]
    review = data["review"]
    lines = [
        "Состояние базы",
        "---------------",
        f"Яндекс:     треков {counts['ym_tracks']} (ISRC {counts['ym_tracks_with_isrc']}), "
        f"плейлистов {counts['ym_playlists']}"
        + (f", пропущено автосгенерированных {counts['ym_playlists_skipped']}" if counts["ym_playlists_skipped"] else ""),
        f"Матчинг:    решено {counts['matched']} · isrc {counts['match_isrc']} · "
        f"точных {counts['match_exact']} · нечётких {counts['match_fuzzy']} · "
        f"ревью {counts['match_review']} · нет результата {counts['match_none']}",
        f"Ручныеправки: {counts['overrides']} · ревью к работе: {review.get('review', 0) + review.get('no_result', 0)}",
        f"Spotify:    в медиатеке {counts['spotify_saved']}, плейлистов {counts['spotify_playlists']}, "
        f"создано нами {counts['created_playlists']}",
        "",
        "Стадии:",
    ]
    for name, state in data["stages"].items():
        mark = "✓" if state == "ok" else "·"
        lines.append(f"  {mark} {name}: {state}")
    if data["created"]:
        lines.append("")
        lines.append("Созданные плейлисты:")
        for row in data["created"]:
            lines.append(
                f"  - {row['name']} [{row['state']}] {row['added']}/{row['planned']} · {row['sp_uri'] or row['sp_id']}"
            )
    if data["events"]:
        lines.append("")
        lines.append("Последние события:")
        for ev in data["events"][:8]:
            lines.append(f"  {ev['ts']} {ev['kind']}: {ev['detail']}")
    return "\n".join(lines)
