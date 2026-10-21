"""Spotify Web API: PKCE-авторизация, троттлинг, типовые вызовы.

Официальный API, никаких кликов по сайту. Авторизация — authorization code
flow с PKCE: поднимаем локальный сервер на 127.0.0.1 из redirect_uri,
пользователь подтверждает доступ в браузере, мы забираем code и меняем его на
токены. Refresh-токен лежит в ``secrets/spotify.json``.

Отдельно выделен ``SpotifyQuota``: приложение в dev-mode режет квоты жёстче
документированных, поэтому ответы 429/403 разбираются явно, а прогресс
сохраняется в чекпоинты — перерыв на полчаса не должен стоить часов работы.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:  # системное хранилище сертификатов: спасает от MITM-перехвата провайдера
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - опциональная зависимость
    pass

from .config import SPOTIFY_TOKEN_CACHE, Config, read_token_cache, require_client_id, write_token_cache

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"

MAX_SAVE_PER_CALL = 50     # PUT /me/tracks принимает не больше 50 id
MAX_ADD_PER_CALL = 100     # POST /playlists/{id}/tracks — 100
MAX_IDS_PER_CALL = 50      # GET /tracks?ids=

ProgressFn = Callable[[str], None]


def _silent(text: str) -> None:  # pragma: no cover - заглушка коллбэка
    pass


class SpotifyError(RuntimeError):
    """Ошибка Spotify с человекочитаемым объяснением и кодом ответа."""

    def __init__(self, message: str, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504)


# --- PKCE ---------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))          # 64 символа — в пределах 43..128
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(cfg: Config, state: str) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    params = {
        "client_id": require_client_id(cfg),
        "response_type": "code",
        "redirect_uri": cfg.spotify.redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": " ".join(cfg.spotify.scopes),
        "state": state,
        "show_dialog": "true",
    }
    return f"{ACCOUNTS}/authorize?{urlencode(params)}", verifier


class _Callback(BaseHTTPRequestHandler):
    """Ловит редирект с кодом авторизации."""

    result: dict[str, str] = {}
    callback_path = "/callback"

    def do_GET(self) -> None:  # noqa: N802 - имя метода по стандарту
        parsed = urlparse(self.path)
        if parsed.path != type(self).callback_path:
            self.send_error(404, "not our callback")
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).result = query
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "error" in query:
            body = f"<h3>Не получилось: {html.escape(query['error'])}</h3><p>Закройте вкладку.</p>"
        else:
            body = "<h3>Готово</h3><p>Можно закрыть эту вкладку и вернуться в терминал.</p>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args: Any) -> None:  # молчим в консоли
        pass


def _wait_for_code(cfg: Config, timeout: float = 300.0) -> dict[str, str]:
    parsed = urlparse(cfg.spotify.redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8899
    _Callback.callback_path = parsed.path or "/callback"
    _Callback.result = {}
    try:
        server = HTTPServer((host, port), _Callback)
    except OSError as exc:
        raise SpotifyError(
            f"не удалось слушать {host}:{port} ({exc}). Освободите порт или "
            "измените SPOTIFY_REDIRECT_URI и пересоздайте приложение."
        ) from exc
    server.timeout = 1.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not _Callback.result:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    if not _Callback.result:
        raise SpotifyError(
            "кода авторизации не дождались. Проверьте, что браузер открылся на том же "
            "компьютере, и повторите команду."
        )
    return _Callback.result


def _proxies(cfg: Config | None = None) -> dict[str, str] | None:
    """Прокси до Spotify: из config.toml ([spotify] proxy) или SPOTIFY_PROXY.

    Актуально, пока Spotify отвечает 403 «unavailable in this country» на
    запросы с RU-адресов: трафик программы можно пустить в тот же туннель,
    которым вы пользуетесь для прослушивания (http, https или socks5).
    """
    proxy = ""
    if cfg is not None:
        proxy = (cfg.spotify.proxy or "").strip()
    proxy = proxy or os.environ.get("SPOTIFY_PROXY", "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def _exchange(cfg: Config, code: str, verifier: str) -> dict[str, Any]:
    response = requests.post(
        f"{ACCOUNTS}/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.spotify.redirect_uri,
            "client_id": require_client_id(cfg),
            "code_verifier": verifier,
        },
        timeout=30,
        proxies=_proxies(cfg),
    )
    if response.status_code != 200:
        raise SpotifyError(
            f"Spotify не обменял код на токен ({response.status_code}): {response.text[:300]}",
            response.status_code,
        )
    return response.json()


@dataclass
class TokenBundle:
    """Кэш токенов Spotify (secrets/spotify.json)."""

    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    scope: str = ""
    client_id: str = ""
    user_id: str = ""

    def needs_refresh(self, skew: int = 120) -> bool:
        return self.expires_at - skew <= int(time.time())

    @classmethod
    def from_response(cls, data: dict[str, Any], client_id: str) -> "TokenBundle":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", "") or "",
            expires_at=int(time.time()) + int(data.get("expires_in") or 3600),
            scope=data.get("scope", "") or "",
            client_id=client_id,
        )

    @classmethod
    def load(cls) -> "TokenBundle | None":
        payload = read_token_cache(SPOTIFY_TOKEN_CACHE)
        if not payload or not payload.get("access_token"):
            return None
        return cls(**{k: payload.get(k, v) for k, v in payload.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        write_token_cache(SPOTIFY_TOKEN_CACHE, self.__dict__)


def refresh_tokens(bundle: TokenBundle, client_id: str, cfg: Config | None = None) -> TokenBundle:
    if not bundle.refresh_token:
        raise SpotifyError("refresh-токена нет — нужна повторная авторизация", 400)
    response = requests.post(
        f"{ACCOUNTS}/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
            "client_id": client_id,
        },
        timeout=30,
        proxies=_proxies(cfg),
    )
    if response.status_code != 200:
        raise SpotifyError(
            f"Spotify отказал в обновлении токена ({response.status_code}): {response.text[:200]}",
            response.status_code,
        )
    fresh = TokenBundle.from_response(response.json(), client_id)
    fresh.refresh_token = fresh.refresh_token or bundle.refresh_token
    fresh.user_id = bundle.user_id
    return fresh


def login(cfg: Config, on_status: ProgressFn = _silent, manual: bool = False) -> TokenBundle:
    """Открывает браузер и ждёт подтверждения доступа."""
    client_id = require_client_id(cfg)
    state = secrets.token_urlsafe(16)
    url, verifier = authorize_url(cfg, state)
    on_status(f"Открываю браузер для подтверждения доступа к Spotify:\n  {url}")
    if not manual and not os.environ.get("SB_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - браузер может быть недоступен
            on_status("Браузер сам не открылся — перейдите по ссылке выше вручную.")
    else:
        on_status("Откройте ссылку выше и после подтверждения скопируйте сюда адрес из строки браузера.")

    if manual:
        code = _ask_manual_code(url, cfg, state)
    else:
        result = _wait_for_code(cfg)
        if "error" in result:
            raise SpotifyError(f"Spotify вернул ошибку: {result.get('error')} — {result.get('error_description', '')}")
        if result.get("state") != state:
            raise SpotifyError("state в колбэке не совпал — похожа на CSRF/чужую вкладку, начинаем заново")
        code = result.get("code", "")
    if not code:
        raise SpotifyError("не получили code в колбэке")

    bundle = TokenBundle.from_response(_exchange(cfg, code, verifier), client_id)
    bundle.save()
    return bundle


def _ask_manual_code(url: str, cfg: Config, state: str) -> str:
    """Запасной путь: пользователь вставляет адрес редиректа сам."""
    parsed = urlparse(cfg.spotify.redirect_uri)
    marker = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/callback'}"
    try:
        raw = input(f"\nВставьте полный адрес, на который Spotify вас перенаправил ({marker}/?...code=...):\n> ").strip()
    except EOFError as exc:  # pragma: no cover - интерактив
        raise SpotifyError("не удалось прочитать код вручную") from exc
    if not raw:
        raise SpotifyError("пустой ввод")
    query = {k: v[0] for k, v in parse_qs(urlparse(raw).query).items()}
    if query.get("state") != state:
        raise SpotifyError("state не совпал — вставляйте адрес из того же прогона")
    return query.get("code", "")


# --- governor -----------------------------------------------------------


@dataclass
class Quota:
    """Счётчики запросов для отчёта."""

    requests: int = 0
    retries: int = 0
    waited_seconds: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)

    def note_error(self, key: str) -> None:
        self.errors[key] = self.errors.get(key, 0) + 1


class TokenBucket:
    """Простой token bucket: скорость request_interval_ms с всплесками до burst."""

    def __init__(self, rate_per_second: float, capacity: int) -> None:
        self.rate = max(0.5, rate_per_second)
        self.capacity = max(1, capacity)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> float:
        """Блокируется, пока не появится разрешение. Возвращает время ожидания."""
        waited = 0.0
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return waited
                need = (1 - self.tokens) / self.rate
                time.sleep(need)
                waited += need


# --- клиент -------------------------------------------------------------


class SpotifyAccount:
    """Клиент Spotify Web API с авторизацией, троттлингом и повторами."""

    def __init__(self, cfg: Config, bucket: TokenBucket, bundle: TokenBundle) -> None:
        self.cfg = cfg
        self.bucket = bucket
        self.bundle = bundle
        self.quota = Quota()
        self.http = requests.Session()
        proxies = _proxies(cfg)
        if proxies:
            self.http.proxies.update(proxies)
        self.user_id = bundle.user_id
        self.product = ""

    # -- подключение -------------------------------------------------------
    @classmethod
    def connect(
        cls,
        cfg: Config,
        *,
        force_login: bool = False,
        manual: bool = False,
        on_status: ProgressFn = _silent,
        quiet: bool = False,
    ) -> "SpotifyAccount":
        client_id = require_client_id(cfg)
        bundle = None if force_login else TokenBundle.load()
        if bundle and bundle.client_id and bundle.client_id != client_id:
            if not quiet:
                on_status("Client ID сменился — забыли старый токен Spotify.")
            bundle = None
        if bundle and bundle.needs_refresh():
            try:
                bundle = refresh_tokens(bundle, client_id)
                bundle.save()
                if not quiet:
                    on_status("Токен Spotify обновлён.")
            except SpotifyError as exc:
                if not quiet:
                    on_status(f"{exc} — запрашиваю доступ заново.")
                bundle = None
        if bundle is None:
            bundle = login(cfg, on_status if not quiet else _silent, manual=manual)
            if not quiet:
                on_status("Доступ к Spotify получен.")

        throttle = cfg.throttle
        bucket = TokenBucket(1000.0 / max(1, throttle.request_interval_ms), throttle.burst)
        account = cls(cfg, bucket, bundle)
        account._verify()
        return account

    def _verify(self) -> None:
        me = self.get("/me")
        self.user_id = me.get("id") or self.bundle.user_id
        self.bundle.user_id = self.user_id
        self.bundle.scope = me.get("scope") or self.bundle.scope
        self.bundle.save()
        self.product = (me.get("product") or "").lower()
        if self.product and self.product != "premium":
            raise SpotifyError(
                f"у аккаунта {self.user_id} тип подписки '{self.product}'. "
                "Запись в медиатеку и создание плейлистов доступны только Premium."
            )

    # -- низкоуровнево -----------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bundle.access_token}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        absolute: bool = False,
    ) -> Any:
        url = path if absolute else f"{API}{path}"
        attempts = max(1, self.cfg.throttle.max_retries)
        refreshed = False
        for attempt in range(1, attempts + 1):
            self.quota.requests += 1
            self.quota.waited_seconds += self.bucket.take()
            try:
                response = self.http.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=self._auth_headers(),
                    timeout=(10, 60),
                )
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise SpotifyError(f"нет связи со Spotify: {exc}") from exc
                self._sleep_backoff(attempt, 0)
                continue

            if response.status_code in (200, 201):
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise SpotifyError(f"Spotify вернул не JSON: {response.text[:200]}") from exc
            if response.status_code == 204:
                return {}

            body = response.text[:300]
            reason = ""
            try:
                reason = (response.json().get("error") or {}).get("message", "") or ""
            except ValueError:
                pass
            detail = reason or body

            if response.status_code == 401 and not refreshed:
                refreshed = True
                self.bundle = refresh_tokens(self.bundle, self.bundle.client_id or require_client_id(self.cfg))
                self.bundle.save()
                continue
            if response.status_code == 429:
                self.quota.retries += 1
                self.quota.note_error("429")
                retry_after = float(response.headers.get("Retry-After") or 1)
                if attempt == attempts:
                    raise SpotifyError(
                        f"Spotify троттлит слишком жёстко ({detail}). Увеличьте "
                        "throttle.request_interval_ms в config.toml и продолжите — прогресс в чекпоинтах.",
                        429,
                        detail,
                    )
                self._sleep_backoff(attempt, retry_after)
                continue
            if response.status_code in (500, 502, 503, 504):
                self.quota.retries += 1
                self.quota.note_error(str(response.status_code))
                if attempt == attempts:
                    raise SpotifyError(f"Spotify недоступен ({response.status_code}): {detail}",
                                       response.status_code, detail)
                self._sleep_backoff(attempt, 0)
                continue
            if response.status_code == 403:
                raise SpotifyError(
                    f"Spotify запретил запрос ({detail}). Обычно это значит, что режим "
                    "Development Mode не выдал нужную квоту либо не хватает скоупов.",
                    403,
                    detail,
                )
            raise SpotifyError(f"{method} {path} → {response.status_code}: {detail}",
                               response.status_code, detail)
        raise SpotifyError(f"{method} {path}: исчерпаны попытки")

    def _sleep_backoff(self, attempt: int, retry_after: float) -> None:
        cfg = self.cfg.throttle
        base = (cfg.backoff_base_ms / 1000.0) * (2 ** (attempt - 1))
        delay = max(retry_after, base)
        delay = min(delay, cfg.backoff_cap_ms / 1000.0)
        time.sleep(delay)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, payload: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, params=params, payload=payload)

    def put(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, payload=payload)

    def delete(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, payload=payload)

    # -- постраничная выдача ------------------------------------------------
    def paged(self, path: str, params: dict[str, Any] | None = None, key: str = "items",
              limit: int | None = None) -> Iterator[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("limit", 50)
        url: str = path if path.startswith("http") else f"{API}{path}"
        first = True
        got = 0
        while url:
            # у next-ссылки Spotify параметры уже внутри, повторный params только сломал бы offset
            page = self.request("GET", url, params=query if first else None, absolute=True)
            first = False
            items = page.get(key) or []
            for item in items:
                yield item
                got += 1
                if limit and got >= limit:
                    return
            nxt = page.get("next")
            if not items or not nxt:
                return
            url = nxt

    # -- чтение -------------------------------------------------------------
    def me(self) -> dict[str, Any]:
        return self.get("/me")

    def saved_tracks(self, limit: int | None = None, on_progress: ProgressFn = _silent) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self.paged("/me/tracks", {"additional_types": "track"}, limit=limit):
            track = item.get("track") or {}
            if track:
                track["_added_at"] = (item.get("added_at") or "")
                out.append(track)
            if len(out) and len(out) % 500 == 0:
                on_progress(f"  лайков Spotify прочитано: {len(out)}")
        return out

    def playlists(self) -> list[dict[str, Any]]:
        return list(self.paged("/me/playlists", {"limit": 50}))

    def playlist_tracks(self, playlist_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self.paged(f"/playlists/{playlist_id}/tracks", {"limit": 100}, limit=limit):
            track = item.get("track") or {}
            if track and track.get("type") != "episode":
                track["_added_at"] = (item.get("added_at") or "")
                out.append(track)
        return out

    def search_track(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            result = self.get("/search", {"q": query, "type": "track", "limit": limit, "market": "from_token"})
        except SpotifyError as exc:
            if exc.status == 429:
                raise
            self.quota.note_error(f"search:{exc.status}")
            return []
        return (result.get("tracks") or {}).get("items") or []

    def tracks(self, ids: Iterable[str]) -> list[dict[str, Any]]:
        """Пакетный GET /tracks?ids= — недоступен в dev-mode, поэтому с фолбэком."""
        wanted = [i for i in ids if i]
        out: list[dict[str, Any]] = []
        for start in range(0, len(wanted), MAX_IDS_PER_CALL):
            chunk = wanted[start : start + MAX_IDS_PER_CALL]
            try:
                result = self.get("/tracks", {"ids": ",".join(chunk)})
            except SpotifyError as exc:
                self.quota.note_error(f"tracks_batch:{exc.status}")
                for single in chunk:
                    try:
                        out.append(self.get(f"/tracks/{single}"))
                    except SpotifyError:
                        continue
                continue
            out.extend([t for t in (result.get("tracks") or []) if t])
        return out

    # -- запись -------------------------------------------------------------
    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict[str, Any]:
        return self.post(f"/users/{self.user_id}/playlists",
                         {"name": name, "description": description, "public": bool(public)})

    def add_tracks(self, playlist_id: str, uris: Iterable[str]) -> dict[str, Any]:
        batch = list(uris)
        return self.post(f"/playlists/{playlist_id}/tracks", {"uris": batch})

    def add_tracks_in_batches(self, playlist_id: str, uris: Sequence[str],
                             on_progress: ProgressFn = _silent) -> int:
        added = 0
        size = min(MAX_ADD_PER_CALL, self.cfg.limits.batch_add)
        for start in range(0, len(uris), size):
            chunk = uris[start : start + size]
            self.add_tracks(playlist_id, chunk)
            added += len(chunk)
            on_progress(f"    добавлено {added}/{len(uris)}")
        return added

    def save_tracks(self, uris: Iterable[str]) -> int:
        batch = [u for u in uris if u]
        if not batch:
            return 0
        self.put("/me/tracks", {"ids": batch})
        return len(batch)

    def save_tracks_in_batches(self, uris: Sequence[str], on_progress: ProgressFn = _silent) -> int:
        saved = 0
        for start in range(0, len(uris), MAX_SAVE_PER_CALL):
            chunk = uris[start : start + MAX_SAVE_PER_CALL]
            saved += self.save_tracks(chunk)
            on_progress(f"    в медиатеку: {saved}/{len(uris)}")
        return saved


# --- разбор ответов ------------------------------------------------------


def track_to_row(track: dict[str, Any]) -> dict[str, Any] | None:
    """JSON Spotify → строка ``sp_track``."""
    uri = track.get("uri") or (f"spotify:track:{track['id']}" if track.get("id") else "")
    if not uri:
        return None
    album = track.get("album") or {}
    external = album.get("external_ids") or {}
    isrc = (track.get("isrc") or external.get("isrc") or "").strip().upper() or None
    return {
        "uri": uri,
        "id": track.get("id"),
        "name": track.get("name") or "",
        "artists": [(a.get("name") or "") for a in (track.get("artists") or []) if a.get("name")],
        "album": album.get("name"),
        "isrc": isrc,
        "duration_ms": int(track.get("duration_ms") or 0),
        "explicit": 1 if track.get("explicit") else 0,
        "raw": {k: track.get(k) for k in ("id", "uri", "name", "duration_ms", "explicit", "isrc", "preview_url")},
    }


def uri_id(uri: str) -> str:
    return uri.rsplit(":", 1)[-1] if uri else ""


def to_uri(track_id_or_uri: str) -> str:
    """Приводит аргумент к spotify-URI трека.

    Принимает: ``spotify:track:ID``, голый ``ID`` или ссылку вида
    ``https://open.spotify.com/track/ID?si=...`` (как её отдаёт веб-плеер).
    """
    value = (track_id_or_uri or "").strip()
    if not value:
        return ""
    if value.startswith("spotify:"):
        return value
    if "open.spotify.com" in value or value.startswith("http"):
        parts = [p for p in urlparse(value).path.split("/") if p]
        if len(parts) >= 2 and parts[-2] in {"track", "episode", "album"}:
            return f"spotify:{parts[-2]}:{parts[-1]}"
        if parts:
            return f"spotify:track:{parts[-1]}"
        return ""
    return f"spotify:track:{value}"
