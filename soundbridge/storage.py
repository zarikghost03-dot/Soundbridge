"""SQLite-состояние Soundbridge.

Всё состояние живёт в одном файле data/soundbridge.db. Это принципиально:
прогон на десятки тысяч треков прерывается (квоты Spotify, выключенный ПК), и
каждый этап обязан продолжаться с того места, где остановился, а не начинать
заново. Кэш сопоставлений делает повторные прогоны бесплатными по запросам.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ym_track (
    ym_id        TEXT PRIMARY KEY,          -- "<trackId>:<albumId>"
    title        TEXT NOT NULL,
    version      TEXT,
    album        TEXT,
    artists      TEXT NOT NULL DEFAULT '[]',-- JSON-список имён
    duration_ms  INTEGER,
    isrc         TEXT,
    explicit     INTEGER,
    type         TEXT,
    state        TEXT,
    available    INTEGER,
    track_source TEXT,
    n_refs       INTEGER NOT NULL DEFAULT 0,
    title_key    TEXT,
    artist_key   TEXT,
    pair_key     TEXT,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS ix_ym_isrc ON ym_track(isrc);
CREATE INDEX IF NOT EXISTS ix_ym_pair ON ym_track(pair_key);
CREATE INDEX IF NOT EXISTS ix_ym_title ON ym_track(title_key);

-- артисты нормализуем один раз: Яндекс пишет «Kino» и «Кино» как разных
-- исполнителей, а матчингу нужна общая вершина
CREATE TABLE IF NOT EXISTS artist (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT 'ym'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_artist ON artist(name, source);

CREATE TABLE IF NOT EXISTS artist_alias (
    alias     TEXT NOT NULL,
    artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
    PRIMARY KEY (alias, artist_id)
);
CREATE INDEX IF NOT EXISTS ix_alias ON artist_alias(alias);

CREATE TABLE IF NOT EXISTS ym_playlist (
    kind           TEXT PRIMARY KEY,
    title          TEXT,
    description    TEXT,
    visibility     TEXT,
    owner_uid      TEXT,
    created        TEXT,
    modified       TEXT,
    n_items        INTEGER DEFAULT 0,
    ordinal        INTEGER DEFAULT 0,
    is_likes       INTEGER DEFAULT 0,
    skipped_reason TEXT,
    raw            TEXT
);

CREATE TABLE IF NOT EXISTS ym_playlist_item (
    kind  TEXT NOT NULL,
    pos   INTEGER NOT NULL,
    ym_id TEXT NOT NULL,
    PRIMARY KEY (kind, pos)
);
CREATE INDEX IF NOT EXISTS ix_item_ym ON ym_playlist_item(ym_id);

CREATE TABLE IF NOT EXISTS sp_track (
    uri         TEXT PRIMARY KEY,
    id          TEXT,
    name        TEXT,
    artists     TEXT NOT NULL DEFAULT '[]',
    album       TEXT,
    isrc        TEXT,
    duration_ms INTEGER,
    explicit    INTEGER,
    title_key   TEXT,
    artist_key  TEXT,
    pair_key    TEXT,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS ix_sp_isrc ON sp_track(isrc);
CREATE INDEX IF NOT EXISTS ix_sp_pair ON sp_track(pair_key);
CREATE INDEX IF NOT EXISTS ix_sp_title ON sp_track(title_key);

CREATE TABLE IF NOT EXISTS sp_saved (
    uri       TEXT PRIMARY KEY,
    added_at  TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS sp_playlist (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    owner       TEXT,
    total       INTEGER,
    snapshot_id TEXT,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS sp_playlist_item (
    playlist_id TEXT NOT NULL,
    pos         INTEGER NOT NULL,
    uri         TEXT NOT NULL,
    PRIMARY KEY (playlist_id, pos)
);

-- результат сопоставления; candidates хранит топ-N вариантов, чтобы менять
-- пороги уверенности и чинить матчасть без новых запросов к Spotify
CREATE TABLE IF NOT EXISTS match (
    ym_id      TEXT PRIMARY KEY,
    uri        TEXT,
    confidence REAL,
    method     TEXT,           -- isrc | exact | fuzzy | review | no_result | cached
    query      TEXT,
    candidates TEXT,
    note       TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_match_method ON match(method);

-- ручные решения из review.csv имеют высший приоритет
CREATE TABLE IF NOT EXISTS override (
    ym_id      TEXT PRIMARY KEY,
    uri        TEXT,
    note       TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS job (
    name       TEXT PRIMARY KEY,
    state      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS event (
    ts     TEXT,
    kind   TEXT,
    detail TEXT
);

-- то, что создали мы: нужно для verify и для отката
CREATE TABLE IF NOT EXISTS created_playlist (
    ym_kind TEXT PRIMARY KEY,
    sp_id   TEXT,
    sp_uri  TEXT,
    name    TEXT,
    volume  INTEGER DEFAULT 1,
    planned INTEGER DEFAULT 0,
    added   INTEGER DEFAULT 0,
    state   TEXT,
    updated_at TEXT
);
"""

# методы, которые считаем «переносимыми без проверки»
AUTOMATIC_METHODS = ("isrc", "exact", "cached", "manual")
ALL_METHODS = AUTOMATIC_METHODS + ("fuzzy", "review", "no_result")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA)
        self._ensure_columns()
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        if current > SCHEMA_VERSION:
            raise SystemExit(
                f"База {self.path} создана более новой версией Soundbridge ({current} > {SCHEMA_VERSION}). "
                "Обнови инструмент, не откатывайся назад."
            )
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- индексы ------------------------------------------------------------
    _NEW_COLUMNS = {
        "ym_track": {"title_key": "TEXT", "artist_key": "TEXT", "pair_key": "TEXT"},
        "sp_track": {"title_key": "TEXT", "artist_key": "TEXT", "pair_key": "TEXT"},
        "override": {"applied_at": "TEXT"},
    }

    def _ensure_columns(self) -> None:
        """Лёгкая миграция: базы, созданные раньше, дополняются колонками."""
        for table, columns in self._NEW_COLUMNS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        self.conn.commit()

    def _register_artist(self, name: str, source: str = "ym") -> int | None:
        from .normalize import keys_for, norm

        clean = (name or "").strip()
        if not clean or not norm(clean):
            return None
        row = self.conn.execute(
            "SELECT id FROM artist WHERE name=? AND source=?", (clean, source)
        ).fetchone()
        if row:
            artist_id = row["id"]
        else:
            cur = self.conn.execute(
                "INSERT INTO artist(name, source) VALUES(?,?)", (clean, source)
            )
            artist_id = cur.lastrowid
        for alias in keys_for(clean):
            self.conn.execute(
                "INSERT OR IGNORE INTO artist_alias(alias, artist_id) VALUES(?,?)",
                (alias, artist_id),
            )
        return artist_id

    def reindex_tracks(self) -> dict[str, int]:
        """Пересчитывает нормализованные ключи для обеих сторон.

        Вызывается после export и после snapshot: матчинг ищет дубли по этим
        ключам, поэтому они должны быть свежими.
        """
        from .normalize import TrackText

        touched_artists = 0
        for row in self.conn.execute("SELECT ym_id, title, version, artists FROM ym_track"):
            artists = json.loads(row["artists"] or "[]")
            title = row["title"] or ""
            if row["version"]:
                title = f"{title} {row['version']}"
            text = TrackText.build(title, artists[0] if artists else "")
            self.conn.execute(
                "UPDATE ym_track SET title_key=?, artist_key=?, pair_key=? WHERE ym_id=?",
                (text.title_key, text.artist_key, text.pair_key, row["ym_id"]),
            )
            for name in artists:
                if self._register_artist(name, "ym"):
                    touched_artists += 1

        for row in self.conn.execute("SELECT uri, name, artists FROM sp_track"):
            artists = json.loads(row["artists"] or "[]")
            text = TrackText.build(row["name"] or "", artists[0] if artists else "")
            self.conn.execute(
                "UPDATE sp_track SET title_key=?, artist_key=?, pair_key=? WHERE uri=?",
                (text.title_key, text.artist_key, text.pair_key, row["uri"]),
            )
            for name in artists:
                if self._register_artist(name, "sp"):
                    touched_artists += 1

        self.conn.commit()
        n_tracks = self.conn.execute("SELECT COUNT(*) FROM ym_track").fetchone()[0]
        return {"ym_tracks": n_tracks, "artist_refs": touched_artists}

    def artist_ids_by_alias(self, alias: str) -> list[int]:
        return [
            r["artist_id"]
            for r in self.conn.execute("SELECT artist_id FROM artist_alias WHERE alias=?", (alias,))
        ]

    def linked_artists(self, alias: str) -> list[str]:
        """Имена артистов с тем же нормализованным ключом (Кино = Kino)."""
        ids = self.artist_ids_by_alias(alias)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return [
            r["name"]
            for r in self.conn.execute(f"SELECT name FROM artist WHERE id IN ({marks})", ids)
        ]

    def sp_index(self) -> dict[str, list[sqlite3.Row]]:
        """Индекс медиатеки/кэда: pair_key и isrc → кандидаты Spotify."""
        by_pair: dict[str, list[sqlite3.Row]] = {}
        by_isrc: dict[str, list[sqlite3.Row]] = {}
        for row in self.conn.execute("SELECT * FROM sp_track"):
            if row["pair_key"]:
                by_pair.setdefault(row["pair_key"], []).append(row)
            isrc = (row["isrc"] or "").strip().upper()
            if isrc:
                by_isrc.setdefault(isrc, []).append(row)
        return {"pair": by_pair, "isrc": by_isrc}

    # -- meta / job -------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def checkpoint(self, job: str, state: Any) -> None:
        self.conn.execute(
            "INSERT INTO job(name,state,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (job, json.dumps(state, ensure_ascii=False, default=str), now()),
        )
        self.conn.commit()

    def resume(self, job: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT state FROM job WHERE name=?", (job,)).fetchone()
        return json.loads(row["state"]) if row else default

    def log_event(self, kind: str, detail: str) -> None:
        self.conn.execute("INSERT INTO event(ts,kind,detail) VALUES(?,?,?)", (now(), kind, detail))
        self.conn.commit()

    # -- Яндекс -----------------------------------------------------------
    @staticmethod
    def _as_json(value: Any) -> str:
        """Клиент мог прислать и словарь, и уже сериализованный JSON."""
        if value is None:
            return "{}"
        if isinstance(value, str):
            return value or "{}"
        return json.dumps(value, ensure_ascii=False, default=str)

    def upsert_ym_track(self, track: dict[str, Any], refs: int = 0) -> None:
        self.conn.execute(
            """INSERT INTO ym_track(ym_id,title,version,album,artists,duration_ms,isrc,explicit,type,state,
                                     available,track_source,n_refs,raw)
               VALUES(:ym_id,:title,:version,:album,:artists,:duration_ms,:isrc,:explicit,:type,:state,
                      :available,:track_source,:n_refs,:raw)
               ON CONFLICT(ym_id) DO UPDATE SET
                   title=excluded.title, version=excluded.version, album=excluded.album,
                   artists=excluded.artists, duration_ms=excluded.duration_ms, isrc=excluded.isrc,
                   explicit=excluded.explicit, type=excluded.type, state=excluded.state,
                   available=excluded.available, track_source=excluded.track_source,
                   n_refs=ym_track.n_refs + excluded.n_refs, raw=excluded.raw""",
            {
                "ym_id": track["ym_id"],
                "title": track.get("title") or "",
                "version": track.get("version"),
                "album": track.get("album"),
                "artists": json.dumps(track.get("artists") or [], ensure_ascii=False),
                "duration_ms": track.get("duration_ms"),
                "isrc": track.get("isrc") or None,
                "explicit": 1 if track.get("explicit") else 0,
                "type": track.get("type"),
                "state": track.get("state"),
                "available": 1 if track.get("available", True) else 0,
                "track_source": track.get("track_source"),
                "n_refs": refs,
                "raw": self._as_json(track.get("raw")),
            },
        )

    def add_playlist(self, playlist: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO ym_playlist(kind,title,description,visibility,owner_uid,created,modified,n_items,
                                       ordinal,is_likes,skipped_reason,raw)
               VALUES(:kind,:title,:description,:visibility,:owner_uid,:created,:modified,:n_items,
                      :ordinal,:is_likes,:skipped_reason,:raw)
               ON CONFLICT(kind) DO UPDATE SET
                   title=excluded.title, description=excluded.description, visibility=excluded.visibility,
                   created=excluded.created, modified=excluded.modified, n_items=excluded.n_items,
                   ordinal=excluded.ordinal, is_likes=excluded.is_likes,
                   skipped_reason=excluded.skipped_reason, raw=excluded.raw""",
            {
                "kind": playlist["kind"],
                "title": playlist.get("title"),
                "description": playlist.get("description"),
                "visibility": playlist.get("visibility"),
                "owner_uid": playlist.get("owner_uid"),
                "created": playlist.get("created"),
                "modified": playlist.get("modified"),
                "n_items": playlist.get("n_items") or 0,
                "ordinal": playlist.get("ordinal") or 0,
                "is_likes": 1 if playlist.get("is_likes") else 0,
                "skipped_reason": playlist.get("skipped_reason"),
                "raw": self._as_json(playlist.get("raw")),
            },
        )
        self.conn.execute("DELETE FROM ym_playlist_item WHERE kind=?", (playlist["kind"],))
        self.conn.executemany(
            "INSERT OR IGNORE INTO ym_playlist_item(kind,pos,ym_id) VALUES(?,?,?)",
            [(playlist["kind"], pos, ym_id) for pos, ym_id in enumerate(playlist.get("items") or [])],
        )

    def bump_refs(self, ym_ids: Iterable[str]) -> None:
        self.conn.executemany(
            "UPDATE ym_track SET n_refs=n_refs+1 WHERE ym_id=?",
            [(x,) for x in ym_ids],
        )

    def recount_refs(self) -> None:
        """Пересчитывает n_refs по факту членства в плейлистах (идемпотентно)."""
        self.conn.execute(
            """UPDATE ym_track SET n_refs = COALESCE((
                   SELECT COUNT(*) FROM ym_playlist_item WHERE ym_playlist_item.ym_id = ym_track.ym_id
               ), 0)"""
        )
        self.conn.commit()

    def upsert_ym_tracks(self, tracks: Iterable[dict[str, Any]]) -> int:
        rows = list(tracks)
        for track in rows:
            self.upsert_ym_track(track)
        self.conn.commit()
        return len(rows)

    def known_ym_track_ids(self) -> set[str]:
        return {r["ym_id"] for r in self.conn.execute("SELECT ym_id FROM ym_track")}

    def get_meta_int(self, key: str, default: int = 0) -> int:
        value = self.get_meta(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def playlist_item_count(self, kind: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM ym_playlist_item WHERE kind=?", (kind,)
        ).fetchone()[0])

    def replace_playlist(
        self,
        *,
        kind: str,
        title: str,
        tracks: Iterable[tuple[str, int]],
        visibility: str | None = None,
        created: str | None = None,
        modified: str | None = None,
        track_count: int = 0,
        generated_type: str = "",
        description: str | None = None,
        owner_uid: str | None = None,
        ordinal: int = 0,
        raw: Any = None,
    ) -> None:
        """Снимок одного плейлиста Яндекса вместе с порядком треков.

        ``tracks`` — пары ``(ym_id, позиция)``: порядок важен, Spotify-плейлист
        переносится в том же виде.
        """
        ordered = [ym_id for _, ym_id in sorted(((pos, ym) for ym, pos in tracks), key=lambda x: x[0])]
        self.add_playlist(
            {
                "kind": kind,
                "title": title,
                "description": description,
                "visibility": visibility,
                "owner_uid": owner_uid,
                "created": created,
                "modified": modified,
                "n_items": len(ordered) or track_count,
                "ordinal": ordinal,
                "is_likes": kind == "likes",
                "skipped_reason": generated_type or None,
                "raw": raw,
                "items": ordered,
            }
        )
        self.conn.commit()

    def playlists(self, include_skipped: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM ym_playlist"
        if not include_skipped:
            sql += " WHERE skipped_reason IS NULL"
        return self.conn.execute(sql + " ORDER BY ordinal").fetchall()

    def playlist_items(self, kind: str) -> list[str]:
        return [r["ym_id"] for r in self.conn.execute(
            "SELECT ym_id FROM ym_playlist_item WHERE kind=? ORDER BY pos", (kind,)
        )]

    def track(self, ym_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ym_track WHERE ym_id=?", (ym_id,)).fetchone()

    def all_tracks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ym_track ORDER BY n_refs DESC").fetchall()

    # -- Spotify ----------------------------------------------------------
    def upsert_sp_candidates(self, candidates: Iterable[dict[str, Any]]) -> None:
        from .normalize import TrackText

        rows = []
        for c in candidates:
            artists = c.get("artists") or []
            text = TrackText.build(c.get("name") or "", artists[0] if artists else "")
            rows.append(
                {
                    "uri": c["uri"],
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "artists": json.dumps(artists, ensure_ascii=False),
                    "album": c.get("album"),
                    "isrc": c.get("isrc"),
                    "duration_ms": c.get("duration_ms"),
                    "explicit": 1 if c.get("explicit") else 0,
                    "title_key": text.title_key,
                    "artist_key": text.artist_key,
                    "pair_key": text.pair_key,
                    "raw": json.dumps(c.get("raw") or {}, ensure_ascii=False, default=str),
                }
            )
        self.conn.executemany(
            """INSERT INTO sp_track(uri,id,name,artists,album,isrc,duration_ms,explicit,
                                     title_key,artist_key,pair_key,raw)
               VALUES(:uri,:id,:name,:artists,:album,:isrc,:duration_ms,:explicit,
                      :title_key,:artist_key,:pair_key,:raw)
               ON CONFLICT(uri) DO UPDATE SET name=excluded.name, artists=excluded.artists,
                   album=excluded.album, isrc=excluded.isrc, duration_ms=excluded.duration_ms,
                   explicit=excluded.explicit, title_key=excluded.title_key,
                   artist_key=excluded.artist_key, pair_key=excluded.pair_key""",
            rows,
        )

    def replace_saved(self, rows: Iterable[tuple[str, str | None]]) -> int:
        self.conn.execute("DELETE FROM sp_saved")
        data = [{"uri": uri, "added_at": added, "synced_at": now()} for uri, added in rows]
        self.conn.executemany(
            "INSERT OR IGNORE INTO sp_saved(uri,added_at,synced_at) VALUES(:uri,:added_at,:synced_at)", data
        )
        self.conn.commit()
        return len(data)

    def saved_uris(self) -> set[str]:
        return {r["uri"] for r in self.conn.execute("SELECT uri FROM sp_saved")}

    def replace_playlists(self, rows: Iterable[dict[str, Any]]) -> int:
        self.conn.execute("DELETE FROM sp_playlist")
        data = [
            {
                "id": r["id"],
                "name": r["name"],
                "owner": r.get("owner"),
                "total": r.get("total"),
                "snapshot_id": r.get("snapshot_id"),
                "synced_at": now(),
            }
            for r in rows
        ]
        self.conn.executemany(
            """INSERT OR REPLACE INTO sp_playlist(id,name,owner,total,snapshot_id,synced_at)
               VALUES(:id,:name,:owner,:total,:snapshot_id,:synced_at)""",
            data,
        )
        self.conn.commit()
        return len(data)

    def playlists_by_name(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = {}
        for row in self.conn.execute("SELECT * FROM sp_playlist"):
            out.setdefault((row["name"] or "").casefold().strip(), []).append(row)
        return out

    def remember_playlist_items(self, playlist_id: str, uris: Iterable[str]) -> None:
        self.conn.execute("DELETE FROM sp_playlist_item WHERE playlist_id=?", (playlist_id,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO sp_playlist_item(playlist_id,pos,uri) VALUES(?,?,?)",
            [(playlist_id, pos, uri) for pos, uri in enumerate(uris)],
        )
        self.conn.commit()

    def playlist_item_uris(self, playlist_id: str) -> set[str]:
        return {r["uri"] for r in self.conn.execute("SELECT uri FROM sp_playlist_item WHERE playlist_id=?", (playlist_id,))}

    # -- сопоставления ----------------------------------------------------
    def save_match(
        self,
        ym_id: str,
        uri: str | None,
        confidence: float,
        method: str,
        query: str = "",
        candidates: list[dict[str, Any]] | None = None,
        note: str = "",
    ) -> None:
        self.conn.execute(
            """INSERT INTO match(ym_id,uri,confidence,method,query,candidates,note,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(ym_id) DO UPDATE SET uri=excluded.uri, confidence=excluded.confidence,
                   method=excluded.method, query=excluded.query, candidates=excluded.candidates,
                   note=excluded.note, updated_at=excluded.updated_at""",
            (ym_id, uri, round(confidence, 4), method, query,
             json.dumps(candidates or [], ensure_ascii=False), note, now()),
        )

    def match_of(self, ym_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM match WHERE ym_id=?", (ym_id,)).fetchone()

    def pending_ym_ids(self, only_unmatched: bool = False) -> list[str]:
        """Треки, по которым ещё нет решения (или которые помечены на повтор)."""
        sql = """
            SELECT t.ym_id FROM ym_track t
            LEFT JOIN match m ON m.ym_id = t.ym_id
            LEFT JOIN override o ON o.ym_id = t.ym_id
            WHERE o.ym_id IS NULL
              AND (m.ym_id IS NULL OR m.method = 'retry')
        """
        if only_unmatched:
            sql += " AND m.method = 'no_result'"
        return [r["ym_id"] for r in self.conn.execute(sql + " ORDER BY t.n_refs DESC, t.ym_id")]

    def set_override(self, ym_id: str, uri: str | None, note: str = "") -> None:
        self.conn.execute(
            """INSERT INTO override(ym_id,uri,note,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(ym_id) DO UPDATE SET uri=excluded.uri, note=excluded.note, updated_at=excluded.updated_at""",
            (ym_id, uri, note, now()),
        )

    def resolved(self) -> dict[str, tuple[str, str, float]]:
        """ym_id -> (spotify_uri, method, confidence) с учётом ручных правок."""
        out: dict[str, tuple[str, str, float]] = {}
        for row in self.conn.execute(
            """SELECT t.ym_id AS ym_id, COALESCE(o.uri, m.uri) AS uri,
                      CASE WHEN o.ym_id IS NOT NULL THEN 'manual' ELSE COALESCE(m.method,'none') END AS method,
                      COALESCE(m.confidence, 0) AS confidence
               FROM ym_track t
               LEFT JOIN match m ON m.ym_id = t.ym_id
               LEFT JOIN override o ON o.ym_id = t.ym_id"""
        ):
            if row["uri"]:
                out[row["ym_id"]] = (row["uri"], row["method"], float(row["confidence"] or 0))
        return out

    # -- ручные решения и ревью -------------------------------------------
    def has_candidates(self, ym_id: str) -> bool:
        """Есть ли уже сохранённые кандидаты (тогда матчинг можно пересобрать офлайн)."""
        row = self.conn.execute(
            "SELECT 1 FROM match WHERE ym_id=? AND candidates IS NOT NULL AND candidates<>'[]' LIMIT 1",
            (ym_id,),
        ).fetchone()
        return row is not None

    def all_matches(self) -> dict[str, tuple[str | None, str, float]]:
        """ym_id -> (uri, method, confidence) из таблицы match (без override)."""
        out: dict[str, tuple[str | None, str, float]] = {}
        for row in self.conn.execute("SELECT ym_id, uri, method, confidence FROM match"):
            out[row["ym_id"]] = (row["uri"], row["method"] or "none", float(row["confidence"] or 0))
        return out

    def recompute_match_methods(self) -> int:
        """Пересортирует method по сохранённым кандидатам и текущим порогам.

        Пороги в config.toml меняют только способ отбора, сами кандидаты лежат
        в базе — так пересборка стоит нулевых запросов к Spotify.
        """
        from .config import load_config

        match_cfg = load_config().match
        touched = 0
        for row in self.conn.execute("SELECT ym_id, method, candidates FROM match"):
            if row["method"] not in {"isrc", "cached", "fuzzy"}:
                continue
            try:
                candidates = json.loads(row["candidates"] or "[]")
            except ValueError:
                continue
            best = next((c for c in candidates if c.get("best")), candidates[0] if candidates else None)
            if not best:
                self.conn.execute(
                    "UPDATE match SET method='no_result', uri=NULL, confidence=0 WHERE ym_id=?",
                    (row["ym_id"],),
                )
                touched += 1
                continue
            score = float(best.get("score") or 0)
            method = ("isrc" if best.get("isrc") else "exact" if best.get("exact") else "fuzzy")
            if score >= match_cfg.auto_score:
                method = "cached" if method == "fuzzy" else method
            elif score >= match_cfg.review_score:
                method = "fuzzy" if method == "cached" else method
            self.conn.execute(
                "UPDATE match SET uri=?, confidence=?, method=? WHERE ym_id=?",
                (best.get("uri"), round(score, 4), method, row["ym_id"]),
            )
            touched += 1
        self.conn.commit()
        return touched

    def clear_override(self, ym_id: str) -> None:
        """Возвращает трек на усмотрение матчинга."""
        self.conn.execute("DELETE FROM override WHERE ym_id=?", (ym_id,))
        self.conn.commit()

    def count_overrides(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM override").fetchone()[0]

    def pending_overrides(self) -> list[tuple[str, str]]:
        """Ручные решения, которые ещё не причесаны матчингом."""
        return [(r["ym_id"], r["uri"] or "") for r in self.conn.execute(
            "SELECT ym_id, uri FROM override WHERE applied_at IS NULL ORDER BY ym_id"
        ).fetchall()]

    def set_override_applied(self, ym_ids: Iterable[str]) -> None:
        for ym_id in ym_ids:
            self.conn.execute("UPDATE override SET applied_at=? WHERE ym_id=?", (now(), ym_id))
        self.conn.commit()

    def apply_overrides(self, overrides: Iterable[tuple[str, str]]) -> dict[str, Any]:
        """Вносит ручные решения ревью и поднимает их в match.

        Пустой uri означает «трек не переносим»: матчинг по нему должен
        показать no_result и с пустым списком кандидатов, иначе при следующем
        прогоне он будет молча восстановлен из старых кандидатов.
        """
        applied: list[str] = []
        cleared: list[str] = []
        missing: list[str] = []
        for ym_id, uri in overrides:
            known = self.conn.execute("SELECT 1 FROM ym_track WHERE ym_id=?", (ym_id,)).fetchone()
            if not known:
                missing.append(f"{ym_id} (нет такого трека в Яндексе)")
                continue
            if not uri:
                self.clear_override(ym_id)
                self.save_match(ym_id, None, 0.0, "no_result", query="", candidates=[],
                                note="в ревью помечен непереносимым")
                cleared.append(ym_id)
                continue
            if not self.conn.execute("SELECT 1 FROM sp_track WHERE uri=?", (uri,)).fetchone():
                # вручную вставленный URI может отсутствовать в кэше — заведём
                # заглушку, снимок или match дополнят метаданные позже
                self.conn.execute("INSERT OR IGNORE INTO sp_track(uri) VALUES(?)", (uri,))
            self.set_override(ym_id, uri, "ручное решение из ревью")
            self.conn.execute(
                """INSERT INTO match(ym_id,uri,confidence,method,query,candidates,note,updated_at)
                   VALUES(?,?,1.0,'manual','review','[]','ручное решение из ревью',?)
                   ON CONFLICT(ym_id) DO UPDATE SET uri=excluded.uri, confidence=1.0,
                       method='manual', note=excluded.note, updated_at=excluded.updated_at""",
                (ym_id, uri, now()),
            )
            applied.append(ym_id)
        self.conn.commit()
        return {"applied": applied, "cleared": cleared, "missing": missing}

    def track_of_uri(self, uri: str) -> dict[str, Any] | None:
        """Разворачивает uri в кандидата для sp_track (по ответу из кэша или пусто)."""
        row = self.conn.execute("SELECT * FROM sp_track WHERE uri=?", (uri,)).fetchone()
        if row:
            return {
                "uri": row["uri"], "id": row["id"], "name": row["name"],
                "artists": json.loads(row["artists"] or "[]"), "album": row["album"],
                "isrc": row["isrc"], "duration_ms": row["duration_ms"],
                "explicit": bool(row["explicit"]), "raw": {},
            }
        return None

    def list_review(self) -> list[sqlite3.Row]:
        """Всё, что требует ручного решения: сомнительные и ненайденные треки."""
        return self.conn.execute(
            """
            SELECT t.ym_id, t.title, t.version, t.album, t.artists, t.duration_ms, t.isrc,
                   t.n_refs, m.uri, m.confidence, m.method, m.candidates, m.note,
                   s.name AS sp_name, s.artists AS sp_artists, s.album AS sp_album,
                   s.duration_ms AS sp_duration_ms
            FROM ym_track t
            LEFT JOIN match m ON m.ym_id = t.ym_id
            LEFT JOIN sp_track s ON s.uri = m.uri
            WHERE m.method IS NULL
               OR m.method IN ('review','no_result')
               OR (m.method = 'fuzzy' AND COALESCE(m.confidence,0) < 1.0)
            ORDER BY t.n_refs DESC, COALESCE(m.confidence,0) ASC, t.ym_id
            """
        ).fetchall()

    def review_counts(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "review": q("SELECT COUNT(*) FROM match WHERE method='review'"),
            "no_result": q("SELECT COUNT(*) FROM match WHERE method='no_result'"),
            "uncertain": q("SELECT COUNT(*) FROM match WHERE method='fuzzy' AND COALESCE(confidence,0)<1.0"),
            "manual": q("SELECT COUNT(*) FROM override"),
            "untouched": q(
                "SELECT COUNT(*) FROM ym_track t LEFT JOIN match m ON m.ym_id=t.ym_id WHERE m.ym_id IS NULL"
            ),
        }

    def logs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT ts, kind, detail FROM event ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()

    def remember_saved(self, uris: Iterable[str]) -> int:
        """Дописывает в кэш медиатеки треки, которые мы сохранили сами.

        ``replace_saved`` перезаписывает таблицу целиком и теряет ``added_at``
        старых строк, а нам нужно лишь добавить новые URI после импорта.
        """
        data = [(uri, now(), now()) for uri in dict.fromkeys(uris) if uri]
        self.conn.executemany(
            "INSERT OR IGNORE INTO sp_saved(uri,added_at,synced_at) VALUES(?,?,?)", data
        )
        self.conn.commit()
        return len(data)

    def mark_playlist_imported(self, ym_kind: str, state: str = "imported") -> None:
        self.conn.execute(
            "UPDATE created_playlist SET state=?, updated_at=? WHERE ym_kind=?",
            (state, now(), ym_kind),
        )
        self.conn.commit()

    def remember_created(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO created_playlist(ym_kind,sp_id,sp_uri,name,volume,planned,added,state,updated_at)
               VALUES(:ym_kind,:sp_id,:sp_uri,:name,:volume,:planned,:added,:state,:updated_at)
               ON CONFLICT(ym_kind) DO UPDATE SET sp_id=excluded.sp_id, sp_uri=excluded.sp_uri,
                   name=excluded.name, volume=excluded.volume, planned=excluded.planned,
                   added=excluded.added, state=excluded.state, updated_at=excluded.updated_at""",
            {**{"volume": 1, "planned": 0, "added": 0}, **row, "updated_at": now()},
        )
        self.conn.commit()

    def created(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM created_playlist ORDER BY ym_kind").fetchall()

    def commit(self) -> None:
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "ym_tracks": q("SELECT COUNT(*) FROM ym_track"),
            "ym_tracks_with_isrc": q("SELECT COUNT(*) FROM ym_track WHERE isrc IS NOT NULL AND isrc<>''"),
            "ym_playlists": q("SELECT COUNT(*) FROM ym_playlist WHERE skipped_reason IS NULL"),
            "ym_playlists_skipped": q("SELECT COUNT(*) FROM ym_playlist WHERE skipped_reason IS NOT NULL"),
            "ym_items": q("SELECT COUNT(*) FROM ym_playlist_item"),
            "matched": q("SELECT COUNT(*) FROM match WHERE uri IS NOT NULL"),
            "match_isrc": q("SELECT COUNT(*) FROM match WHERE method='isrc'"),
            "match_exact": q("SELECT COUNT(*) FROM match WHERE method='exact'"),
            "match_fuzzy": q("SELECT COUNT(*) FROM match WHERE method='fuzzy'"),
            "match_review": q("SELECT COUNT(*) FROM match WHERE method='review'"),
            "match_none": q("SELECT COUNT(*) FROM match WHERE method='no_result'"),
            "overrides": q("SELECT COUNT(*) FROM override"),
            "spotify_saved": q("SELECT COUNT(*) FROM sp_saved"),
            "spotify_playlists": q("SELECT COUNT(*) FROM sp_playlist"),
            "sp_tracks": q("SELECT COUNT(*) FROM sp_track"),
            "created_playlists": q("SELECT COUNT(*) FROM created_playlist"),
            "quota_events": q("SELECT COUNT(*) FROM event WHERE kind LIKE 'http_%'"),
        }

    def method_breakdown(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT method, COUNT(*) AS n FROM match GROUP BY method ORDER BY n DESC"
        ).fetchall()


@contextmanager
def session(path: Path | str) -> Iterator[Database]:
    db = Database(path)
    try:
        yield db
        db.commit()
    finally:
        db.close()
