"""Загрузка конфигурации: ``config.toml`` (настройки) + ``.env`` (секреты).

Секреты в git не попадают: токены лежат в ``secrets/*.json``, клиентские
идентификаторы — в ``.env``.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("SOUNDBRIDGE_HOME", Path.cwd())).resolve()

CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"
SECRETS_DIR = ROOT / "secrets"
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"

DB_PATH = DATA_DIR / "soundbridge.db"

SPOTIFY_TOKEN_CACHE = SECRETS_DIR / "spotify.json"
YANDEX_TOKEN_CACHE = SECRETS_DIR / "yandex.json"


@dataclass
class YandexCfg:
    device_poll_seconds: int = 5
    request_delay_ms: int = 250


@dataclass
class SpotifyCfg:
    client_id: str = ""
    redirect_uri: str = "http://127.0.0.1:8899/callback"
    scopes: list[str] = field(default_factory=lambda: [
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
        "playlist-modify-public",
        "user-library-read",
        "user-library-modify",
        "user-read-email",
        "user-private",
    ])


@dataclass
class LimitsCfg:
    """Жёсткие лимиты Spotify API (не настраиваются «под себя»)."""

    playlist_max_tracks: int = 10_000
    library_max_tracks: int = 10_000
    batch_write: int = 100
    batch_add: int = 100


@dataclass
class MatchCfg:
    auto_score: float = 0.90
    review_score: float = 0.72
    duration_tolerance_ms: int = 2_500
    duration_hard_ms: int = 12_000
    weight_isrc: float = 1.0
    weight_title: float = 0.45
    weight_artist: float = 0.35
    weight_duration: float = 0.20
    version_penalty: float = 0.08
    search_limit: int = 10
    version_tags: list[str] = field(default_factory=lambda: [
        "remaster",
        "remastered",
        "deluxe",
        "bonus track",
        "mono",
        "stereo",
        "single version",
        "album version",
        "radio edit",
        "live version",
    ])


@dataclass
class ThrottleCfg:
    request_interval_ms: int = 220
    burst: int = 8
    max_retries: int = 6
    backoff_base_ms: int = 1_000
    backoff_cap_ms: int = 60_000
    checkpoint_every: int = 200


@dataclass
class ImportCfg:
    dry_run: bool = True
    create_playlists: bool = True
    import_likes: bool = True
    skip_playlists: list[str] = field(default_factory=list)
    only_playlists: list[str] = field(default_factory=list)
    prefix: str = ""
    max_playlist_tracks: int = 10_000
    likes_playlist: bool = True
    likes_playlist_title: str = "Мне нравится (перенос из Яндекса)"


@dataclass
class Config:
    yandex: YandexCfg = field(default_factory=YandexCfg)
    spotify: SpotifyCfg = field(default_factory=SpotifyCfg)
    limits: LimitsCfg = field(default_factory=LimitsCfg)
    match: MatchCfg = field(default_factory=MatchCfg)
    throttle: ThrottleCfg = field(default_factory=ThrottleCfg)
    import_: ImportCfg = field(default_factory=ImportCfg)

    @property
    def playlist_max(self) -> int:
        return min(self.limits.playlist_max_tracks, self.import_.max_playlist_tracks)


def _fill(instance: Any, data: dict[str, Any]) -> Any:
    """Копирует известные поля из словаря в dataclass, игнорируя лишние."""
    known = {f.name for f in fields(instance)}
    for key, value in data.items():
        if key not in known:
            continue
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            _fill(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_env(path: Path | None = None) -> None:
    env_path = path or ENV_PATH
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except ImportError:  # pragma: no cover
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: Path | None = None) -> Config:
    """Читает config.toml (если есть) и наложения из переменных окружения."""
    load_env()
    cfg = Config()
    toml_path = path or CONFIG_PATH
    if toml_path.exists():
        with toml_path.open("rb") as fh:
            _fill(cfg, tomllib.load(fh))

    sp = cfg.spotify
    sp.client_id = os.getenv("SPOTIFY_CLIENT_ID", sp.client_id)
    sp.redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", sp.redirect_uri)
    if os.getenv("SPOTIFY_SCOPES"):
        sp.scopes = [s.strip() for s in os.environ["SPOTIFY_SCOPES"].split(",") if s.strip()]

    if os.getenv("SB_IMPORT_DRY_RUN"):
        cfg.import_.dry_run = os.environ["SB_IMPORT_DRY_RUN"].strip().lower() not in {
            "0",
            "false",
            "no",
        }
    return cfg


def require_client_id(cfg: Config) -> str:
    if not cfg.spotify.client_id:
        raise SystemExit(
            "Не задан Spotify Client ID.\n"
            "  1) Создайте приложение: https://developer.spotify.com/dashboard\n"
            "  2) Redirect URI: http://127.0.0.1:8899/callback\n"
            "  3) Скопируйте .env.example в .env и впишите SPOTIFY_CLIENT_ID"
        )
    return cfg.spotify.client_id


def read_token_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_token_cache(path: Path, payload: dict[str, Any]) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - на Windows права наследуются
        pass


def ensure_dirs() -> None:
    for d in (SECRETS_DIR, DATA_DIR, OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
