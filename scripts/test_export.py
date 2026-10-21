"""Прогон export_library на подставных объектах Яндекса (без сети).

Экспорт — самая дорогая по запросам стадия, поэтому её логику (лайки с
дочитыванием деталей, плейлисты со вложенными треками, пропуск служебных)
проверяем офлайн на фиктивной базе.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from soundbridge.config import load_config  # noqa: E402
from soundbridge.storage import Database  # noqa: E402
from soundbridge.yandex_client import export_library  # noqa: E402


def track(track_id: int, album_id: int, title: str, artist: str, *, isrc=None, ms=200_000):
    return SimpleNamespace(
        id=f"{track_id}:{album_id}",
        real_id=track_id,
        title=title,
        version=None,
        duration_ms=ms,
        explicit=False,
        type="file",
        state=None,
        available=True,
        track_source="official",
        isrc=isrc,
        ugc_artist_name=None,
        artists=[SimpleNamespace(name=artist)],
        albums=[SimpleNamespace(id=album_id, title=f"Album {album_id}")],
    )


SHORTS = {
    "500:50": track(500, 50, "Группа крови", "Кино", isrc="RUR920900906"),
    "501:50": track(501, 50, "Звезда по имени Солнце", "Кино"),
    "600:60": track(600, 60, "Walk on the Wild Side", "Lou Reed", isrc="USRC17605535"),
    "700:70": track(700, 70, "Shoes", "Обувки", ms=180_000),
}


class FakeAccount:
    """Дубликат YandexAccount, только без HTTP."""

    uid = 123456789
    login = "smoke.test"

    def __init__(self, likes_revision: int) -> None:
        self.likes_revision = likes_revision
        self.calls: list[str] = []

    def likes(self):
        shorts = [SimpleNamespace(id=i, album_id=int(i.split(":")[1]), track=None)
                  for i in ("500:50", "501:50", "600:60")]
        return shorts, self.likes_revision

    def playlists(self):
        return [
            SimpleNamespace(kind="likes", title="Мне нравится", generated_playlist_type=None, type=None),
            SimpleNamespace(kind="aa11bb", title="Рокапок", generated_playlist_type=None, type="playlist"),
            SimpleNamespace(kind="cc22dd", title="Моя волна", generated_playlist_type="playlist", type="wave"),
            SimpleNamespace(kind="ee33ff", title="Премьеры", generated_playlist_type="premiere", type="premiere"),
        ]

    def playlist(self, kind: str):
        self.calls.append(kind)
        if kind == "aa11bb":
            inner = [track(700, 70, "Shoes", "Обувки"), track(500, 50, "Группа крови", "Кино",
                                                              isrc="RUR920900906")]
            return SimpleNamespace(
                kind=kind,
                uid=self.uid,
                title="Рокапок",
                description="проверка",
                visibility="public",
                created="2024-01-02T03:04:05+00:00",
                modified="2024-05-06T07:08:09+00:00",
                track_count=2,
                type="playlist",
                generated_playlist_type=None,
                tracks=[SimpleNamespace(id=f"{t.id}", album_id=t.albums[0].id, track=t) for t in inner],
            )
        raise AssertionError(f"пропущенный плейлист запрошен: {kind}")

    def tracks_by_ids(self, ids):
        self.calls.append("details:" + ",".join(ids))
        return [SHORTS[i] for i in ids if i in SHORTS]


def main() -> int:
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "smoke.db")
        account = FakeAccount(likes_revision=42)
        log: list[str] = []
        stats = export_library(account, db, on_progress=log.append)

        print("playlists:", stats.playlists, "skipped:", stats.skipped,
              "likes:", stats.likes, "tracks:", stats.tracks)
        assert stats.playlists == 1, stats
        assert stats.skipped == 2, stats
        assert stats.likes == 3, stats
        assert stats.tracks == 4, stats

        kinds = {row["kind"]: row for row in db.playlists(include_skipped=True)}
        assert set(kinds) == {"likes", "aa11bb"}, list(kinds)
        assert kinds["likes"]["is_likes"] == 1
        assert kinds["aa11bb"]["n_items"] == 2
        assert db.playlist_items("likes") == ["500:50", "501:50", "600:60"], db.playlist_items("likes")
        assert db.playlist_items("aa11bb") == ["700:70", "500:50"], db.playlist_items("aa11bb")

        by_id = {row["ym_id"]: row for row in db.all_tracks()}
        assert by_id["500:50"]["isrc"] == "RUR920900906"
        assert by_id["500:50"]["n_refs"] == 2, by_id["500:50"]["n_refs"]
        assert by_id["600:60"]["n_refs"] == 1
        assert by_id["500:50"]["pair_key"], "ключи не пересчитаны"
        assert db.get_meta_int("ym_likes_revision") == 42

        # второй прогон: ревизия та же → детали лайков не дочитываем
        account.calls.clear()
        again = export_library(account, db, on_progress=log.append)
        assert again.likes == 3 and again.tracks == 4, again
        assert not any(call.startswith("details:") for call in account.calls), account.calls
        assert db.playlist_item_count("likes") == 3
        assert {row["ym_id"] for row in db.all_tracks()} == set(SHORTS)
        assert "ревизия 42 не менялась" in "\n".join(log)

        # смена ревизии → снова идём за деталями
        account.likes_revision = 43
        account.calls.clear()
        export_library(account, db, on_progress=log.append)
        assert any(call.startswith("details:") for call in account.calls), account.calls
        db.close()

    print("\n".join(f"  | {line}" for line in log))
    print("OK: экспорт идемпотентен, служебные плейлисты пропущены, ссылки посчитаны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
