"""Создание плейлистов и лайков в Spotify.

Главное свойство: этап можно прерывать в любой момент и продолжать. Перед
записью в плейлист читаем его текущее содержимое и добавляем только недостающее,
поэтому повторный запуск не создаёт дубликатов.
"""
from __future__ import annotations

import math
import sqlite3
import time
from typing import Any, Iterable

from .config import Settings
from .normalize import now
from .spotify_client import SpotifyAccount, SpotifyError, uri_id
from .storage import Database, checkpoint, mark_done, resume

JOB = "import"
LIKES_TITLE = "Мне нравится (Яндекс)"


# --------------------------------------------------------------------------
# план
# --------------------------------------------------------------------------
def wanted_by_kind(db: Database, settings: Settings) -> dict[str, dict[str, Any]]:
    """Что и в каком порядке заливаем: {kind: {"title":..., "uris":[...]}}.

    Лайки идут отдельным плейлистом: их нужно вычесть из уже сохранённых в
    Spotify, иначе `save_tracks` просто вернёт пустой ответ на дубликаты.
    """
    resolved = db.resolved()
    result: dict[str, dict[str, Any]] = {}
    for pl in db.playlists():
        kind = pl["kind"]
        if kind == "likes":
            ids = []
            for uri in db.playlist_items(kind):
                row = db.track_of_uri(uri)
                if row:
                    ids.append(row["ym_id"])
        else:
            ids = [r["ym_id"] for r in db.conn.execute(
                "SELECT ym_id FROM playlist_track WHERE playlist_id=? ORDER BY ord",
                (pl["id"],))]
        seen: set[str] = set()
        uris: list[str] = []
        missing = 0
        for ym_id in ids:
            hit = resolved.get(ym_id)
            if not hit:
                missing += 1
                continue
            if hit[0] in seen:
                continue
            seen.add(hit[0])
            uris.append(hit[0])
        result[kind] = {
            "title": LIKES_TITLE if kind == "likes" else (pl["title"] or kind),
            "uris": uris,
            "unmatched": missing,
            "total_source": len(ids),
        }
    saved = db.saved_uris()
    if "likes" in result:
        before = len(result["likes"]["uris"])
        result["likes"]["uris"] = [u for u in result["likes"]["uris"] if u not in saved]
        result["likes"]["already_in_spotify"] = before - len(result["likes"]["uris"])
    return result


def _split(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        size = 10000
    return [items[i:i + size] for i in range(0, len(items), size)] or []


def plan(settings: Settings) -> list[dict[str, Any]]:
    """Список плейлистов, которые будут созданы (с разбивкой на тома)."""
    db = Database(settings.db_path)
    try:
        rows = []
        for kind, data in wanted_by_kind(db, settings).items():
            chunks = _split(data["uris"], settings.cfg.playlist_max)
            for index, chunk in enumerate(chunks, start=1):
                rows.append({
                    "kind": kind,
                    "title": data["title"],
                    "volume": index,
                    "volumes": len(chunks),
                    "uris": chunk,
                    "unmatched": data["unmatched"] if index == 1 else 0,
                })
        return rows
    finally:
        db.close()


def planned_volumes(db: Database, settings: Settings) -> dict[str, int]:
    volumes: dict[str, int] = {}
    for kind, data in wanted_by_kind(db, settings).items():
        n = len(data["uris"])
        volumes[kind] = max(1, math.ceil(n / max(1, settings.cfg.playlist_max))) if n else 0
    return volumes


def describe(settings: Settings) -> str:
    rows = plan(settings)
    lines = ["— Что будет создано в Spotify —"]
    if not rows:
        lines.append("  пусто: сначала export и match")
        return "\n".join(lines)
    for row in rows:
        suffix = f" (часть {row['volume']}/{row['volumes']})" if row["volumes"] > 1 else ""
        lines.append(f"  {row['title']}{suffix}: {len(row['uris'])} треков")
    for kind, data in wanted_by_kind(Database(settings.db_path), settings).items():
        if data["unmatched"]:
            lines.append(f"  {data['title']}: {data['unmatched']} позиций не перенесётся "
                         f"(нет подтверждения соответствия)")
        if data.get("already_in_spotify"):
            lines.append(f"  {data['title']}: {data['already_in_spotify']} уже есть в твоих лайках")
    total = sum(len(r["uris"]) for r in rows)
    lines.append(f"  Итого: {total} треков в {len(rows)} плейлистах")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# выполнение
# --------------------------------------------------------------------------
def _upsert_created(db: Database, **fields: Any) -> None:
    row = {
        "ym_kind": fields["kind"],
        "sp_id": fields.get("sp_id", ""),
        "sp_uri": fields.get("sp_uri", ""),
        "name": fields.get("name", ""),
        "volume": fields.get("volume", 1),
        "planned": fields.get("planned", 0),
        "added": fields.get("added", 0),
        "state": fields.get("state", "created"),
    }
    db.conn.execute(
        """
        INSERT INTO created_playlist(ym_kind, sp_id, sp_uri, name, volume, planned, added,
                                     state, updated_at)
        VALUES(:ym_kind, :sp_id, :sp_uri, :name, :volume, :planned, :added, :state, :updated_at)
        ON CONFLICT(ym_kind, volume) DO UPDATE SET
            sp_id=excluded.sp_id, sp_uri=excluded.sp_uri, name=excluded.name,
            planned=excluded.planned, added=excluded.added, state=excluded.state,
            updated_at=excluded.updated_at
        """,
        {**row, "updated_at": now()},
    )
    db.commit()


def _existing(db: Database, kind: str, volume: int) -> sqlite3.Row | None:
    return db.conn.execute(
        "SELECT * FROM created_playlist WHERE ym_kind=? AND volume=?", (kind, volume)
    ).fetchone()


def _fill(account: SpotifyAccount, db: Database, uri: str, want: Iterable[str]) -> dict[str, int]:
    """Дописывает в плейлист недостающие треки (идемпотентно)."""
    want_list = list(want)
    try:
        current = list(account.playlist_items(uri))
    except SpotifyError as exc:
        print(f"    ! не удалось прочитать плейлист ({exc}), считаем его пустым")
        current = []
    have = set(current)
    missing = [u for u in want_list if u not in have]
    added = 0
    failed = 0
    for batch_index, batch in enumerate(_batches(missing), start=1):
        try:
            account.api.add_tracks_in_batches(uri, [batch])
        except SpotifyError as exc:
            failed += len(batch)
            print(f"    ! пакет {batch_index}: {exc}")
            continue
        db.remember_playlist_items(uri_id(uri), batch)
        added += len(batch)
        print(f"    + {added}/{len(missing)}")
    return {"added": added, "failed": failed, "already": len(want_list) - len(missing)}


def _batches(items: list[str], size: int = 50):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def run(settings: Settings, do_apply: bool = False, only: str | None = None) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        if not db.get_meta("sp_snapshot_at"):
            raise SystemExit(
                "Нет снимка Spotify. Он нужен, чтобы не плодить дубликаты в твоих ~700 лайках.\n"
                "  python -m soundbridge snapshot"
            )
        rows = plan(settings)
        if only:
            rows = [r for r in rows if r["kind"] == only]
            if not rows:
                raise SystemExit(f"Источник «{only}» не найден. Есть: "
                                 + ", ".join(sorted({r['kind'] for r in plan(settings)})))
    finally:
        db.close()

    total = sum(len(r["uris"]) for r in rows)
    print(f"План: {len(rows)} плейлистов, {total} треков")
    for row in rows:
        suffix = f" (часть {row['volume']}/{row['volumes']})" if row["volumes"] > 1 else ""
        print(f"  · {row['title']}{suffix}: {len(row['uris'])}")
    if not do_apply:
        print("\nПрогон без записи. Добавь --apply, чтобы создать в Spotify.")
        return {"playlists": len(rows), "tracks": total, "dry_run": True}

    account = SpotifyAccount(
        settings.credentials_path,
        client_id=settings.spotify_client_id,
        scopes=list(settings.scopes),
        cache_dir=settings.root / "data",
        open_browser=not settings.no_browser,
    )
    db = Database(settings.db_path)
    stats = {"created": 0, "reused": 0, "added": 0, "failed": 0, "already": 0,
             "playlists": 0, "errors": []}
    try:
        me = account.me()
        print(f"Аккаунт Spotify: {me.get('display_name') or me.get('id')} "
              f"(premium={me.get('product')})")
        if me.get("product") != "premium":
            print("! Spotify без Premium: добавление треков в медиатеку недоступно, "
                  "плейлисты создадутся")
        start_index = resume(db, JOB, {"index": 0})["index"]
        for position, row in enumerate(rows):
            if position < start_index:
                continue
            name = row["title"]
            if row["volumes"] > 1:
                name = f"{row['title']} ({row['volume']}/{row['volumes']})"
            print(f"[{position + 1}/{len(rows)}] {name} · {len(row['uris'])} треков")

            existing = _existing(db, row["kind"], row["volume"])
            uri = existing["sp_uri"] if existing and existing["sp_uri"] else ""
            if uri:
                stats["reused"] += 1
                print(f"    уже существует: {uri}")
            else:
                try:
                    created = account.create_playlist(name, description="Перенесено из Яндекс Музыки",
                                                      public=False)
                except SpotifyError as exc:
                    stats["errors"].append(f"{name}: {exc}")
                    print(f"    ! не создан: {exc}")
                    checkpoint(db, JOB, {"index": position, "total": len(rows)})
                    continue
                uri = created["uri"]
                stats["created"] += 1
                _upsert_created(db, kind=row["kind"], sp_id=uri_id(uri), sp_uri=uri,
                                name=name, volume=row["volume"], planned=len(row["uris"]),
                                added=0, state="created")
                print(f"    создан: {uri}")

            result = _fill(account, db, uri, row["uris"])
            stats["added"] += result["added"]
            stats["failed"] += result["failed"]
            stats["already"] += result["already"]
            state = "imported" if result["failed"] == 0 else "partial"
            _upsert_created(db, kind=row["kind"], sp_id=uri_id(uri), sp_uri=uri,
                            name=name, volume=row["volume"], planned=len(row["uris"]),
                            added=result["added"] + result["already"], state=state)
            checkpoint(db, JOB, {"index": position + 1, "total": len(rows)})
            time.sleep(0.2)

        if only is None:
            mark_done(db, JOB, {"tracks": stats["added"], "playlists": len(rows)})
        else:
            db.log_event("import_partial_run", {"only": only, "stats": stats})
        db.commit()
        print("\n— Итог —")
        print(f"  создано плейлистов: {stats['created']}, использовано готовых: {stats['reused']}")
        print(f"  добавлено треков: {stats['added']}, уже было: {stats['already']}, "
              f"ошибок: {stats['failed']}")
        if stats["errors"]:
            print("  ошибки: " + "; ".join(stats["errors"][:5]))
        print("  проверь: python -m soundbridge verify")
        return stats
    finally:
        db.close()


# --------------------------------------------------------------------------
# сверка
# --------------------------------------------------------------------------
def verify(settings: Settings) -> dict[str, Any]:
    """Сравнивает то, что задумано, с тем, что реально лежит в Spotify."""
    account = SpotifyAccount(
        settings.credentials_path,
        client_id=settings.spotify_client_id,
        scopes=list(settings.scopes),
        cache_dir=settings.root / "data",
        open_browser=not settings.no_browser,
    )
    db = Database(settings.db_path)
    report: dict[str, Any] = {"playlists": [], "missing_total": 0}
    try:
        rows = db.created()
        if not rows:
            print("Нет созданных плейлистов. Выполни: python -m soundbridge import --apply")
            return report
        print("— Сверка с Spotify —")
        for row in rows:
            try:
                actual = len(list(account.playlist_items(row["sp_uri"])))
            except SpotifyError as exc:
                print(f"  {row['name']}: ошибка {exc}")
                report["playlists"].append({"name": row["name"], "error": str(exc)})
                continue
            planned = int(row["planned"] or 0)
            diff = planned - actual
            report["missing_total"] += max(0, diff)
            report["playlists"].append({"name": row["name"], "planned": planned,
                                        "actual": actual})
            flag = "OK" if diff <= 0 else f"не хватает {diff}"
            print(f"  {row['name']}: задумано {planned}, в Spotify {actual} · {flag}")

        liked = account.saved_count()
        report["saved"] = liked
        print(f"  Твоя медиатека Spotify: {liked} треков")
        db.log_event("verify", {"playlists": report["playlists"], "saved": liked})
        db.commit()
        if report["missing_total"]:
            print("\nЧто-то потерялось — просто повтори: python -m soundbridge import --apply")
        else:
            print("\nВсё на месте.")
        return report
    finally:
        db.close()
