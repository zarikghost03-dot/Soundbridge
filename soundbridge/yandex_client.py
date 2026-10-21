"""Яндекс.Музыка: OAuth (device flow) и выгрузка библиотеки.

Пароль нигде не используется: ``Client.device_auth`` выдаёт код вида
``XXXX-XXXX``, который подтверждается в браузере. Полученный токен кладётся в
``secrets/yandex.json`` и дальше молча обновляется по refresh-токену.

Библиотека ``yandex-music`` умеет только отдавать объект токена, но не
обновлять его, поэтому ``refresh_access_token`` сделан вручную — обычный POST
на ``oauth.yandex.ru/token`` с публичными кредентами приложения.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import requests
from yandex_music import Client
from yandex_music.exceptions import (
    BadRequestError,
    DeviceAuthError,
    NetworkError,
    NotFoundError,
    TimedOutError,
    UnauthorizedError,
    YandexMusicError,
)

from .config import YANDEX_TOKEN_CACHE, Config, read_token_cache, write_token_cache
from .storage import Database

OAUTH_TOKEN_URL = "https://oauth.yandex.ru/token"
# Публичные OAuth-креденты официального Android-приложения Яндекс.Музыки
# (те же, что использует библиотека по умолчанию).
OAUTH_CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
OAUTH_CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"  # noqa: S105

TRACKS_CHUNK = 100

ProgressFn = Callable[[str], None]


def _now() -> int:
    return int(time.time())


def _silent(text: str) -> None:  # pragma: no cover - заглушка коллбэка
    pass


class YandexError(RuntimeError):
    """Любая ошибка Яндекса, которую умеет объяснять человек."""


@dataclass
class TokenBundle:
    """Кэш OAuth-токена (то же, что лежит в secrets/yandex.json)."""

    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    uid: int | None = None
    login: str = ""

    @property
    def expires_in(self) -> int:
        return max(0, self.expires_at - _now())

    def needs_refresh(self, skew: int = 3600) -> bool:
        return self.expires_at - skew <= _now()

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "TokenBundle":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=_now() + int(data.get("expires_in") or 31_536_000),
        )

    @classmethod
    def load(cls) -> "TokenBundle | None":
        payload = read_token_cache(YANDEX_TOKEN_CACHE)
        if not payload or not payload.get("access_token"):
            return None
        return cls(**{k: payload.get(k, v) for k, v in asdict(cls()).items()})

    def save(self) -> None:
        write_token_cache(YANDEX_TOKEN_CACHE, asdict(self))


def refresh_access_token(bundle: TokenBundle) -> TokenBundle:
    """Обновляет access-токен по refresh-токену."""
    if not bundle.refresh_token:
        raise YandexError("refresh-токена нет — нужна повторная авторизация")
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": bundle.refresh_token,
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
            },
            timeout=30,
        )
    except requests.RequestException as exc:  # pragma: no cover - сеть
        raise YandexError(f"не удалось обновить токен Яндекса: {exc}") from exc
    if response.status_code != 200:
        raise YandexError(
            f"Яндекс отказал в обновлении токена ({response.status_code}): {response.text[:200]}"
        )
    fresh = TokenBundle.from_response(response.json())
    fresh.uid, fresh.login = bundle.uid, bundle.login
    return fresh


def device_login(cfg: Config, on_code: Callable[[str, str], None] | None = None) -> TokenBundle:
    """Интерактивный OAuth device flow.

    ``on_code(user_code, verification_url)`` — коллбек для интерфейса: по нему
    печатаем код и ссылку, пока пользователь их подтверждает.
    """
    client = Client()

    def _show(code: Any) -> None:
        if on_code:
            on_code(code.user_code, code.verification_url)
        else:  # pragma: no cover - CLI всегда передаёт коллбек
            print(f"Подтвердите доступ: {code.verification_url}\nКод: {code.user_code}")

    try:
        token = client.device_auth(
            _show,
            poll_interval=cfg.yandex.device_poll_seconds,
            timeout=420,
        )
    except DeviceAuthError as exc:
        raise YandexError(
            f"device flow не завершён: {exc}. Попробуйте ещё раз и подтвердите код быстрее."
        ) from exc
    bundle = TokenBundle(
        access_token=token.access_token,
        refresh_token=token.refresh_token or "",
        expires_at=_now() + int(token.expires_in or 31_536_000),
    )
    bundle.save()
    return bundle


class YandexAccount:
    """Клиент Яндекса с автоматическим обновлением токена."""

    def __init__(self, cfg: Config, client: Client, bundle: TokenBundle) -> None:
        self.cfg = cfg
        self.client = client
        self.bundle = bundle
        self.uid: int = bundle.uid or 0
        self.login: str = bundle.login
        self._last_request = 0.0

    # -- подключение --------------------------------------------------------
    @classmethod
    def connect(
        cls,
        cfg: Config,
        *,
        force_login: bool = False,
        on_code: Callable[[str, str], None] | None = None,
        quiet: bool = False,
    ) -> "YandexAccount":
        bundle = None if force_login else TokenBundle.load()
        if bundle and bundle.needs_refresh():
            try:
                bundle = refresh_access_token(bundle)
                bundle.save()
                if not quiet:
                    print("Токен Яндекса обновлён.")
            except YandexError as exc:
                if not quiet:
                    print(f"{exc} — запрашиваю доступ заново.")
                bundle = None
        if bundle is None:
            bundle = device_login(cfg, on_code)
            if not quiet:
                print("Доступ к Яндекс.Музыке получен.")

        account = cls(cfg, Client(token=bundle.access_token, language="ru"), bundle)
        account._verify()
        return account

    def _verify(self) -> None:
        try:
            status = self.client.account_status()
        except UnauthorizedError as exc:
            raise YandexError(
                "Яндекс не принял токен. Запустите `soundbridge auth --force`."
            ) from exc
        except (NetworkError, TimedOutError) as exc:  # pragma: no cover - сеть
            raise YandexError(f"нет связи с Яндекс.Музыкой: {exc}") from exc
        self.uid = int(getattr(status, "uid", 0) or self.bundle.uid or 0)
        self.login = getattr(status, "login", "") or self.bundle.login
        can_premium = bool(getattr(status, "premium", False))
        self.bundle.uid, self.bundle.login = self.uid, self.login
        self.bundle.save()
        self.premium = can_premium

    # -- низкоуровневая вежливость ------------------------------------------
    def _polite(self) -> None:
        delay = self.cfg.yandex.request_delay_ms / 1000.0
        wait = self._last_request + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        attempts = 4
        for attempt in range(1, attempts + 1):
            self._polite()
            try:
                return func(*args, **kwargs)
            except (NetworkError, TimedOutError) as exc:
                if attempt == attempts:
                    raise YandexError(f"Яндекс недоступен: {exc}") from exc
                time.sleep(min(30, 2 ** attempt))
            except UnauthorizedError as exc:  # токен отозвали посреди прогона
                raise YandexError(
                    "Яндекс отозвал токен во время работы. Перелогиньтесь: "
                    "`soundbridge auth --force`."
                ) from exc
        return None

    # -- данные ------------------------------------------------------------
    def playlists(self) -> list[Any]:
        return list(self._call(self.client.users_playlists_list, self.uid) or [])

    def playlist(self, kind: str) -> Any:
        return self._call(self.client.users_playlists, kind, self.uid)

    def likes(self) -> tuple[list[Any], int]:
        """Лайки: (список TrackShort, revision для инкрементальных прогонов)."""
        result = self._call(self.client.users_likes_tracks, self.uid)
        if result is None:
            return [], 0
        return list(result.tracks or []), int(result.revision or 0)

    def tracks_by_ids(self, track_ids: Sequence[str]) -> list[Any]:
        """Достаёт детали треков пачками (Яндекс отдаёт не всё вложенным объектом)."""
        out: list[Any] = []
        ids = [str(i) for i in track_ids if i]
        for start in range(0, len(ids), TRACKS_CHUNK):
            chunk = ids[start : start + TRACKS_CHUNK]
            try:
                fetched = self._call(self.client.tracks, chunk) or []
            except (NotFoundError, BadRequestError, YandexError) as exc:
                # часть id могла протухнуть — достаём поодиночке, что осталось
                if isinstance(exc, YandexError):
                    raise
                for single in chunk:
                    try:
                        got = self._call(self.client.tracks, [single]) or []
                    except (NotFoundError, BadRequestError):
                        continue
                    out.extend(got)
                continue
            out.extend(fetched)
        return out


# --- разбор ответов -------------------------------------------------------


def _dump(obj: Any) -> str:
    """Сериализует модель библиотеки в JSON для колонки raw."""
    for method in ("to_json", "to_dict"):
        func = getattr(obj, method, None)
        if callable(func):
            try:
                value = func()
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
    data = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return json.dumps(data, ensure_ascii=False, default=str)


def _album_of(track: Any) -> Any:
    albums = getattr(track, "albums", None) or []
    return albums[0] if albums else None


def _artist_names(track: Any, fallback_source: str = "") -> list[str]:
    names = []
    for artist in getattr(track, "artists", None) or []:
        name = getattr(artist, "name", None)
        if name:
            names.append(name)
    if not names:
        # UGC-треки: артист лежит в ugc_artist_name / track_source
        for value in (getattr(track, "ugc_artist_name", None), fallback_source):
            if value:
                names.append(str(value))
    return names


def track_row(track: Any, album_id: Any = None) -> dict[str, Any] | None:
    """Превращает модель Track в словарь для ``storage.upsert_ym_track``."""
    if track is None:
        return None
    real_id = getattr(track, "real_id", None) or getattr(track, "id", None)
    album = _album_of(track)
    album_id = album_id or getattr(album, "id", None) or 0
    if album_id and ":" not in str(getattr(track, "id", "")):
        ym_id = f"{real_id}:{album_id}"
    else:
        raw_id = str(getattr(track, "id", "") or "")
        ym_id = raw_id if ":" in raw_id else f"{raw_id}:{album_id or 0}"
    if not ym_id or ym_id.startswith("None"):
        return None
    return {
        "ym_id": ym_id,
        "title": getattr(track, "title", "") or "",
        "version": getattr(track, "version", None),
        "album": getattr(album, "title", None) if album is not None else None,
        "artists": _artist_names(track),
        "duration_ms": int(getattr(track, "duration_ms", 0) or 0),
        "isrc": (getattr(track, "isrc", None) or "").strip().upper() or None,
        "explicit": 1 if getattr(track, "explicit", False) else 0,
        "type": getattr(track, "type", None),
        "state": getattr(track, "state", None),
        "available": 1 if getattr(track, "available", True) else 0,
        "track_source": getattr(track, "track_source", None),
        "raw": _dump(track),
    }


def _short_id(short: Any) -> str:
    track_id = getattr(short, "id", None)
    album_id = getattr(short, "album_id", None)
    if track_id is None:
        return ""
    text = str(track_id)
    if ":" in text:
        return text
    return f"{text}:{album_id or 0}"


def _album_id_of(short: Any) -> Any:
    text = _short_id(short)
    return text.split(":", 1)[1] if ":" in text else None


def resolve_tracks(
    account: YandexAccount,
    shorts: Iterable[Any],
    on_progress: ProgressFn = _silent,
) -> dict[str, dict[str, Any]]:
    """TrackShort → готовые строки треков, вложенные объекты берём как есть.

    У Яндекса в ответе плейлиста трек обычно вложен (``short.track``), а в
    лайках — нет: недостающее дочитываем пачками через ``Client.tracks``.
    """
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for short in shorts:
        ym_id = _short_id(short)
        if not ym_id or ym_id in rows:
            continue
        nested = getattr(short, "track", None)
        if nested is not None:
            row = track_row(nested, _album_id_of(short))
            if row:
                rows[row["ym_id"]] = row
                continue
        pending.append(ym_id)

    if pending:
        on_progress(f"дочитываю детали {len(pending)} треков…")
        known = set()
        for chunk_start in range(0, len(pending), TRACKS_CHUNK):
            chunk = pending[chunk_start : chunk_start + TRACKS_CHUNK]
            for track in account.tracks_by_ids(chunk):
                row = track_row(track)
                if row:
                    rows[row["ym_id"]] = row
                    known.add(str(getattr(track, "id", "")))
            done = chunk_start + len(chunk)
            if done % 2000 < TRACKS_CHUNK:
                on_progress(f"  детали: {done}/{len(pending)}")
        missing = [i for i in pending if i not in known and i not in rows]
        if missing:
            on_progress(f"не удалось получить детали {len(missing)} треков (удалены/недоступны)")
    return rows


# --- экспорт ------------------------------------------------------------


@dataclass
class ExportStats:
    tracks: int = 0
    playlists: int = 0
    skipped: int = 0
    likes: int = 0
    notes: list[str] = field(default_factory=list)


def _is_generated(playlist: Any) -> str:
    """Причина пропуска, если плейлист служебный («Моя волна», «Премьеры»…)."""
    kind = getattr(playlist, "kind", "") or ""
    if kind == "likes":
        return ""
    generated = getattr(playlist, "generated_playlist_type", None)
    ptype = (getattr(playlist, "type", None) or "").lower()
    if generated:
        return str(generated)
    if ptype and ptype != "playlist":
        return ptype
    return ""


def export_library(
    account: YandexAccount,
    storage: Database,
    *,
    include_generated: bool = False,
    on_progress: ProgressFn = _silent,
) -> ExportStats:
    """Выгружает пользовательские плейлисты и лайки в SQLite.

    Идемпотентно: треки апсернятся, плейлисты перезаписываются целиком, а
    детали лайков второй раз не запрашиваются, если ревизия не менялась.
    """
    stats = ExportStats()
    storage.set_meta("ym_uid", account.uid)
    storage.set_meta("ym_login", account.login)
    storage.set_meta("ym_exported_at", utcnow())
    on_progress(f"аккаунт {account.login} (uid {account.uid})")
    on_progress(f"в базе уже есть {len(storage.known_ym_track_ids())} треков")

    # 1. лайки — самая большая коллекция, для неё важно не перебирать зря
    likes_shorts, revision = account.likes()
    stored_revision = storage.get_meta_int("ym_likes_revision")
    ids = [_short_id(s) for s in likes_shorts if _short_id(s)]
    if ids:
        unchanged = revision and revision == stored_revision and storage.playlist_item_count("likes") == len(ids)
        if unchanged:
            on_progress(f"лайков: {len(ids)} — ревизия {revision} не менялась, детали не дочитываю")
            stats.notes.append(f"лайки не изменились (ревизия {revision})")
        else:
            shorts = list(likes_shorts)
            rows = resolve_tracks(account, shorts, on_progress)
            storage.upsert_ym_tracks(rows.values())
            stats.tracks += len(rows)
            on_progress(f"лайков: {len(ids)} (ревизия {revision})")
        storage.replace_playlist(
            kind="likes",
            title="Мне нравится",
            tracks=[(ym_id, pos) for pos, ym_id in enumerate(ids)],
            visibility="private",
            track_count=len(ids),
            generated_type="",
            owner_uid=str(account.uid),
        )
        storage.set_meta("ym_likes_revision", revision)
        stats.likes = len(ids)
    elif stored_revision:
        on_progress("Яндекс не вернул лайки — оставляю прошлый снимок")
        stats.notes.append("лайки не вернулись, использован кэш базы")
        stats.likes = storage.playlist_item_count("likes")

    # 2. плейлисты
    playlists = account.playlists()
    on_progress(f"плейлистов в аккаунте: {len(playlists)}")
    ordinal = 0
    for number, header in enumerate(playlists, start=1):
        kind = getattr(header, "kind", "") or ""
        title = getattr(header, "title", "") or kind
        if kind == "likes":
            continue
        reason = _is_generated(header)
        if reason and not include_generated:
            stats.skipped += 1
            on_progress(f"  пропуск ({reason}): {title}")
            continue
        try:
            playlist = account.playlist(kind)
        except YandexError as exc:
            stats.notes.append(f"{title}: {exc}")
            on_progress(f"  ошибка: {title} — {exc}")
            continue
        if playlist is None:
            stats.notes.append(f"{title}: Яндекс вернул пустой ответ")
            continue
        shorts = list(getattr(playlist, "tracks", None) or [])
        items = [_short_id(s) for s in shorts if _short_id(s)]
        rows = resolve_tracks(account, shorts, on_progress)
        storage.upsert_ym_tracks(rows.values())
        ordinal += 1
        storage.replace_playlist(
            kind=kind,
            title=title,
            tracks=[(ym_id, pos) for pos, ym_id in enumerate(items)],
            visibility=getattr(playlist, "visibility", None) or "private",
            created=str(getattr(playlist, "created", "") or "") or None,
            modified=str(getattr(playlist, "modified", "") or "") or None,
            track_count=int(getattr(playlist, "track_count", 0) or len(items)),
            generated_type="",
            description=str(getattr(playlist, "description", "") or "") or None,
            owner_uid=str(getattr(playlist, "uid", "") or account.uid),
            ordinal=ordinal,
            raw=_dump(playlist),
        )
        stats.playlists += 1
        stats.tracks += len(rows)
        on_progress(f"[{number}/{len(playlists)}] {title}: {len(items)} треков")

    storage.recount_refs()
    on_progress("пересчёт нормализованных ключей…")
    index = storage.reindex_tracks()
    on_progress(f"треков в базе: {index['ym_tracks']}")
    stats.tracks = index["ym_tracks"]
    storage.log_event("export", f"playlists={stats.playlists} likes={stats.likes} tracks={stats.tracks}")
    return stats


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
