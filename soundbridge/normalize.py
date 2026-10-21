"""Нормализация названий и артистов для сопоставления Яндекс ⇄ Spotify.

Задача: из ``iqosphere — Формейшн (Formation) [Official Video]`` и
``IQOSPHERE - Formation`` получить один и тот же ключ.

Приёмы:
- транслитерация кириллицы (Spotify для RU-релизов часто отдаёт латиницу);
-variants из скобок: ``(Formation)`` → отдельный вариант названия;
- вынос фита (``feat. ...``) из названия в артистов;
- зачистка издательского мусора (``2011 Remaster``, ``Deluxe Edition``);
- приведение к «голой» буквенно-цифровой форме без диакритики.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

# --- транслитерация -------------------------------------------------------

CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # украinking/белорусские
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}

# Частотные «неполные» соответствия: y/и иногда путаются и т.п.
TRANSLIT_VARIANTS = {
    "zh": {"j", "g"},
    "sch": {"shch", "sc", "shch"},
    "yo": "o",
    "yu": "u",
    "ya": "a",
}

# --- мусорные маркеры -----------------------------------------------------

NOISE_PHRASES = [
    "official video", "official audio", "official music video", "official lyric video",
    "official", "lyric video", "lyrics", "lyric", "visualizer", "audio", "video",
    "hq", "hd", "4k", "full hd",
    "remaster", "remastered", "remastering", "deluxe", "deluxe edition", "deluxe version",
    "bonus track", "bonus video", "hidden track", "alternative take", "alternative version",
    "alternative", "single version", "album version", "album edit", "album", "single",
    "radio edit", "radio version", "extended mix", "extended version", "extended",
    "club mix", "original mix", "original radio edit", "original", "mix", "edit", "version",
    "mono", "stereo", "remix", "remixes", "vip", "bootleg", "mashup", "mash up",
    "live", "live version", "live take", "live performance", "concert", "unplugged",
    "acoustic", "acoustic version", "acoustic mix", "instrumental", "inst",
    "karaoke", "a cappella", "acapella", "reprise", "interlude", "intro", "outro",
    "part", "mastered", "explicit", "clean", "explicit content", "clean version",
    "explicit version", "cover", "cover song", "demo", "rough mix", "remake",
    "slowed", "reverb", "sped up", "nightcore", "daycore", "speed up", "slowed",
    "preview", "snippet", "teaser", "outtake", "unreleased", "remaster edition",
    "24 bit", "16 bit", "atmos", "spatial audio", "with lyrics", "ost",
    "soundtrack", "theme", "main title", "end credits",
]

FEAT_MARKERS = [
    "feat.", "feat", "ft.", "ft", "featuring", "feature", "with", "w/", "w.",
    "vs.", "vs", "совместно с", "при участии",
]

FEAT_SPLIT = re.compile(r",| & | and | feat\.| ft\.| featuring | и | с ", flags=re.I)

_PAREN = re.compile(r"[\(\[\{\u2039\u203a\u00ab\u00bb\u201c\u201d\u2018\u2019\"']([^\(\)\[\]]*)[\)\]\}]")
_DASH_TAIL = re.compile(r"\s[-–—_~|:]+\s*[^\-–—_~|:]+$")
_MULTI_SPACES = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_SCRIPT_SUFFIX = re.compile(r"^\d+\s*[.)]?\s*")


def transliterate(text: str) -> str:
    out = []
    for ch in text.lower():
        out.append(CYRILLIC_TO_LATIN.get(ch, ch))
    return "".join(out)


def has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _squash(text: str) -> str:
    return _MULTI_SPACES.sub(" ", text).strip()


def norm(text: str) -> str:
    """Канонический ключ: нижний регистр, без диакритики, только [a-z0-9] и пробелы."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text).lower()
    value = transliterate(value)
    value = strip_accents(value)
    value = value.replace("&", " and ").replace("+", " and ")
    value = _SCRIPT_SUFFIX.sub("", value)
    value = _NON_ALNUM.sub(" ", value)
    return _squash(value)


def _is_noise(chunk: str) -> bool:
    key = norm(chunk)
    if not key:
        return True
    tokens = key.split()
    if any(tok in NOISE_PHRASES for tok in tokens):
        return True
    for phrase in NOISE_PHRASES:
        if " " in phrase and f" {phrase} " in f" {key} ":
            return True
    if re.fullmatch(r"(19|20)\d{2}([ /-](19|20)\d{2})?", key):
        return True  # «(2011)», «(2011 / 2015)» — год переиздания
    return False


def contains_version_tag(text: str, tags: Iterable[str] = ()) -> bool:
    """Есть ли в названии издательская метка (remaster, deluxe, live…)."""
    key = norm(text)
    if not key:
        return False
    checked = set(NOISE_PHRASES) | {norm(t) for t in tags if t}
    tokens = set(key.split())
    if tokens & checked:
        return True
    return any(" " in phrase and f" {phrase} " in f" {key} " for phrase in checked if phrase)


def _clean_feat_chunks(tail: str) -> list[str]:
    """``feat. Basta, Guf`` → ``['Basta', 'Guf']`` (маркеры вырезаются)."""
    out: list[str] = []
    text = tail
    for marker in sorted(FEAT_MARKERS, key=len, reverse=True):
        text = re.sub(rf"(?i)(?<![a-z0-9])\W{re.escape(marker)}\.?", " ", f" {text} ")
    for chunk in FEAT_SPLIT.split(text):
        chunk = _MULTI_SPACES.sub(" ", _PAREN.sub(" ", chunk).strip(" .,-–—~|:"))
        if not chunk:
            continue
        key = norm(chunk)
        if len(key) <= 1 or all(tok in NOISE_PHRASES for tok in key.split()):
            continue
        if chunk not in out:
            out.append(chunk)
    return out


def split_features(artist: str) -> tuple[str, list[str]]:
    """Основной артист + фиты. ``A feat. B & C`` → ``('A', ['B', 'C'])``.

    Составные имена («Король и Шут») не режем: маркером считается только
    явный feat/ft/participation.
    """
    if not artist:
        return "", []
    text = _squash(artist)
    lowered = f" {text.lower()} "
    cut = len(text)
    for marker in (" feat. ", " feat ", " ft. ", " ft ", " featuring ", " при участии ", " совместно с ", " w/ ", " vs. "):
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    main_part, tail = text[:cut].strip(), text[cut:].strip(" ,;-–—")
    return (main_part or text), _clean_feat_chunks(tail)


def _translation_like(core: str, alt: str) -> bool:
    """Скобочный текст — это перевод названия или метка издания?

    «Формейшн (Formation)» → да (разные письменности),
    «Лесник (Live)» → нет (метка, к тому же в стоп-списке).
    """
    if not alt or alt.lower() == core.lower():
        return False
    if _is_noise(alt):
        return False
    key = norm(alt)
    if len(key) < 3 and " " not in key:
        return False
    if has_cyrillic(core) != has_cyrillic(alt):
        return True
    return not any(tok in NOISE_PHRASES for tok in key.split())


def title_variants(title: str, artist: str = "") -> tuple[str, list[str], list[str]]:
    """Разбирает название.

    Возвращает ``(основа, варианты-переводы, найденные_в_названии_фиты)``.

    ``iqosphere — Формейшн (Formation) [Official Video]`` →
    основа ``Формейшн``, варианты ``['Formation']``.
    """
    if not title:
        return "", [], []
    original = _squash(title)
    body = original
    found_feats: list[str] = []

    # 1. продублированный артист в начале: «Мельница - Меланья»
    for candidate in filter(None, [artist.strip() if artist else "", split_features(artist)[0] if artist else ""]):
        match = re.match(rf"(?i)^{re.escape(candidate)}\s*[-–—:]\s*(.+)$", body)
        if match and len(match.group(1).strip()) > 1:
            body = match.group(1).strip()
            break

    # 2. feat вне скобок
    lowered = f" {body.lower()} "
    cut = len(body)
    for marker in (" feat. ", " feat ", " ft. ", " ft ", " featuring ", " при участии ", " совместно с "):
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    if cut < len(body):
        found_feats.extend(_clean_feat_chunks(body[cut:]))
        body = body[:cut]

    # 3. скобки: метки издания — прочь, переводы — в варианты
    alternatives: list[str] = []

    def _paren_repl(match: re.Match) -> str:
        inner = match.group(1).strip()
        if not inner:
            return " "
        if re.match(r"(?i)^(feat\.?|ft\.?|featuring|при участии|совместно с)\b", inner):
            found_feats.extend(_clean_feat_chunks(inner))
            return " "
        if _is_noise(inner):
            return " "
        alternatives.append(inner)
        return " "

    stripped = _PAREN.sub(_paren_repl, body)

    # 4. хвост после тире: «- Radio Edit», «: 2015 Remastered»
    for _ in range(3):
        match = _DASH_TAIL.search(stripped)
        if not match:
            break
        value = re.sub(r"^\s[-–—_~|:]+\s*", "", match.group(0)).strip()
        if _is_noise(value):
            stripped = stripped[: match.start()]
        else:
            break
    core = _squash(re.sub(r"[-–—_~|:,]+$", "", stripped)) or original

    variants: list[str] = []
    for alt in alternatives:
        alt = alt.strip(" .,-")
        if _translation_like(core, alt) and norm(alt) != norm(core):
            variants.append(alt)
    return core, variants, found_feats


def keys_for(text: str) -> list[str]:
    """Все нормализованные ключи строки: родной + транслит + переводы из скобок."""
    result: list[str] = []
    core, alts, _feats = title_variants(text)
    for value in [core, *alts]:
        if not value:
            continue
        for candidate in (norm(value), norm(transliterate(value))):
            if candidate and candidate not in result:
                result.append(candidate)
    return result


def best_keys(*texts: str) -> list[str]:
    out: list[str] = []
    for text in texts:
        for key in keys_for(text):
            if key not in out:
                out.append(key)
    return out


@dataclass
class TrackText:
    """Нормализованное представление трека для матчинга."""

    raw_title: str = ""
    raw_artist: str = ""
    title: str = ""
    alt_titles: list[str] = field(default_factory=list)
    artist: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    isrc: str | None = None
    title_keys: list[str] = field(default_factory=list)
    artist_keys: list[str] = field(default_factory=list)
    pair_keys: list[str] = field(default_factory=list)
    title_key: str = ""
    artist_key: str = ""
    pair_key: str = ""
    versioned: bool = False

    @classmethod
    def build(
        cls,
        title: str,
        artist: str,
        album: str = "",
        duration_ms: int = 0,
        isrc: str | None = None,
    ) -> "TrackText":
        core, alts, feats_from_title = title_variants(title or "", artist or "")
        main_artist, feat_artists = split_features(artist or "")
        seen: set[str] = set()
        artists: list[str] = []
        for value in [main_artist, *feat_artists, *feats_from_title]:
            key = norm(value)
            if value and key and key not in seen:
                seen.add(key)
                artists.append(value)

        obj = cls(
            raw_title=title or "",
            raw_artist=artist or "",
            title=core,
            alt_titles=alts,
            artist=main_artist,
            artists=artists,
            album=album or "",
            duration_ms=int(duration_ms or 0),
            isrc=(isrc or None),
        )
        obj.title_keys = best_keys(core, *alts)
        obj.artist_keys = best_keys(main_artist, *feat_artists, *feats_from_title)
        obj.title_key = obj.title_keys[0] if obj.title_keys else ""
        obj.artist_key = obj.artist_keys[0] if obj.artist_keys else ""
        obj.pair_key = f"{obj.artist_key} :: {obj.title_key}" if obj.title_key else ""
        obj.pair_keys = [f"{a} :: {t}" for a in obj.artist_keys[:3] for t in obj.title_keys]
        obj.versioned = contains_version_title(title or "")
        return obj

    @property
    def primary_title(self) -> str:
        return self.title_key

    @property
    def primary_artist(self) -> str:
        return self.artist_key

    @property
    def display(self) -> str:
        return f"{self.artist or self.raw_artist or '?'} — {self.raw_title or self.title or '?'}"


def contains_version_title(title: str) -> bool:
    """Название содержит издательскую метку (remaster/deluxe/live…)."""
    return bool(contains_version_tag(title))


def duration_score(a_ms: int, b_ms: int, tolerance_ms: int, hard_ms: int) -> float:
    """1.0 при совпадении длительности, плавно до 0 к ``hard_ms``."""
    if not a_ms or not b_ms:
        return 0.6  # нет данных — не наказываем, но и не поощряем
    diff = abs(int(a_ms) - int(b_ms))
    if diff <= tolerance_ms:
        return 1.0
    if diff >= hard_ms:
        return 0.0
    return 1.0 - (diff - tolerance_ms) / max(1, hard_ms - tolerance_ms)


def is_ugc(explicit_kind: str | None) -> bool:
    """Треки, загруженные пользователями: на Spotify их почти наверняка нет."""
    return (explicit_kind or "").lower() in {"ugc", "podcast_episode", "video", "other"}
