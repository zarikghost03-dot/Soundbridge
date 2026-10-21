"""Ручное ревью спорных совпадений прямо в терминале.

CSV (``soundbridge.report``) удобен, когда треков много и хочется разобрать их
пачкой в таблице. Этот модуль — для «быстро докидать десяток»: показываем трек
из Яндекса, показываем кандидатов Spotify, жмём цифру / ``s`` / ``m``.

Все решения уходят в те же таблицы (``override`` + ``match``), что и CSV-ревью,
поэтому два способа можно мешать как угодно.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from .spotify_client import to_uri
from .storage import Database

HELP = """
  1..9 — взять кандидата с этим номером
  s    — трек не переносим (пропустить)
  m    — вставить spotify-URI / ссылку / id вручную
  n    — пропустить, вернуться к нему позже (ничего не сохраняем)
  d    — показать кандидатов ещё раз
  q    — закончить
  ?    — эта справка
""".strip()

#: как часто сбрасывать накопленные решения в базу, чтобы не потерять их при обрыве
FLUSH_EVERY = 25


def _artists_of(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(a) for a in raw if a)
    text = str(raw)
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
    return f"{total // 60}:{total % 60:02d}" if total > 0 else ""


def pending(db: Database) -> list[Any]:
    """Всё спорное: низкая уверенность или нет результата."""
    return list(db.list_review())


def _show(row: Any, index: int, total: int) -> None:
    method = row["method"] or "нет решения"
    score = row["confidence"]
    print()
    print(f"─" * 78)
    print(f"[{index + 1}/{total}] {row['title'] or '(без названия)'}"
          + (f" [{row['version']}]" if row["version"] else ""))
    print(f"   {row['ym_id']} · исполнитель: {_artists_of(row['artists']) or '—'}")
    print(f"   альбом: {row['album'] or '—'} · длительность: {_mmss(row['duration_ms'])}"
          f" · ISRC: {row['isrc'] or '—'} · в плейлистах: {row['n_refs']}")
    if method == "no_result":
        print("   ⚠ Spotify-аналог не найден (или помечен непереносимым)")
    elif method == "review":
        print(f"   ⚠ слишком низкая уверенность: {float(score or 0):.2f}")
    else:
        print(f"   метод: {method} · уверенность: {float(score or 0):.2f}")
    if row["note"]:
        print(f"   заметка матчера: {row['note']}")

    cands = _candidates(row)
    if not cands:
        print("   кандидатов нет — вставьте URI сами (m) или пропустите (s)")
        return
    print("   кандидаты:")
    for i, cand in enumerate(cands[:9], start=1):
        mark = "→" if cand.get("uri") == row["uri"] else " "
        print(f"   {mark} {i}. {cand.get('name') or cand.get('title') or '(без имени)'}"
              f" — {_artists_of(cand.get('artists')) or '—'}")
        print(f"        {cand.get('uri')} · {cand.get('album') or '—'} · "
              f"{_mmss(cand.get('duration_ms'))} · "
              f"оценка {float(cand.get('score') or cand.get('confidence') or 0):.2f}"
              f" · {cand.get('method') or cand.get('how') or '?'}")


def _candidates(row: Any) -> list[dict[str, Any]]:
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


def apply(db: Database, decisions: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Один общий вход для CLI и интерактивного режима: ``(ym_id, uri)``."""
    pairs = [(ym, (uri or "").strip()) for ym, uri in decisions]
    pairs = [(ym, to_uri(uri)) if uri else (ym, "") for ym, uri in pairs if ym]
    if not pairs:
        return {"applied": 0, "cleared": 0, "missing": 0, "missing_detail": []}
    result = db.apply_overrides(pairs)
    db.log_event("review", f"ручных решений принято: {len(pairs)}")
    return {
        "applied": len(result["applied"]),
        "cleared": len(result["cleared"]),
        "missing": len(result["missing"]),
        "missing_detail": result["missing"][:20],
    }


def run_review(
    db: Database,
    limit: int = 0,
    reader: Callable[[str], str] = input,
    printer: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Интерактивный проход по спорным трекам.

    ``reader``/``printer`` можно подменить в тестах, чтобы не дёргать терминал.
    """
    rows = pending(db)
    if limit and limit > 0:
        rows = rows[:limit]
    if not rows:
        printer("Спорных треков нет — можно переходить к импорту.")
        return {"seen": 0, "decided": [], "skipped": [], "left": []}

    printer(f"Требуется решений: {len(rows)} (всего спорных {len(pending(db))})")
    printer(HELP)

    decided: list[str] = []
    skipped: list[str] = []
    left: list[str] = []
    batch: list[tuple[str, str]] = []

    for index, row in enumerate(rows):
        if len(batch) >= FLUSH_EVERY:
            _flush(db, batch)
        _show(row, index, len(rows))
        while True:
            try:
                answer = (reader("решение> ") or "").strip().lower()
            except EOFError:
                answer = "q"
            if answer in {"", "n", "next"}:
                left.append(row["ym_id"])
                break
            if answer in {"q", "quit", "exit"}:
                printer("Останавливаем ревью; несохранённые решения не применяются.")
                _flush(db, batch)
                return {"seen": index + 1, "decided": decided, "skipped": skipped,
                        "left": left + [r["ym_id"] for r in rows[index + 1:]]}
            if answer in {"?", "h", "help"}:
                printer(HELP)
                continue
            if answer == "d":
                _show(row, index, len(rows))
                continue
            if answer in {"s", "skip"}:
                batch.append((row["ym_id"], ""))
                skipped.append(row["ym_id"])
                printer("  ✓ помечен непереносимым")
                break
            if answer.startswith("m"):
                rest = answer[1:].strip()
                try:
                    value = rest or reader("spotify URI / ссылка / id> ")
                except EOFError:
                    value = ""
                uri = to_uri((value or "").strip())
                if not uri:
                    printer("  пусто — ничего не сохраняем, жмём n чтобы пропустить")
                    continue
                batch.append((row["ym_id"], uri))
                decided.append(row["ym_id"])
                printer(f"  ✓ {uri}")
                break
            if answer.isdigit():
                cands = _candidates(row)
                num = int(answer)
                if not (1 <= num <= len(cands)):
                    printer(f"  нет кандидата №{num} (есть {min(len(cands), 9)})")
                    continue
                uri = to_uri(cands[num - 1].get("uri") or "")
                if not uri:
                    printer("  у кандидата кривой URI — возьмите другой или вставьте сами")
                    continue
                batch.append((row["ym_id"], uri))
                decided.append(row["ym_id"])
                printer(f"  ✓ {uri}")
                break
            printer("  не понял. ? — справка, q — выйти")

    _flush(db, batch)
    printer(f"Готово: сохранено {len(decided)}, помечено непереносимыми {len(skipped)},"
            f" отложено {len(left)}")
    return {"seen": len(rows), "decided": decided, "skipped": skipped, "left": left}


def _flush(db: Database, batch: list[tuple[str, str]]) -> None:
    if not batch:
        return
    apply(db, list(batch))
    batch.clear()
