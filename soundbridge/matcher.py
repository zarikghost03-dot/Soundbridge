"""Каскад сопоставления треков Яндекс → Spotify.

Порядок попыток (от дешёвых к дорогим):

1. ``isrc``   — совпал международный код записи, сомневаться нечего.
2. ``exact``  — совпал нормализованный ключ ``исполнитель :: название``
   с чем-то из уже загруженного индекса (медиатека + результаты прошлых поисков).
3. ``search`` — только если первых двух не хватило: до двух запросов к /search
   и скоринг кандидатов через rapidfuzz.

Решение сохраняется вместе со ВСЕМИ кандидатами и их оценками. Пороги в [match]
можно менять и пересобирать классификацию офлайн, не дёргая Spotify
(``store.recompute_match_methods``).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from rapidfuzz import fuzz

from .config import Config, MatchCfg
from .normalize import TrackText, duration_score, has_cyrillic, is_ugc, norm, transliterate
from .spotify_client import SpotifyError, SpotifyAccount, track_to_row
from .storage import Database

ProgressFn = Callable[[str], None]

TOP_CANDIDATES = 5


def _silent(_: str) -> None:
    return None


def _val(cand: Any, key: str, default: Any = None) -> Any:
    """Одинаковое чтение для sqlite3.Row и dict-кандидата."""
    try:
        value = cand[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if not value:
        return []
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return [str(value)]
    return [str(a) for a in data if a] if isinstance(data, list) else []


VERSION_HINTS = (
    "live", "remaster", "remix", "acoustic", "instrumental", "demo", "edit",
    "version", "mix", "mono", "stereo", "take", "session", "outtake", "alt", "remake",
)


def looks_versioned(title: str) -> bool:
    lower = (title or "").lower()
    return any(f"({tag}" in lower or f"[{tag}" in lower or f" {tag} " in lower for tag in VERSION_HINTS)


class Matcher:
    def __init__(self, store: Database, sp: SpotifyAccount, cfg: Config | None = None):
        self.store = store
        self.sp = sp
        self.cfg = cfg or Config()
        self.m: MatchCfg = self.cfg.match
        self.index: dict[str, dict[str, list]] = {"pair": {}, "isrc": {}, "title": {}}
        self.decided: dict[str, tuple[str, float]] = {}
        self.new_candidates: dict[str, dict[str, Any]] = {}
        self.searches = 0
        self._sp_keys: dict[str, tuple[list[str], list[str]]] = {}

    # -- подготовка ---------------------------------------------------------
    def load_index(self) -> None:
        base = self.store.sp_index()
        self.index["pair"] = base.get("pair", {})
        self.index["isrc"] = base.get("isrc", {})
        by_title: dict[str, list] = {}
        seen: set[str] = set()
        for rows in self.index["pair"].values():
            for row in rows:
                if row["uri"] not in seen:
                    seen.add(row["uri"])
            for row in rows:
                if row["title_key"]:
                    by_title.setdefault(row["title_key"], []).append(row)
        for row in self.store.conn.execute("SELECT * FROM sp_track WHERE title_key IS NOT NULL"):
            if row["uri"] not in seen:
                by_title.setdefault(row["title_key"], []).append(row)
        self.index["title"] = by_title
        for ym_id, (uri, _method, conf) in self.store.resolved().items():
            row = self.store.track(ym_id)
            if row is None or not row["pair_key"]:
                continue
            prev = self.decided.get(row["pair_key"])
            if prev is None or prev[1] < conf:
                self.decided[row["pair_key"]] = (uri, conf)

    # -- текст трека --------------------------------------------------------
    def text_for(self, row: Any) -> TrackText:
        artists = _as_list(_val(row, "artists", []))
        title = _val(row, "title", "") or ""
        version = _val(row, "version", "") or ""
        if version:
            title = f"{title} {version}"
        return TrackText.build(title, artists[0] if artists else "",
                              _val(row, "album", "") or "",
                              int(_val(row, "duration_ms", 0) or 0),
                              _val(row, "isrc", "") or None)

    def _keys_of(self, cand: Any) -> tuple[list[str], list[str]]:
        """Нормализованные ключи кандидата: берём из БД, для свежего поиска считаем."""
        uri = _val(cand, "uri", "")
        cached = self._sp_keys.get(uri)
        if cached:
            return cached
        title_key = _val(cand, "title_key", "")
        artist_key = _val(cand, "artist_key", "")
        if title_key and artist_key:
            pair = ([title_key], [artist_key])
        else:
            artists = _as_list(_val(cand, "artists", []))
            text = TrackText.build(_val(cand, "name", "") or "", artists[0] if artists else "")
            pair = (text.title_keys or [""], text.artist_keys or [""])
        self._sp_keys[uri] = pair
        return pair

    # -- оценка -------------------------------------------------------------
    def score(self, text: TrackText, cand: Any) -> float:
        title_keys, artist_keys = self._keys_of(cand)
        dur = duration_score(text.duration_ms, int(_val(cand, "duration_ms", 0) or 0),
                             self.m.duration_tolerance_ms, self.m.duration_hard_ms)
        if dur <= 0.0:
            return 0.0
        title = _best_ratio(text.title_keys, title_keys)
        artist = _best_ratio(text.artist_keys, artist_keys)
        weights = self.m.weight_title + self.m.weight_artist + self.m.weight_duration
        value = (self.m.weight_title * title + self.m.weight_artist * artist
                 + self.m.weight_duration * dur) / weights
        if text.versioned != looks_versioned(_val(cand, "name", "") or ""):
            value -= self.m.version_penalty
        album_a, album_b = norm(text.album), norm(_val(cand, "album", "") or "")
        if album_a and album_a == album_b:
            value += 0.02
        return max(0.0, min(1.0, value))

    def _candidate(self, cand: Any, score: float, *, isrc_match: bool = False,
                   exact: bool = False) -> dict[str, Any]:
        return {
            "uri": _val(cand, "uri"),
            "id": _val(cand, "id"),
            "name": _val(cand, "name", ""),
            "artists": _as_list(_val(cand, "artists", [])),
            "album": _val(cand, "album", ""),
            "duration_ms": _val(cand, "duration_ms", 0),
            "isrc": _val(cand, "isrc", ""),
            "explicit": 1 if _val(cand, "explicit", 0) else 0,
            "score": round(float(score), 4),
            "isrc_match": bool(isrc_match),
            "exact": bool(exact),
        }

    def _remember(self, cand: dict[str, Any]) -> None:
        """Кандидаты из /search кладём в кэш sp_track: он ещё пригодится."""
        self.new_candidates[cand["uri"]] = cand

    # -- этап 1-2: локальный индекс ----------------------------------------
    def offline(self, row: Any, text: TrackText) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        isrc = (_val(row, "isrc", "") or "").strip().upper()
        if isrc:
            for cand in self.index["isrc"].get(isrc, []):
                found[_val(cand, "uri")] = self._candidate(cand, 1.0, isrc_match=True)
        if found:
            return sorted(found.values(), key=lambda c: -c["score"])[:TOP_CANDIDATES]

        for key in text.pair_keys[:6]:
            for cand in self.index["pair"].get(key, []):
                value = 1.0 if key == text.pair_key else 0.97
                score = min(value, max(value, self.score(text, cand)))
                prev = found.get(_val(cand, "uri"))
                if prev is None or prev["score"] < score:
                    found[_val(cand, "uri")] = self._candidate(cand, score, exact=True)
        for key in text.title_keys[:2]:
            for cand in self.index["title"].get(key, [])[:20]:
                score = self.score(text, cand)
                if score < self.m.review_score:
                    continue
                prev = found.get(_val(cand, "uri"))
                if prev is None or prev["score"] < score:
                    found[_val(cand, "uri")] = self._candidate(cand, score)
        return sorted(found.values(), key=lambda c: -c["score"])[:TOP_CANDIDATES]

    # -- этап 3: поиск в Spotify -------------------------------------------
    def queries_for(self, row: Any, text: TrackText) -> list[str]:
        raw_title = (_val(row, "title", "") or "").strip()
        artists = _as_list(_val(row, "artists", []))
        raw_artist = (artists[0] if artists else "").strip()
        out: list[str] = []
        if raw_title and raw_artist:
            out.append(f'track:"{raw_title}" artist:"{raw_artist}"')
        if raw_title:
            out.append(f"{raw_title} {raw_artist}".strip())
            if has_cyrillic(raw_title) or has_cyrillic(raw_artist):
                out.append(f"{transliterate(raw_title)} {transliterate(raw_artist)}".strip())
        dedup, seen = [], set()
        for query in out:
            query = query.strip()
            if query and query not in seen:
                seen.add(query)
                dedup.append(query)
        return dedup[:2]

    def online(self, row: Any, text: TrackText, budget: int = 2) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for query in self.queries_for(row, text)[:budget]:
            try:
                items = self.sp.search_track(query, limit=self.m.search_limit)
            except SpotifyError as exc:
                if exc.status == 429:
                    raise
                self.store.log_event("search_error", f"{query}: {exc}")
                continue
            self.searches += 1
            for raw in items:
                cand = track_to_row(raw)
                if not cand:
                    continue
                self._remember(cand)
                score = self.score(text, cand)
                prev = found.get(cand["uri"])
                if prev is None or prev["score"] < score:
                    found[cand["uri"]] = dict(cand, score=round(score, 4),
                                              isrc_match=False, exact=False)
            if any(c["score"] >= self.m.auto_score for c in found.values()):
                break
        return sorted(found.values(), key=lambda c: -c["score"])[:TOP_CANDIDATES]

    # -- классификация ------------------------------------------------------
    def save(self, row: Any, text: TrackText, candidates: list[dict[str, Any]],
             searched: bool) -> str:
        ym_id = row["ym_id"]
        if not candidates:
            note = ""
            if is_ugc(_val(row, "type", "")):
                note = "UGC-загрузка: в Spotify такое обычно отсутствует"
            self.store.save_match(ym_id, None, 0.0, "no_result",
                                  query="поиск: пусто" if searched else "индекс: пусто",
                                  candidates=[], note=note)
            return "no_result"

        best = candidates[0]
        score = float(best["score"])
        method = "isrc" if best.get("isrc_match") else "exact" if best.get("exact") else "fuzzy"
        uri: str | None = best["uri"]
        if score < self.m.review_score:
            method, uri = "no_result", None
        elif score < self.m.auto_score:
            method = "review"
        prepared = [dict(c, best=(i == 0)) for i, c in enumerate(candidates)]
        self.store.save_match(ym_id, uri, score, method,
                              query="через поиск" if searched else "по индексу",
                              candidates=prepared)
        if uri and score >= self.m.auto_score and text.pair_key:
            self.decided.setdefault(text.pair_key, (uri, score))
        return method if uri is not None else "no_result"

    # -- основной цикл ------------------------------------------------------
    def run(
        self,
        progress: ProgressFn = _silent,
        ids: list[str] | None = None,
        force: bool = False,
        search: bool = True,
        limit: int | None = None,
        checkpoint_every: int | None = None,
    ) -> dict[str, Any]:
        if ids is None:
            ids = [t["ym_id"] for t in self.store.all_tracks()] if force else self.store.pending_ym_ids()
            if force:
                ids = [i for i in ids if not self._has_override(i)]
        if limit:
            ids = ids[:limit]
        total = len(ids)
        stats = {"total": total, "isrc": 0, "exact": 0, "fuzzy": 0, "review": 0,
                 "no_result": 0, "cached": 0, "searches": 0}
        if not total:
            progress("Нечего сопоставлять: у всех треков есть решение.")
            return stats

        self.load_index()
        step = checkpoint_every or self.cfg.throttle.checkpoint_every
        progress(f"Сопоставляем {total} треков…")
        try:
            for done, ym_id in enumerate(ids, 1):
                row = self.store.track(ym_id)
                if row is None:
                    continue
                text = self.text_for(row)
                hit = self.decided.get(text.pair_key) if not force else None
                if hit:
                    uri, conf = hit
                    cached = self.store.conn.execute(
                        "SELECT * FROM sp_track WHERE uri=?", (uri,)).fetchone()
                    if cached is not None:
                        self.store.save_match(ym_id, uri, conf, "cached", query="повтор пары",
                                              candidates=[self._candidate(cached, conf, exact=True)])
                        stats["cached"] += 1
                        self._tick(done, total, stats, step, progress)
                        continue

                candidates = self.offline(row, text)
                searched = False
                good_enough = bool(candidates) and candidates[0]["score"] >= self.m.auto_score
                if search and not good_enough:
                    extra = self.online(row, text, budget=1 if candidates else 2)
                    searched = True
                    candidates = sorted(candidates + extra, key=lambda c: -c["score"])[:TOP_CANDIDATES]
                stats[self.save(row, text, candidates, searched)] += 1
                if self.new_candidates:
                    self.flush(progress=None)
                self._tick(done, total, stats, step, progress)
        except SpotifyError as exc:
            self.flush()
            self.store.commit()
            raise SystemExit(
                f"Spotify отказал ({exc}). Прогресс сохранён — повтори тот же шаг, "
                "сначала используй кэш: уменьшите частоту запросов в [throttle]."
            ) from exc
        self.flush()
        self.store.commit()
        stats["searches"] = self.searches
        self.store.log_event("match", f"processed={total} searches={self.searches}")
        return stats

    def _tick(self, done: int, total: int, stats: dict[str, Any], step: int,
              progress: ProgressFn) -> None:
        if done % step:
            return
        self.store.commit()
        self.store.checkpoint("match", {"processed": done, "total": total})
        progress(f"  {done}/{total} · isrc {stats['isrc']} · точные {stats['exact']} · "
                 f"похожие {stats['fuzzy']} · на ревью {stats['review']} · "
                 f"пусто {stats['no_result']} · из кэша {stats['cached']} · "
                 f"запросов {self.searches}")

    def _has_override(self, ym_id: str) -> bool:
        return self.store.conn.execute(
            "SELECT 1 FROM override WHERE ym_id=? LIMIT 1", (ym_id,)
        ).fetchone() is not None

    def flush(self, progress: Callable[[str], None] | None = None) -> None:
        if not self.new_candidates:
            return
        rows = list(self.new_candidates.values())
        self.store.upsert_sp_candidates(rows)
        if progress:
            progress(f"  закешировано новых треков Spotify: {len(rows)}")
        self.new_candidates = {}


def _best_ratio(keys_a: list[str], keys_b: list[str]) -> float:
    best = 0.0
    for a in keys_a[:3]:
        if not a:
            continue
        for b in keys_b[:3]:
            if not b:
                continue
            if a == b:
                return 1.0
            best = max(best, fuzz.token_set_ratio(a, b) / 100.0)
    return best
