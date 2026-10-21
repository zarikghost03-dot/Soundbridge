"""Ручное ревью соответствий.

Два способа работать:
  * `out/review.csv` — таблица (см. report.py), удобно в Excel;
  * интерактивный режим — проходит по самым «дорогим» позициям прямо в терминале.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import Settings
from .normalize import display_track
from .storage import Database


def _candidates(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        return json.loads(row["candidates"] or "[]")
    except ValueError:
        return []


def show(settings: Settings, limit: int = 25) -> dict[str, Any]:
    """Печатает список того, что требует решения."""
    db = Database(settings.db_path)
    try:
        counts = db.review_counts()
        rows = db.list_review()
        print("— Требует решения —")
        print(f"  не найдено: {counts['no_result']}, сомнительно: {counts['review']}, "
              f"нечёткие: {counts['fuzzy']}, без матчинга: {counts['none']}, "
              f"всего: {counts['total']}")
        if not rows:
            print("  пусто — можно запускать import")
        for i, row in enumerate(rows[:limit], start=1):
            print(f"\n{i}. {display_track(row['artists'], row['title'], row['version'])}"
                  f" [{row['ym_id']}] · используется в {row['n_refs']} местах")
            print(f"   Яндекс: {row['album'] or '—'} · {row['duration_ms'] or 0} мс"
                  f" · ISRC {row['isrc'] or 'нет'}")
            if row["uri"]:
                print(f"   сейчас стоит: {row['sp_artists']} — {row['sp_name']}"
                      f" ({row['method']}, {round(float(row['confidence'] or 0), 2)})")
                print(f"     {row['uri']}")
            else:
                print("   сейчас стоит: (пусто)")
            for j, cand in enumerate(_candidates(row)[:3], start=1):
                mark = ""
                if cand.get("duration_ms") and row["duration_ms"]:
                    delta = abs(int(cand["duration_ms"]) - int(row["duration_ms"]))
                    mark = f" · Δ{delta / 1000:.1f} c"
                print(f"   {j}) {cand.get('score', 0):.2f} {cand.get('artists')} — "
                      f"{cand.get('title')} [{cand.get('album')}] {cand.get('duration_ms')}мс{mark}")
                print(f"      {cand.get('uri')}")
        if len(rows) > limit:
            print(f"\n…и ещё {len(rows) - limit}. Полный список: out/review.csv")
        print("\nРешение: `python -m soundbridge review --set <ym_id> <spotify_uri>` "
              "или заполни колонку decision_uri в out/review.csv и `review --apply`.")
        return {"total": counts["total"], "shown": min(limit, len(rows))}
    finally:
        db.close()


def set_decision(settings: Settings, ym_id: str, uri: str, note: str = "") -> None:
    db = Database(settings.db_path)
    try:
        if uri and not uri.startswith("spotify:track:"):
            track = db.track_of_uri(uri)
            if not track:
                raise SystemExit(
                    f"Spotify-трек {uri} не найден в базе. Возьми URI из списка кандидатов "
                    "или из ссылки на трек."
                )
        db.set_override(ym_id, uri, note)
        if uri:
            db.save_match(ym_id, uri, 1.0, "override", note=note or "решение пользователя")
        else:
            db.save_match(ym_id, None, 0.0, "no_result", note=note or "решение пользователя: не переносим")
        db.commit()
        action = f"{ym_id} → {uri}" if uri else f"{ym_id} → пропуск"
        print(f"Записано: {action}")
        print("Обнови матчинг (дешёвая операция, сети не трогает): python -m soundbridge match")
    finally:
        db.close()


def interactive(settings: Settings, limit: int = 30) -> dict[str, int]:
    """Проходит по списку: 1/2/3 выбрать кандидата, n — пропустить, q — выйти."""
    db = Database(settings.db_path)
    decided = {"chosen": 0, "skipped": 0}
    try:
        rows = [r for r in db.list_review() if _candidates(r)]
        if not rows:
            print("Нет позиций с кандидатами — сначала python -m soundbridge match")
            return decided
        print("Навигация: 1/2/3 — взять кандидата, n — не переносим, "
              "回车 — оставить как есть, q — выйти\n")
        for i, row in enumerate(rows[:limit], start=1):
            cands = _candidates(row)
            print(f"[{i}/{min(len(rows), limit)}] {display_track(row['artists'], row['title'], row['version'])}"
                  f" · {row['album'] or '—'} · {row['duration_ms'] or 0} мс · "
                  f"в плейлистах: {row['n_refs']}")
            for j, cand in enumerate(cands[:3], start=1):
                print(f"  {j}) {cand.get('score', 0):.2f} {cand.get('artists')} — "
                      f"{cand.get('title')} [{cand.get('album')}] {cand.get('duration_ms')}мс")
                print(f"     {cand.get('uri')}")
            try:
                answer = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if answer in ("q", "quit", "exit"):
                break
            if answer == "n":
                db.set_override(row["ym_id"], "", "отклонено вручную")
                decided["skipped"] += 1
                continue
            if answer in ("1", "2", "3") and len(cands) >= int(answer):
                uri = cands[int(answer) - 1]["uri"]
                db.set_override(row["ym_id"], uri, "выбрано вручную")
                decided["chosen"] += 1
                print(f"  принято: {uri}")
                continue
            print("  пропущено без изменений")
        db.commit()
    finally:
        db.close()
    if decided["chosen"] or decided["skipped"]:
        print(f"\nПринято {decided['chosen']}, отклонено {decided['skipped']}. "
              "Обнови матчинг: python -m soundbridge match")
    return decided
