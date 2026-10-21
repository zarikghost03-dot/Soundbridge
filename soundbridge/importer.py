"""Стадия импорта: создаём плейлисты в Spotify и кладём в них треки.

Железные правила:

* **идемпотентность** — перед записью сверяемся с ``created_playlist`` и с самим
  Spotify, повторный запуск не создаёт дублей и не добавляет тот же трек дважды;
* **лимиты** — в плейлисте максимум 10 000 треков, поэтому большие списки
  режутся на томы ``kind#v2``; медиатека тоже ограничена, поэтому лайки
  обрезаются по свободным местам;
* **dry-run по умолчанию** — пока ``cfg.import_.dry_run == True`` (или не дано
  ``--apply``), ни одного запроса на запись не делается.

Функции принимают уже открытые ``Database``/``SpotifyAccount``/``Config`` —
соединения живут в :mod:`soundbridge.cli`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config, OUT_DIR, ensure_dirs
from .spotify_client import ProgressFn, SpotifyAccount
from .storage import Database

IMPORT_JOB = "import"
VERIFY_MD = "verify.md"


def _silent(_: str) -> None:
    return None


def verify_path() -> Path:
    ensure_dirs()
    return OUT_DIR / VERIFY_MD


# --------------------------------------------------------------------------- #
# план
# --------------------------------------------------------------------------- #


def _wanted(cfg: Config, kind: str, title: str, only: Sequence[str] | None) -> bool:
    """Прошли ли плейлист фильтры ``only_playlists``/``skip_playlists``/``--only``."""
    needles = {s.lower() for s in (cfg.import_.only_playlists or []) if s}
    if only:
        needles |= {s.lower() for s in only if s}
    if needles and not any(n in kind.lower() or n in title.lower() for n in needles):
        return False
    blocked = {s.lower() for s in (cfg.import_.skip_playlists or []) if s}
    if blocked and any(b in kind.lower() or b in title.lower() for b in blocked):
        return False
    return True


def _ordered_uris(ids: Iterable[str], resolved: dict[str, tuple[str, str, float]]) -> list[str]:
    """ym_id → spotify-URI в исходном порядке плейлиста, без повторов."""
    out: list[str] = []
    seen: set[str] = set()
    for ym_id in ids:
        hit = resolved.get(ym_id)
        if not hit or not hit[0]:
            continue
        if hit[0] in seen:
            continue
        seen.add(hit[0])
        out.append(hit[0])
    return out


def build_plan(db: Database, cfg: Config, only: Sequence[str] | None = None) -> dict[str, Any]:
    """Что именно поедем в Spotify: томы плейлистов + списки лайков.

    Возвращает ``{"targets": [...], "likes": [...], "notes": [...]}``;
    каждый target — ``key/name/volume/volumes/uris/missing``.
    """
    resolved = db.resolved()
    per_volume = max(1, min(cfg.import_.max_playlist_tracks, cfg.limits.playlist_max_tracks))
    targets: list[dict[str, Any]] = []
    likes_uris: list[str] = []
    notes: list[str] = []

    for pl in db.playlists():
        kind = pl["kind"]
        title = pl["title"] or kind
        ids = db.playlist_items(kind)
        uris = _ordered_uris(ids, resolved)
        decided = sum(1 for ym in ids if resolved.get(ym, ("",))[0])
        missing = max(0, len(ids) - decided)
        if pl["is_likes"] or kind == "likes":
            likes_uris = uris
            if not cfg.import_.import_likes:
                notes.append("перенос лайков в медиатеку выключён (import_.import_likes = false)")
            if not cfg.import_.likes_playlist:
                notes.append("плейлист из лайков не создаём (import_.likes_playlist = false)")
                continue
            if not _wanted(cfg, "likes", cfg.import_.likes_playlist_title, only):
                notes.append("плейлист лайков отфильтрован")
                continue
            name = f"{cfg.import_.prefix}{cfg.import_.likes_playlist_title}"
            volumes = [uris[i : i + per_volume] for i in range(0, len(uris), per_volume)] or [[]]
            for i, chunk in enumerate(volumes, start=1):
                targets.append(
                    {
                        "key": "likes" if len(volumes) == 1 else f"likes#v{i}",
                        "kind": kind,
                        "volume": i,
                        "volumes": len(volumes),
                        "name": name if len(volumes) == 1 else f"{name} (часть {i})",
                        "description": f"Перенос из Яндекс Музыки · {len(chunk)} треков",
                        "uris": chunk,
                        "missing": missing,
                        "is_likes": True,
                    }
                )
            continue

        if not _wanted(cfg, kind, title, only):
            notes.append(f"плейлист «{title}» отфильтрован")
            continue
        if not uris:
            notes.append(f"плейлист «{title}»: ни одного решённого трека ({len(ids)} шт.) — пропускаем")
            continue

        name = f"{cfg.import_.prefix}{title}"
        volumes = [uris[i : i + per_volume] for i in range(0, len(uris), per_volume)]
        for i, chunk in enumerate(volumes, start=1):
            targets.append(
                {
                    "key": kind if len(volumes) == 1 else f"{kind}#v{i}",
                    "kind": kind,
                    "volume": i,
                    "volumes": len(volumes),
                    "name": name if len(volumes) == 1 else f"{name} (часть {i})",
                    "description": pl["description"] or f"Перенос из Яндекс Музыки · {title}",
                    "uris": chunk,
                    "missing": missing if i == 1 else 0,
                    "is_likes": False,
                }
            )

    if not db.playlists():
        notes.append("в базе нет ни одного плейлиста Яндекса — выполните `export`")

    return {"targets": targets, "likes": likes_uris, "notes": notes, "per_volume": per_volume}


# --------------------------------------------------------------------------- #
# запись
# --------------------------------------------------------------------------- #


def _playlist_uri_set(sp: SpotifyAccount, playlist_id: str, progress: ProgressFn) -> set[str]:
    try:
        return {t.get("uri") for t in sp.playlist_tracks(playlist_id) if t.get("uri")}
    except Exception as exc:  # сеть/лимит — лучше перестраховаться и не дублировать
        progress(f"    не удалось прочитать плейлист {playlist_id}: {exc}")
        raise


def run_import(
    db: Database,
    sp: SpotifyAccount,
    cfg: Config,
    progress: ProgressFn = _silent,
    force: bool = False,
    only: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Главная стадия записи. Подробности — в docstring модуля."""
    dry = bool(cfg.import_.dry_run)
    plan = build_plan(db, cfg, only=only)
    targets: list[dict[str, Any]] = plan["targets"]
    likes_uris: list[str] = plan["likes"]

    summary: dict[str, Any] = {
        "dry_run": dry,
        "targets": len(targets),
        "playlists_created": 0,
        "playlists_reused": 0,
        "tracks_added": 0,
        "likes_saved": 0,
        "likes_dropped": 0,
        "notes": list(plan["notes"]),
        "volumes": {},
    }

    if dry:
        progress("Режим dry_run: ничего не создаём и не добавляем (ключ --apply).")
        for t in targets:
            progress(f"  {t['name']}: {len(t['uris'])} треков"
                     + (f", {t['volumes']} томов" if t["volumes"] > 1 else ""))
        free = max(0, cfg.limits.library_max_tracks - db.counts().get("spotify_saved", 0))
        to_save = [u for u in likes_uris if u not in db.saved_uris()]
        summary["likes_saved"] = min(len(to_save), free) if cfg.import_.import_likes else 0
        summary["likes_dropped"] = max(0, len(to_save) - free)
        summary["tracks_added"] = sum(len(t["uris"]) for t in targets)
        progress(f"  лайков к добавлению в медиатеку: {summary['likes_saved']}"
                 + (f" (ещё {summary['likes_dropped']} не влезут)" if summary["likes_dropped"] else ""))
        db.log_event("import", f"dry_run targets={len(targets)} tracks={summary['tracks_added']}")
        return summary

    if not cfg.import_.create_playlists:
        targets = [t for t in targets if t["is_likes"]]
        summary["notes"].append("создание плейлистов выключено (import_.create_playlists = false)")

    known = {row["ym_kind"]: row for row in db.created()}
    done_keys: list[str] = list(db.resume(IMPORT_JOB, []) or [])

    # ------- лайки в медиатеку ------------------------------------------- #
    if cfg.import_.import_likes and likes_uris:
        already = db.saved_uris()
        pending = [u for u in likes_uris if u not in already]
        free = max(0, cfg.limits.library_max_tracks - len(already))
        if len(pending) > free:
            summary["notes"].append(
                f"медиатека заполнена: добавляем {free} из {len(pending)} лайков, "
                f"{len(pending) - free} остаются только в плейлисте"
            )
            progress(f"  медиатека: свободно {free} мест, лайков ждёт {len(pending)} — обрезаем")
            pending = pending[:free]
        if pending:
            progress(f"Сохраняем {len(pending)} лайков в медиатеку…")
            saved = sp.save_tracks_in_batches(pending, on_progress=progress)
            summary["likes_saved"] = saved
            db.remember_saved(pending)
        else:
            progress("  лайки уже в медиатеке, пропускаем")

    # ------- плейлисты ---------------------------------------------------- #
    for t in targets:
        if not t["uris"]:
            summary["notes"].append(f"{t['name']}: пустой список решений — том не создаём")
            continue
        if not force and t["key"] in done_keys:
            progress(f"  {t['name']}: уже сделан (чекпоинт)")
            continue

        row = known.get(t["key"])
        playlist_id = "" if force else (row["sp_id"] if row else "")
        if playlist_id:
            progress(f"{t['name']}: дописываем существующий {playlist_id}")
            summary["playlists_reused"] += 1
        else:
            created = sp.create_playlist(t["name"], t["description"], public=False)
            playlist_id = created.get("id") or ""
            if not playlist_id:
                raise RuntimeError(f"Spotify не вернул id плейлиста для «{t['name']}»")
            summary["playlists_created"] += 1
            progress(f"{t['name']}: создан {created.get('uri') or playlist_id}")

        have: set[str] = set()
        if row and row["sp_id"] == playlist_id and not force:
            have = _playlist_uri_set(sp, playlist_id, progress)
        to_add = [u for u in t["uris"] if u not in have]

        added = 0
        if to_add:
            progress(f"  добавляем {len(to_add)} треков (в плейлисте уже {len(have)})…")
            added = sp.add_tracks_in_batches(playlist_id, to_add, on_progress=progress)
        total_in_playlist = len(have | set(to_add))

        db.remember_created(
            {
                "ym_kind": t["key"],
                "sp_id": playlist_id,
                "sp_uri": f"spotify:playlist:{playlist_id}",
                "name": t["name"],
                "volume": t["volume"],
                "planned": len(t["uris"]),
                "added": total_in_playlist,
                "state": "imported",
            }
        )
        db.remember_playlist_items(playlist_id, t["uris"])
        done_keys.append(t["key"])
        db.checkpoint(IMPORT_JOB, done_keys)
        summary["tracks_added"] += added
        summary["volumes"][t["key"]] = {"playlist": playlist_id, "planned": len(t["uris"]), "in_list": total_in_playlist}
        known[t["key"]] = None  # не пересоздаём при повторном проходе внутри запуска

    # ручные решения, из которых собран план, больше не «pending»
    pending_ids = [ym for ym, _uri in db.pending_overrides()]
    if pending_ids:
        db.set_override_applied(pending_ids)
        summary["overrides_applied"] = len(pending_ids)

    db.log_event(
        "import",
        f"создано {summary['playlists_created']}, дописано {summary['playlists_reused']},"
        f" треков {summary['tracks_added']}, лайков {summary['likes_saved']}",
    )
    if summary["notes"]:
        for note in summary["notes"]:
            progress(f"  · {note}")
    return summary


# --------------------------------------------------------------------------- #
# проверка
# --------------------------------------------------------------------------- #


def verify(db: Database, sp: SpotifyAccount, progress: ProgressFn = _silent) -> dict[str, Any]:
    """Сверяем то, что записали, с тем, что реально лежит в Spotify."""
    rows = db.created()
    report: list[dict[str, Any]] = []
    mismatches = 0
    for row in rows:
        pid = row["sp_id"]
        try:
            meta = sp.get(f"/playlists/{pid}", {"additional_types": "track"})
            actual = int((meta.get("tracks") or {}).get("total") or 0)
        except Exception as exc:
            actual = -1
            progress(f"  {row['name']}: не удалось прочитать ({exc})")
        ok = actual == row["added"]
        mismatches += 0 if ok else 1
        report.append(
            {
                "key": row["ym_kind"],
                "name": row["name"],
                "sp_id": pid,
                "sp_uri": row["sp_uri"],
                "planned": row["planned"],
                "recorded": row["added"],
                "actual": actual,
                "ok": ok,
            }
        )
        progress(f"  {'✓' if ok else '⚠'} {row['name']}: записано {row['added']}, в Spotify {actual}")

    saved_total: Any = None
    try:
        saved_total = int((sp.get("/me/tracks", {"limit": 1}) or {}).get("total") or 0)
    except Exception as exc:
        progress(f"  медиатеку прочитать не удалось: {exc}")

    counts = db.counts()
    expected_saved = counts.get("spotify_saved", 0)
    result = {
        "playlists": len(report),
        "mismatches": mismatches,
        "rows": report,
        "saved_total": saved_total,
        "saved_before": expected_saved,
    }
    lines = ["# Проверка переноса", ""]
    lines.append(f"- создано тонов: **{len(report)}**, расхождений: **{mismatches}**")
    if saved_total is not None:
        lines.append(f"- треков в медиатеке Spotify: **{saved_total}** (было до импорта {expected_saved})")
    lines += ["", "| Тон | Planned | Записано | В Spotify | Статус | Ссылка |", "| --- | --- | --- | --- | --- | --- |"]
    for row in report:
        lines.append(
            f"| {row['name']} | {row['planned']} | {row['recorded']} | {row['actual']} |"
            f" {'ok' if row['ok'] else 'проверить'} | {row['sp_uri']} |"
        )
    verify_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.log_event("verify", f"playlists={len(report)} mismatches={mismatches} saved={saved_total}")
    return result
