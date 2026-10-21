"""Снимок того, что уже есть в Spotify.

Нужен по двум причинам:
1. Не плодить дубликаты — в медиатеке уже ~700 треков, часть плейлистов
   могла быть перенесена в прошлых прогонах.
2. Матчингу нужен локальный индекс: если трек уже виден в профиле, искать его
   через /search не приходится, а квота на поиск в dev-mode самая жёсткая.

Снимок идемпотентен: sp_saved/sp_playlist/sp_playlist_item перезаписываются
целиком, sp_track — upsert'ом, поэтому повторный прогон не удваивает строки.
"""
from __future__ import annotations

from typing import Any, Callable

from .config import Config
from .spotify_client import SpotifyAccount, track_to_row
from .storage import Database, now

ProgressFn = Callable[[str], None]


def _silent(_: str) -> None:
    return None


class Snapshot:
    def __init__(self, store: Database, sp: SpotifyAccount, cfg: Config | None = None):
        self.store = store
        self.sp = sp
        self.cfg = cfg or Config()

    # -- медиатека ----------------------------------------------------------
    def saved_tracks(self, progress: ProgressFn = _silent, limit: int | None = None) -> int:
        progress("Читаем «Мне нравится» Spotify…")
        raw = self.sp.saved_tracks(limit=limit, on_progress=progress)
        rows = [(track_to_row(t), t.get("_added_at")) for t in raw]
        rows = [(r, a) for r, a in rows if r]
        self.store.upsert_sp_candidates([r for r, _ in rows])
        self.store.replace_saved([(r["uri"], a) for r, a in rows])
        progress(f"  в кэше лайков: {len(rows)} треков")
        return len(rows)

    # -- плейлисты ----------------------------------------------------------
    def playlists(self, progress: ProgressFn = _silent, max_tracks: int | None = None) -> int:
        progress("Читаем плейлисты Spotify…")
        mine = self.sp.playlists()
        head = [
            {
                "id": pl.get("id"),
                "name": pl.get("name") or "",
                "owner": ((pl.get("owner") or {}).get("id")) or "",
                "total": ((pl.get("tracks") or {}).get("total")) or 0,
                "snapshot_id": pl.get("snapshot_id"),
            }
            for pl in mine
            if pl.get("id")
        ]
        self.store.replace_playlists(head)
        progress(f"  плейлистов: {len(head)}")

        for i, pl in enumerate((p for p in mine if p.get("id")), 1):
            pid = pl.get("id") or ""
            tracks = self.sp.playlist_tracks(pid, limit=max_tracks)
            cands = [c for c in (track_to_row(t) for t in tracks) if c]
            self.store.upsert_sp_candidates(cands)
            self.store.remember_playlist_items(pid, [c["uri"] for c in cands])
            progress(f"  [{i}/{len(head)}] {pl.get('name')}: {len(cands)} треков")
        return len(head)

    # -- общий прогон -------------------------------------------------------
    def run(self, progress: ProgressFn = _silent, with_playlists: bool = True) -> dict[str, Any]:
        me = self.sp.me()
        uid = me.get("id") or ""
        product = me.get("product") or ""
        self.store.set_meta("sp_uid", uid)
        self.store.set_meta("sp_product", product)
        self.store.set_meta("sp_snapshot_at", now())
        progress(f"Профиль Spotify: {uid or '?'}, подписка: {product or 'неизвестно'}")
        if product and product.lower() != "premium":
            progress("  ВНИМАНИЕ: добавление в медиатеку работает только с Premium")

        stats: dict[str, Any] = {"uid": uid, "product": product, "saved": 0, "playlists": 0}
        stats["saved"] = self.saved_tracks(progress)
        if with_playlists:
            stats["playlists"] = self.playlists(progress)
        indexed = self.store.reindex_tracks()
        stats["sp_tracks"] = indexed.get("sp_tracks", 0) if isinstance(indexed, dict) else 0
        self.store.log_event("snapshot", f"saved={stats['saved']} playlists={stats['playlists']}")
        return stats
