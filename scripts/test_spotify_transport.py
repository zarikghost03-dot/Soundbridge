"""Офлайн-проверка транспорта Spotify: пагинация, 429, рефреш, батчи записи.

Реальной сети здесь нет: подменяем ``Session.request`` фиктивной доставкой.
Смысл именно в том, что эти ветки в живом прогоне на 5000+ треков всплывают
гарантированно, а стоят дорогих прерываний.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from soundbridge.config import load_config  # noqa: E402
from soundbridge.spotify_client import (  # noqa: E402
    MAX_ADD_PER_CALL,
    MAX_SAVE_PER_CALL,
    SpotifyAccount,
    SpotifyError,
    TokenBucket,
    TokenBundle,
    track_to_row,
)


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, headers: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text else ("" if payload is None else str(payload))
        self.content = b"" if payload is None and not text else b"x"

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Записывает вызовы и отвечает по заранее заготовленному сценарию."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json,
                           "headers": headers})
        if not self.script:
            raise AssertionError(f"лишний запрос: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_account(script, *, interval_ms: int = 1) -> SpotifyAccount:
    cfg = load_config()
    cfg.throttle.request_interval_ms = interval_ms
    cfg.throttle.burst = 100
    cfg.throttle.backoff_base_ms = 1
    cfg.throttle.backoff_cap_ms = 5
    bundle = TokenBundle(access_token="old", refresh_token="r", expires_at=int(time.time()) + 3600,
                         client_id="cid", user_id="u123")
    account = SpotifyAccount(cfg, TokenBucket(10_000, 100), bundle)
    account.http = FakeSession(script)  # type: ignore[assignment]
    return account


def page(items: list[str], nxt: str | None) -> dict:
    return {"items": [{"id": i, "uri": f"spotify:track:{i}"} for i in items], "next": nxt}


def test_paged() -> None:
    account = make_account([
        FakeResponse(200, page(["a", "b"], "https://api.spotify.com/v1/me/tracks?offset=2&limit=2")),
        FakeResponse(200, page(["c"], None)),
    ])
    got = list(account.paged("/me/tracks", {"limit": 2}))
    assert [g["id"] for g in got] == ["a", "b", "c"], got
    first = account.http.calls[0]
    assert first["url"].endswith("/me/tracks") and first["params"] == {"limit": 2}, first
    # вторая страница запрашивается по absolute-URL без повторных params
    second = account.http.calls[1]
    assert second["url"].startswith("https://") and second["params"] is None, second
    print("OK пагинация: 3 элемента за 2 запроса, next берётся как есть")


def test_paged_limit() -> None:
    account = make_account([FakeResponse(200, page(["a", "b", "c", "d"], "https://x/next"))])
    got = list(account.paged("/me/tracks", limit=3))
    assert len(got) == 3, got
    assert len(account.http.calls) == 1
    print("OK лимит пагинации: остановились, не читая лишнего")


def test_retry_after() -> None:
    account = make_account([
        FakeResponse(429, None, headers={"Retry-After": "0"}, text="{}"),
        FakeResponse(503, None, text="busy"),
        FakeResponse(200, {"id": "u123"}),
    ])
    started = time.monotonic()
    assert account.get("/me") == {"id": "u123"}
    assert account.quota.retries == 2, account.quota.retries
    assert account.quota.errors == {"429": 1, "503": 1}, account.quota.errors
    assert account.quota.requests == 3
    assert time.monotonic() - started < 2
    print("OK 429/503: два повтора с backoff, квота учтена")


def test_retry_exhausted() -> None:
    account = make_account([FakeResponse(429, None, headers={"Retry-After": "0"}, text="{}")] * 6)
    account.cfg.throttle.max_retries = 3
    try:
        account.get("/me")
    except SpotifyError as exc:
        assert exc.status == 429 and "request_interval_ms" in str(exc), exc
        print("OK 429 до исчерпания попыток → понятная подсказка про throttle")
        return
    raise AssertionError("ожидали SpotifyError")


def test_refresh_on_401() -> None:
    account = make_account([
        FakeResponse(401, {"error": {"message": "The access token expired"}}),
        FakeResponse(200, {"ok": True}),
    ])

    calls: list[str] = []
    import soundbridge.spotify_client as sp

    def fake_refresh(bundle: TokenBundle, client_id: str) -> TokenBundle:
        calls.append(client_id)
        return TokenBundle(access_token="new", refresh_token=bundle.refresh_token,
                           expires_at=int(time.time()) + 3600, client_id=client_id,
                           user_id=bundle.user_id)

    original = sp.refresh_tokens
    sp.refresh_tokens = fake_refresh  # type: ignore[assignment]
    try:
        assert account.get("/me/tracks") == {"ok": True}
    finally:
        sp.refresh_tokens = original  # type: ignore[assignment]
    assert calls == ["cid"], calls
    assert account.bundle.access_token == "new"
    sent = account.http.calls[-1]["headers"]["Authorization"]
    assert sent == "Bearer new", sent
    print("OK 401: один рефреш, повторный запрос уже со свежим токеном")


def test_forbidden_explains() -> None:
    account = make_account([FakeResponse(403, {"error": {"message": "app locked"}}, text="app locked")])
    try:
        account.post("/users/u123/playlists", {"name": "x"})
    except SpotifyError as exc:
        assert exc.status == 403 and "Development Mode" in str(exc), exc
        print("OK 403 объясняется как проблема квоты dev-mode, а не молча тонет")
        return
    raise AssertionError("ожидали SpotifyError")


def test_write_batches() -> None:
    uris = [f"spotify:track:{i}" for i in range(120)]
    account = make_account([
        FakeResponse(201, {}), FakeResponse(201, {}),
        FakeResponse(200, {}), FakeResponse(200, {}), FakeResponse(200, {}),
    ])
    added = account.add_tracks_in_batches("pl1", uris)
    saved = account.save_tracks_in_batches(uris)
    assert added == 120 and saved == 120
    add_sizes = [len(c["json"]["uris"]) for c in account.http.calls[:2]]
    save_sizes = [len(c["json"]["ids"]) for c in account.http.calls[2:]]
    assert add_sizes == [MAX_ADD_PER_CALL, 20], add_sizes
    assert save_sizes == [MAX_SAVE_PER_CALL, MAX_SAVE_PER_CALL, 20], save_sizes
    assert all(c["method"] in ("POST", "PUT") for c in account.http.calls)
    print(f"OK батчи записи: add {add_sizes} (лимит {MAX_ADD_PER_CALL}), save {save_sizes} (лимит {MAX_SAVE_PER_CALL})")


def test_row_parsing() -> None:
    row = track_to_row({
        "id": "abc", "uri": "spotify:track:abc", "name": "Группа крови (Remastered)",
        "duration_ms": 287000, "explicit": False,
        "artists": [{"name": "Кино"}, {"name": ""}],
        "album": {"name": "Ночная серия", "external_ids": {"isrc": "rut1234567"}},
    })
    assert row and row["uri"] == "spotify:track:abc"
    assert row["isrc"] == "RUT1234567", row["isrc"]
    assert row["artists"] == ["Кино"], row["artists"]
    assert row["duration_ms"] == 287000 and row["explicit"] == 0
    assert track_to_row({"name": "без uri и id"}) is None
    # у episode-подобного ответа uri есть, но id нет — берём uri
    only_uri = track_to_row({"uri": "spotify:track:xyz", "name": "x", "artists": []})
    assert only_uri and only_uri["id"] is None and only_uri["artists"] == []
    print("OK разбор трека: ISRC из album.external_ids и в верхний регистр, пустые артисты отброшены")


def main() -> int:
    for test in (test_paged, test_paged_limit, test_retry_after, test_retry_exhausted,
                 test_refresh_on_401, test_forbidden_explains, test_write_batches, test_row_parsing):
        test()
    print("\nВсе проверки транспорта Spotify пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
