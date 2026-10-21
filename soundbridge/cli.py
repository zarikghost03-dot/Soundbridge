"""Точка входа командной строки: ``python -m soundbridge <команда>``.

Конвейер состоит из идемпотентных стадий, которые можно перезапускать в любом
порядке и с любого места::

    auth → export → snapshot → match → ревью (report/review/apply-overrides)
        → import → verify

Никакая команда не пишет в Spotify без явного ``--apply`` у ``import``
(или ``import_.dry_run = false`` в config.toml).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Config, DB_PATH, ensure_dirs, load_config
from .importer import build_plan, run_import, verify
from .matcher import Matcher
from .report import (
    apply_decisions,
    format_status,
    plan_path,
    review_path,
    status,
    write_plan,
    write_review,
)
from .review import run_review
from .snapshot import Snapshot
from .spotify_client import SpotifyAccount, SpotifyError
from .storage import Database
from .yandex_client import YandexAccount, YandexError, export_library

PROGRESS: Callable[[str], None] = lambda msg: print(msg, flush=True)  # noqa: E731


# --------------------------------------------------------------------------- #
# вспомогательное
# --------------------------------------------------------------------------- #


def _utf8_stdout() -> None:
    """Windows-консоль иначе падает на кириллице/эмодзи в cp866."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _load(args: argparse.Namespace) -> Config:
    return load_config(Path(args.config) if getattr(args, "config", None) else None)


def _db() -> Database:
    ensure_dirs()
    return Database(DB_PATH)


def _connect_yandex(cfg: Config, args: argparse.Namespace) -> YandexAccount:
    def _show_code(code: str, url: str) -> None:
        PROGRESS(f"Откройте страницу Яндекса и подтвердите доступ. Код: {code}")
        PROGRESS(f"    {url}")
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    return YandexAccount.connect(cfg, force_login=getattr(args, "relogin", False),
                                 on_code=_show_code)


def _connect_spotify(cfg: Config, args: argparse.Namespace) -> SpotifyAccount:
    sp = SpotifyAccount.connect(
        cfg,
        force_login=getattr(args, "relogin", False),
        manual=getattr(args, "manual", False),
        on_status=PROGRESS,
    )
    return sp


def _remember_uid(db: Database, sp: SpotifyAccount) -> None:
    if sp.user_id:
        db.set_meta("sp_uid", sp.user_id)


# --------------------------------------------------------------------------- #
# команды
# --------------------------------------------------------------------------- #


def cmd_auth(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        if not args.spotify_only:
            account = _connect_yandex(cfg, args)
            PROGRESS(f"Яндекс: uid {account.uid} ({account.login}), premium={account.premium}")
            db.set_meta("ym_uid", str(account.uid))
        if not args.yandex_only:
            sp = _connect_spotify(cfg, args)
            _remember_uid(db, sp)
            PROGRESS(f"Spotify: {sp.user_id}, подписка '{sp.product}'")
        PROGRESS("Токены сохранены — можно запускать export/snapshot.")
        return 0
    finally:
        db.close()


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        account = _connect_yandex(cfg, args)
        stats = export_library(
            account,
            db,
            include_generated=args.include_generated,
            on_progress=PROGRESS,
        )
        PROGRESS(
            f"Экспорт готов: треков {stats.tracks}, плейлистов {stats.playlists}"
            f" (пропущено автосгенерированных {stats.skipped}), лайков {stats.likes}"
        )
        for note in stats.notes:
            PROGRESS(f"  · {note}")
        return 0
    finally:
        db.close()


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        sp = _connect_spotify(cfg, args)
        _remember_uid(db, sp)
        stats = Snapshot(db, sp, cfg).run(PROGRESS, with_playlists=not args.skip_playlists)
        PROGRESS(f"Снимок Spotify обновлён: {stats}")
        return 0
    finally:
        db.close()


def cmd_match(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        ids = list(args.ym_ids or [])
        if args.ids_file:
            ids += [ln.strip() for ln in Path(args.ids_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        if args.search:
            sp = _connect_spotify(cfg, args)
            _remember_uid(db, sp)
        else:
            sp = None  # type: ignore[assignment]  # /search не дёргаем, sp не нужен
            PROGRESS("Без /search: работаем только по локальным кандидатам и индексу.")
        stats = Matcher(db, sp, cfg).run(
            PROGRESS,
            ids=ids or None,
            force=args.redo,
            search=args.search,
            limit=args.limit,
        )
        PROGRESS(f"Матчинг: {stats}")
        review = db.review_counts()
        if review.get("review") or review.get("no_result"):
            PROGRESS("Есть спорные/ненайденные треки → `report` (CSV) или `review` (терминал).")
        return 0
    finally:
        db.close()


def cmd_report(args: argparse.Namespace) -> int:
    db = _db()
    try:
        stats = write_review(db, limit=args.limit)
        PROGRESS(f"CSV ревью: {stats['written']} строк из {stats['total_review']} → {stats['path']}")
        PROGRESS("Правьте колонку «decision_uri»: URI/ссылка/id; «skip» — не переносим. Затем: apply-overrides")
        return 0
    finally:
        db.close()


def cmd_review(args: argparse.Namespace) -> int:
    db = _db()
    try:
        result = run_review(db, limit=args.limit)
        PROGRESS(
            f"Сохранено {len(result['decided'])}, не переносим {len(result['skipped'])},"
            f" отложено {len(result['left'])}"
        )
        return 0
    finally:
        db.close()


def cmd_apply_overrides(args: argparse.Namespace) -> int:
    db = _db()
    try:
        path = Path(args.path) if args.path else review_path()
        result = apply_decisions(db, path, dry_run=args.dry_run)
        if args.dry_run:
            PROGRESS(
                f"dry-run: применили бы {result['would_apply']} решений,"
                f" отметили бы непереносимыми {result['would_skip']}"
            )
        else:
            PROGRESS(
                f"Принято {result['applied']}, помечено непереносимыми {result['skipped']},"
                f" не найдено в базе {result['missing']}"
            )
            for detail in result.get("missing_detail") or []:
                PROGRESS(f"  · нет такого ym_id: {detail}")
        PROGRESS("Дальше: `plan` для контроля и `import --apply`.")
        return 0
    finally:
        db.close()


def cmd_recompute(args: argparse.Namespace) -> int:
    db = _db()
    try:
        touched = db.recompute_match_methods()
        PROGRESS(f"Методы пересчитаны по сохранённым кандидатам: {touched} треков.")
        return 0
    finally:
        db.close()


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        result = write_plan(db, cfg)
        PROGRESS(f"План: {result['volumes']} тонов → {plan_path()}")
        if args.verbose:
            for t in build_plan(db, cfg)["targets"]:
                PROGRESS(
                    f"  {t['name']}: {len(t['uris'])} треков (том {t['volume']}/{t['volumes']})"
                    + (f", решений не хватает: {t['missing']}" if t["missing"] else "")
                )
        return 0
    finally:
        db.close()


def cmd_import(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.apply:
        cfg.import_.dry_run = False
    db = _db()
    try:
        sp = _connect_spotify(cfg, args)
        _remember_uid(db, sp)
        summary = run_import(db, sp, cfg, PROGRESS, force=args.force, only=args.only or None)
        if summary["dry_run"]:
            PROGRESS(
                f"Черновик: тонов {summary['targets']}, треков {summary['tracks_added']},"
                f" лайков {summary['likes_saved']}. Реальная запись — с ключом --apply."
            )
        else:
            PROGRESS(
                f"Готово: создано {summary['playlists_created']}, дописано {summary['playlists_reused']},"
                f" треков добавлено {summary['tracks_added']}, лайков сохранено {summary['likes_saved']}"
            )
            if summary.get("likes_dropped"):
                PROGRESS(f"  ⚠ {summary['likes_dropped']} лайков не влезли в медиатеку — они остались в плейлисте")
        for note in summary["notes"]:
            PROGRESS(f"  · {note}")
        if not summary["dry_run"]:
            PROGRESS("Финальная сверка: `python -m soundbridge verify`")
        return 0
    finally:
        db.close()


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        sp = _connect_spotify(cfg, args)
        result = verify(db, sp, PROGRESS)
        PROGRESS(
            f"Проверено тонов: {result['playlists']}, расхождений: {result['mismatches']}."
            " Подробно — out/verify.md"
        )
        return 0 if not result["mismatches"] else 2
    finally:
        db.close()


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = _db()
    try:
        PROGRESS(format_status(status(db, cfg)))
        return 0
    finally:
        db.close()


def cmd_logs(args: argparse.Namespace) -> int:
    db = _db()
    try:
        for row in db.logs(limit=args.limit):
            PROGRESS(f"{row['ts']}  [{row['kind']}]  {row['detail']}")
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# парсер
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soundbridge",
        description="Перенос аудиобиблиотеки из Яндекс Музыки в Spotify через их API.",
        epilog="Типовой путь: auth → export → snapshot → match → report/review → "
               "apply-overrides → import --apply → verify",
    )
    parser.add_argument("--version", action="version", version=f"Soundbridge {__version__}")
    parser.add_argument("--config", help="путь к config.toml (по умолчанию ./config.toml)")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--relogin", action="store_true",
                        help="забыть сохранённый токен и залогиниться заново")
    common.add_argument("--manual", action="store_true",
                        help="Spotify: вставить ссылку/код вручную, без браузера")

    sub = parser.add_subparsers(dest="command", metavar="<команда>")

    p = sub.add_parser("auth", parents=[common], help="получить токены Яндекса и Spotify")
    p.add_argument("--spotify-only", action="store_true")
    p.add_argument("--yandex-only", action="store_true")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("export", parents=[common], help="выгрузить библиотеку Яндекса в SQLite")
    p.add_argument("--include-generated", action="store_true",
                   help="забрать и автосгенерированные плейлисты (Вайб-плейлист и т.п.)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("snapshot", parents=[common], help="снять текущее состояние Spotify")
    p.add_argument("--skip-playlists", action="store_true", help="только медиатека")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("match", parents=[common], help="сопоставить треки Яндекса со Spotify")
    p.add_argument("--limit", type=int, help="обработать не более N треков")
    p.add_argument("--redo", action="store_true",
                   help="пересопоставить даже треки, по которым решение уже есть")
    p.add_argument("--no-search", dest="search", action="store_false",
                   help="не дёргать /search (только кэш и локальный индекс)")
    p.add_argument("--ym-id", dest="ym_ids", action="append", metavar="ID",
                   help="пересобрать конкретные треки (можно несколько раз)")
    p.add_argument("--ids-file", help="файл со списком ym_id, по одному в строке")
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("report", help="выписать спорные треки в CSV для ручного ревью")
    p.add_argument("--limit", type=int, default=0, help="максимум строк в CSV")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("review", help="интерактивное ревью прямо в терминале")
    p.add_argument("--limit", type=int, default=0, help="разобрать не более N треков")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("apply-overrides", help="применить решения из CSV ревью")
    p.add_argument("--path", help="CSV (по умолчанию out/review.csv)")
    p.add_argument("--dry-run", action="store_true", help="только посчитать, не писать")
    p.set_defaults(func=cmd_apply_overrides)

    p = sub.add_parser("recompute", help="пересчитать методы матчинга по новым порогам (без сети)")
    p.set_defaults(func=cmd_recompute)

    p = sub.add_parser("plan", help="записать markdown-план переноса")
    p.add_argument("-v", "--verbose", action="store_true", help="распечатать по томам")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("import", parents=[common], help="создать плейлисты и залить треки")
    p.add_argument("--apply", action="store_true", help="реально писать в Spotify")
    p.add_argument("--force", action="store_true",
                   help="пересоздать плейлисты даже если они уже импортированы")
    p.add_argument("--only", action="append", metavar="ЧАСТЬ",
                   help="обрабатывать только плейлисты, содержащие подстроку")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("verify", parents=[common], help="сверить записи с фактом в Spotify")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("status", help="текущее состояние конвейера")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("logs", help="последние события из журнала базы")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_logs)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except (YandexError, SpotifyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"Файл не найден: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано. Состояние сохранено — команду можно повторить.", file=sys.stderr)
        return 130


def run() -> None:
    raise SystemExit(main())
