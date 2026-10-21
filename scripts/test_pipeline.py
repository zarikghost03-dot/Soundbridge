# -*- coding: utf-8 -*-
"""Смоук-тест всего конвейера офлайн: сид → CSV-ревью → план → dry-run → запись.

Spotify здесь подменён фейком (FakeSp), поэтому тест не требует ни сети, ни
токенов. Запуск из корня проекта::

    $env:PYTHONPATH="."; .venv\\Scripts\\python.exe -X utf8 scripts/test_pipeline.py
"""

from __future__ import annotations

import csv
import io
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from soundbridge.config import load_config  # noqa: E402
from soundbridge.importer import build_plan, run_import, verify  # noqa: E402
from soundbridge.report import apply_decisions, write_review  # noqa: E402
from soundbridge.review import pending, run_review  # noqa: E402
from soundbridge.storage import Database  # noqa: E402

A_URI = "spotify:track:AAAA"
B_URI = "spotify:track:BBBB"        # «нечёткое» совпадение матчера — плохое
B2_URI = "spotify:track:B2B2B2"     # то, что выбирает человек из кандидатов


class FakeSp:
    """Минимальная подделка SpotifyAccount для стадии записи."""

    def __init__(self) -> None:
        self.playlists: dict[str, dict] = {}
        self.saved: set[str] = set()
        self._n = 0
        self.user_id = "fake-user"

    def create_playlist(self, name, description="", public=False):
        self._n += 1
        pid = f"fake{self._n}"
        self.playlists[pid] = {"name": name, "uris": []}
        return {"id": pid, "uri": f"spotify:playlist:{pid}"}

    def playlist_tracks(self, playlist_id, limit=None):
        return [{"uri": u} for u in self.playlists.get(playlist_id, {}).get("uris", [])]

    def add_tracks_in_batches(self, playlist_id, uris, on_progress=None):
        self.playlists[playlist_id]["uris"].extend(uris)
        return len(uris)

    def save_tracks_in_batches(self, uris, on_progress=None):
        self.saved.update(uris)
        return len(uris)

    def get(self, path, params=None):
        if path.startswith("/playlists/"):
            pid = path.rsplit("/", 1)[-1]
            return {"tracks": {"total": len(self.playlists.get(pid, {}).get("uris", []))}}
        if path.startswith("/me/tracks"):
            return {"total": len(self.saved)}
        return {}


def seed(db: Database) -> None:
    db.upsert_ym_tracks([
        {"ym_id": "ymA", "title": "Песня А", "artists": ["Артист А"], "album": "Альбом",
         "duration_ms": 200_000, "isrc": "RU-A-001"},
        {"ym_id": "ymB", "title": "Песня B", "artists": ["Артист B"], "album": "Альбом",
         "duration_ms": 180_000},
        {"ym_id": "ymC", "title": "Ремикс C", "artists": ["Артист C"], "album": "Сингл",
         "duration_ms": 240_000},
    ])
    db.replace_playlist(kind="user1", title="Мой рок",
                        tracks=[("ymA", 1), ("ymB", 2), ("ymC", 3)])
    db.replace_playlist(kind="likes", title="Мне нравится",
                        tracks=[("ymA", 1), ("ymB", 2)])
    db.save_match("ymA", A_URI, 1.0, "isrc")
    db.save_match("ymB", B_URI, 0.61, "fuzzy",
                  candidates=[{"uri": B2_URI, "title": "Song B", "artists": ["Artist B"],
                               "duration_ms": 180_000, "score": 0.61, "best": True},
                              {"uri": B_URI, "title": "Song B (demo)", "artists": ["Artist B"],
                               "duration_ms": 170_000, "score": 0.55}])
    db.save_match("ymC", None, 0.0, "no_result", candidates=[], note="нет в Spotify")
    db.replace_saved([(A_URI, "2024-01-01")])  # ~700 «уже в медиатеке» — одна строка для примера


def test_csv_review(db: Database, out_dir: Path) -> None:
    stats = write_review(db, path=out_dir / "review.csv")
    assert stats["written"] == 2, stats  # B (fuzzy<1) и C (no_result)

    path = out_dir / "review.csv"
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        if row["ym_id"] == "ymB":
            row["decision_uri"] = "https://open.spotify.com/track/B2B2B2?si=123"
        elif row["ym_id"] == "ymC":
            row["decision_uri"] = "skip"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    preview = apply_decisions(db, path, dry_run=True)
    assert preview["would_apply"] == 1 and preview["would_skip"] == 1, preview

    result = apply_decisions(db, path)
    assert result["applied"] == 1 and result["skipped"] == 1, result

    resolved = db.resolved()
    assert resolved["ymB"][0] == B2_URI and resolved["ymB"][1] == "manual", resolved["ymB"]
    assert "ymC" not in resolved  # no_result в перенос не попадает
    assert resolved["ymA"][1] == "isrc"
    print("  csv-ревью: ок")


def test_interactive_review(db: Database) -> None:
    # ymC снова «не найден», но мы уже вынесли приговор — интерактив умеет
    # откладывать: ответ "n" ничего не сохраняет.
    rows = pending(db)
    answers = iter(["n"] * len(rows))
    log: list[str] = []
    result = run_review(db, reader=lambda _prompt: next(answers, "q"), printer=log.append)
    assert len(result["left"]) == len(rows), result
    assert db.resolved()["ymB"][1] == "manual"  # решения не сломались
    print(f"  интерактивное ревью: ок ({len(rows)} строк отложено)")


def test_plan_and_import(db: Database, cfg) -> None:
    cfg.import_.max_playlist_tracks = 1  # заставляем резать на томы
    plan = build_plan(db, cfg)
    keys = [t["key"] for t in plan["targets"]]
    assert keys == ["user1#v1", "user1#v2", "likes#v1", "likes#v2"], keys
    assert sum(len(t["uris"]) for t in plan["targets"]) == 4
    assert plan["likes"] == [A_URI, B2_URI]

    dry = run_import(db, None, cfg)  # type: ignore[arg-type]  # dry-run sp не трогает
    assert dry["dry_run"] and dry["tracks_added"] == 4 and dry["likes_saved"] == 1, dry

    cfg.import_.dry_run = False
    sp = FakeSp()
    summary = run_import(db, sp, cfg)
    assert summary["playlists_created"] == 4, summary
    assert summary["tracks_added"] == 4 and summary["likes_saved"] == 1, summary
    assert sp.saved == {B2_URI}  # A_URI уже лежит в медиатеке — повторно не сохраняем
    assert {u for pl in sp.playlists.values() for u in pl["uris"]} == {A_URI, B2_URI}
    assert db.pending_overrides() == []  # ручные решения закрыты импортом
    assert len(db.created()) == 4
    assert B2_URI in db.saved_uris()

    # повторный запуск с очищенным чекпоинтом — ничего не дублируем
    db.checkpoint("import", [])
    again = run_import(db, sp, cfg)
    assert again["playlists_created"] == 0 and again["tracks_added"] == 0, again
    assert again["playlists_reused"] == 4
    assert sum(len(pl["uris"]) for pl in sp.playlists.values()) == 4

    report = verify(db, sp)
    assert report["playlists"] == 4 and report["mismatches"] == 0, report
    assert report["saved_total"] == 1  # в фейковой медиатеке дописан только B2
    print("  план/импорт/верификация: ок")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = Database(tmp_path / "pipeline.db")
        try:
            cfg = load_config()
            seed(db)
            test_csv_review(db, tmp_path)
            test_interactive_review(db)
            test_plan_and_import(db, cfg)
        finally:
            db.close()
    print("PIPELINE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
