# Soundbridge

Перенос музыкальной библиотеки из **Яндекс Музыки** в **Spotify** — напрямую
через официальные API обеих сторон. Без платных сервисов-переносчиков и без
браузерной автоматизации (агент, кликающий по чужим аккаунтам, нарушает правила
Spotify; здесь его нет).

Переносится:

* пользовательские плейлисты Яндекса (порядок треков сохраняется);
* «Мне нравится» — и в медиатеку Spotify (`/me/tracks`), и отдельным плейлистом;
* ничего не дублируется: перед записью снимается состояние уже существующей
  библиотеки Spotify.

Не переносится (сознательно): подкасты, радио, автосгенерированные плейлисты
Яндекса («Моя волна», «Премьеры» и т.п.) — они помечаются как skipped и в
отчётах видны.

## Как это работает

Конвейер из идемпотентных стадий; каждая перезапускается с любого места, всё
состояние живёт в SQLite `data/soundbridge.db`:

```
auth → export → snapshot → match → review → import → verify
                                 ↑                    │
                                 └── recompute ←──────┘ (пороги правятся офлайн)
```

1. **auth** — OAuth: Яндекс через device flow (пароль никуда не передаётся),
   Spotify через официальный Web API (PKCE, локальный redirect).
2. **export** — библиотека Яндекса выгружается в базу (треки, плейлисты,
   порядок, ISRC, лайки).
3. **snapshot** — снимок Spotify: что уже лежит в медиатеке и плейлистах, чтобы
   не тащить дубликаты в 700+ уже сохранённых треков.
4. **match** — каскад сопоставления: ISRC → нормализованный ключ (с
   транслитерацией кириллицы) → rapidfuzz по локальному индексу → `/search`.
   Все кандидаты сохраняются в базу, поэтому пороги можно менять повторным
   `recompute` **без единого запроса** к Spotify.
5. **review** — спорное (>5000 треков без ручного ревью никуда): CSV
   `out/review.csv` или интерактивный режим прямо в терминале.
6. **import** — создание плейлистов и заливка треков. По умолчанию `dry_run`:
   ни одного запроса на запись, пока не скажете `--apply`. Лимит Spotify
   10 000 треков на плейлист обходится автонарезкой на томы
   («Плейлист (часть 2)»).
7. **verify** — сверка «что записали» с «что реально в Spotify», отчёт
   `out/verify.md`.

## Установка

Нужен Python 3.12+ и активная подписка Spotify Premium (запись в библиотеку).

```powershell
cd Soundbridge
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Spotify: Client ID

1. Зайдите на <https://developer.spotify.com/dashboard> → *Create app*.
2. В **Redirect URI** укажите ровно: `http://127.0.0.1:8899/callback`
   (это локальный one-shot сервер, который `auth` поднимает сам).
3. Скопируйте `.env.example` → `.env` и впишите `SPOTIFY_CLIENT_ID=...`
   (можно и в `config.toml`, но секретам в конфиге не место — `.env` в gitignore).

### Яндекс

Ничего заводить не нужно: используется публичный client_id приложения Яндекс
Музыки и OAuth device flow — вы подтверждаете доступ кодом на официальной
странице `music.yandex.ru/authorize`.

### Конфиг

`config.example.toml` → `config.toml` (не обязателен, работает и на дефолтах).
Там пороги уверенности (`[match]`), лимиты и паузы (`[limits]`, `[throttle]`),
режимы импорта (`[import_]`).

## Запуск

Из корня проекта. В Windows консоль cp866 ломает кириллицу — запускайте с
`-X utf8` (сам CLI тоже переключает поток вывода, но `-X utf8` надёжнее):

```powershell
.venv\Scripts\python.exe -X utf8 -m soundbridge <команда>
```

Типовой путь:

```powershell
.venv\Scripts\python.exe -X utf8 -m soundbridge auth      # токены (browser сам откроется)
.venv\Scripts\python.exe -X utf8 -m soundbridge export    # библиотека Яндекса → SQLite
.venv\Scripts\python.exe -X utf8 -m soundbridge snapshot  # что уже есть в Spotify
.venv\Scripts\python.exe -X utf8 -m soundbridge match     # сопоставление (самая долгая стадия)
.venv\Scripts\python.exe -X utf8 -m soundbridge status    # сводка: сколько решено/спорно

# ручное ревью — на выбор (или оба):
.venv\Scripts\python.exe -X utf8 -m soundbridge report    # CSV out/review.csv + правка в Excel
.venv\Scripts\python.exe -X utf8 -m soundbridge apply-overrides
.venv\Scripts\python.exe -X utf8 -m soundbridge review    # или интерактивно в терминале

.venv\Scripts\python.exe -X utf8 -m soundbridge plan      # что поедет и какими томами
.venv\Scripts\python.exe -X utf8 -m soundbridge import            # dry-run, ничего не пишет
.venv\Scripts\python.exe -X utf8 -m soundbridge import --apply    # реальная запись
.venv\Scripts\python.exe -X utf8 -m soundbridge verify    # сверка с фактом
```

## Команды

| Команда | Назначение |
| --- | --- |
| `auth` | получить/обновить токены (`--yandex-only`, `--spotify-only`, `--relogin`, `--manual`) |
| `export` | выгрузить Яндекс в базу (`--include-generated` — захватить и автосгенерированные) |
| `snapshot` | снимок медиатеки/плейлистов Spotify (`--skip-playlists`) |
| `match` | сопоставить треки (`--limit N`, `--redo`, `--no-search`, `--ym-id`, `--ids-file`) |
| `report` | выписать спорное в `out/review.csv` (`--limit N`) |
| `review` | интерактивное ревью в терминале (1..9 — кандидат, `s` — не переносим, `m` — вставить URI, `n` — позже, `q` — выход) |
| `apply-overrides` | применить правки из CSV (`--dry-run`, `--path`) |
| `recompute` | пересчитать методы матчинга по новым порогам, без сети |
| `plan` | markdown-план переноса в `out/plan.md` (`-v` — по томам) |
| `import` | перенос в Spotify (по умолчанию dry-run; `--apply`, `--force` — пересоздать, `--only подстрока`) |
| `verify` | сверка записанного с фактом → `out/verify.md` |
| `status` / `logs` | состояние конвейера / журнал событий |

Глобальный ключ `--config путь` — прочитать другой `config.toml`.

## Ручное ревью

`report` кладёт в `out/review.csv` все спорные треки: колонка `decision_uri`
принимает `spotify:track:...`, ссылку `https://open.spotify.com/track/<id>`
(любые `?si=...` отсекаются) или голый 22-символьный id. Маркеры
`skip` / `нет` / `-` помечают трек непереносимым. Пустая ячейка — «ничего не
менять». Правки применяются `apply-overrides` и переживают повторные прогоны
`match` (ручные решения — приоритет `manual`).

## Честные ограничения

* **Реальные потери 5–15 %.** Часть треков Яндекса просто отсутствует в
  Spotify (русский андеграунд, концерты, ремиксы); их видно в `status` и CSV —
  это не баг переносчика.
* **Лимиты Spotify**: 10 000 треков в плейлисте и 10 000 в «Мне нравится».
  Плейлисты режутся на тома; лайки, не влезшие в медиатеку, остаются в
  отдельном плейлисте «Мне нравится (перенос из Яндекса)».
* **Dev-mode приложения** (2026): `/search` отдаёт не больше 10 кандидатов,
  а пакетный `GET /tracks?ids=` может быть недоступен — код это учитывает.
* Скорость: десятки тысяч запросов матчинга с троттлингом — это часы, а не
  минуты. Стадия чанкуется чекпоинтами, `Ctrl+C` не страшен.

## Разработка

```powershell
$env:PYTHONPATH="."
.venv\Scripts\python.exe -X utf8 scripts/test_normalize.py
.venv\Scripts\python.exe -X utf8 scripts/test_export.py
.venv\Scripts\python.exe -X utf8 scripts/test_spotify_transport.py
.venv\Scripts\python.exe -X utf8 scripts/test_pipeline.py   # весь конвейер на фейковом Spotify
```

Структура: `soundbridge/config.py` (пути/настройки), `normalize.py` (ключи +
транслит), `yandex_client.py` (OAuth device flow + `export_library`),
`spotify_client.py` (PKCE + транспорт с бакетом/backoff), `storage.py`
(SQLite-контракт), `matcher.py`, `snapshot.py`, `review.py` / `report.py`
(ревью), `importer.py` (запись/верификация), `cli.py` (сборка всего).

## Безопасность

В git не попадают: `.env`, `secrets/` (токены OAuth), `data/` (база с вашим
профилем), `out/` (отчёты). Токены Яндекса отзываемы
в <https://id.yandex.ru/security/devices>, Spotify —
в <https://www.spotify.com/account/apps/>.

### Если провайдер перехватывает TLS (Tattelecom и подобные)

GitHub за таким провайдером отдаёт цепочку, подписанную сертификатом
провайдера (`CN=Tattelecom.ttc`), с просроченным intermediate — git падает на
`CRYPT_E_NO_REVOCATION_CHECK` (schannel) или `certificate has expired`
(openssl). Разовое лечение для пуша из этого репозитория:

```powershell
git config --local http.schannelCheckRevoke false
git config --local http.sslVerify false   # ⚠ отключает проверку сертификата
```

Безопаснее тот же трафик по SSH (перехват TLS на него не действует):
сгенерируйте ключ (`ssh-keygen -t ed25519`), добавьте публичную часть в
GitHub → Settings → SSH keys, и переключите remote на
`git@github.com:<user>/Soundbridge.git`.

### Если Spotify отвечает 403 «Spotify is unavailable in this country»

Spotify официально не работает в РФ и режет Web API по IP. Нужен туннель
**уровня системы** — VPN-расширение в браузере не подходит: оно проксирует
только сам браузер, а Python-программа ходит в интернет напрямую.

Самый простой бесплатный способ — Psiphon 3 для Windows
(официальная страница: <https://s3.amazonaws.com/0ubz-2q11-gi9y/ru.html>):

1. скачать `psiphon3.exe`, запустить, нажать «Подключиться»;
2. в `.env` раскомментировать строку
   `SPOTIFY_PROXY=http://127.0.0.1:10809` (это локальный HTTP-прокси Псифона;
   SOCKS-порт — 10808, для него нужен `pip install pysocks`);
3. перелогинить Spotify: `.venv\Scripts\python.exe -m soundbridge auth
   --spotify-only --relogin` (или `scripts/manual_auth.py new/finish`);
4. убедиться, что перехвата нет: `scripts/check_spotify_tls.py` — issuer
   должен быть настоящим (DigiCert и т.п.), а не `Tattelecom.ttc`.

Psiphon должен оставаться включённым на время `snapshot` / `match` / `import`
(можно кусками — чекпоинты в SQLite позволяют продолжить позже).

