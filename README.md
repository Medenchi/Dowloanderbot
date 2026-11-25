# Dowloanderbot

Даунлоадер для Telegram, у которого **весь интерфейс — HTML-виджеты Bot API 10.4/10.5**.
Никаких «бот пишет текстом: выбрал качество / скачиваю / готово»: состояние, прогресс,
качество, ошибки и стрелки карусели живут в `chat_data` одного виджета, а меняется только оно.

Стиль — iOS 26/27 «liquid glass» **без градиентов**: полупрозрачное стекло, `backdrop-filter`
(ровно 1 «тяжёлый» блок, лимит профиля — 2), радиусы 26px, системный шрифт, цвета из
`--tg-theme-*`, авто light/dark. Виджет 250px высотой, без скролла.

```
┌──────────────────────────────┐
│  сообщение с фото / видео     │  ← editMessageMedia (меняется по стрелкам)
├──────────────────────────────┤
│  ┌────────────────────────┐  │
│  │ TIKTOK      карусель   │  │  ← sendWidget, HTML НЕ меняется
│  │ Photo dump             │  │
│  │ ▓▓▓▓▓▓▓▓▓░░  72%      │  │  ← editMessageWidgetData, ~1 раз/с
│  │ 360p 720p 1080p Аудио  │  │
│  │   ‹      2 / 7      ›  │  │  ← widget_query: "n" / "p"
│  │ Ещё раз Отмена Источник│  │
│  └────────────────────────┘  │
└──────────────────────────────┘
```

## Файлы (условно «3 файла», как просили)

| Файл | Что |
|---|---|
| **`bot.py`** | весь Python: aiogram-хендлеры, yt-dlp/ffmpeg, карусели, состояние, SQLite, очередь, webhook/polling |
| **`widget.html`** | единственный HTML-виджет (все фазы рендерятся из `chat_data`) |
| **`requirements.txt`** | зависимости |

Дополнительно: `.env.example`, `docs/BOT_API_WIDGETS.md` (исследование API и лимиты),
`scripts/widget_budget.py` (валидатор 4 КБ), `tests/test_bot.py` (27 тестов), `pytest.ini`.

## Быстрый старт

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
apt install ffmpeg            # либо brew install ffmpeg — для слияния и пережатия >48 МБ
cp .env.example .env          # вписать BOT_TOKEN
.venv/bin/python bot.py --check   # проверка без токена: бюджет виджета, версии, ffmpeg
.venv/bin/python bot.py
```

`MODE=polling` по умолчанию. Для `MODE=webhook` задать `WEBHOOK_URL`, `WEBHOOK_SECRET`,
`WEBHOOK_HOST=0.0.0.0`, `WEBHOOK_PORT`; `allowed_updates` уже включает `widget_query` и
`shared_widget_query`.

Тесты: `.venv/bin/python -m pytest tests -q` (в `pytest.ini` отключён autoload pytest-плагина
aiogram — иначе на Python 3.11 он импортируется раньше compat-шима).

> **Python:** рекомендуется 3.12+. На 3.11 `aiogram>=3.33` + `pydantic` падают при импорте
> (`TypeError: unhashable type: 'FailFast'` из-за хеширования `Annotated` в `typing` 3.11).
> В `bot.py` есть минимальный compat-шим — работает и на 3.11, это проверено тестами.

## Как работает

1. `/start` → бот отправляет **два** своих сообщения: PNG-заглушку (генерируется кодом, без
   ассетов) и под ней виджет. Заглушка потом заменяется реальным медиа через `editMessageMedia` —
   поэтому «фото сверху, виджетик снизу» сохраняется всегда.
2. Пользователь присылает ссылку. Поля ввода в виджете запрещены профилем 1 (нет `<input>`),
   поэтому ссылка — это сообщение **пользователя**; от бота текстов нет.
3. Тап по кнопке виджета → `TelegramWidget.request()` (1–64 байта) → `widget_query` →
   мгновенный `answerWidgetQuery` (дедлайн 15 с) → фоновый воркер → `editMessageWidgetData`.
   Команды короткие и из allow-list: `q:360`, `q:720`, `q:1080`, `q:a`, `n`, `p`, `r`, `x`.
4. Прогресс пушится троттлингом ~1 раз/с (правка состояния считается отправленным сообщением);
   неточный/плавый процент не жжёт лимиты. `Источник` — это `openLink` прямо из виджета.
5. `file_id` кэшируются в SQLite по `(url, качество)` и по слайдам: повторное качество и
   перелистывание карусели **не перезаливают** файлы.

## Площадки

Топ-список с подписями в виджете: YouTube, TikTok, Douyin, Instagram (Reels + карусели),
X/Twitter, VK, OK, Reddit, Twitch, Pinterest, Rutube, SoundCloud, Bilibili, Facebook,
Dailymotion, Vimeo, Threads, Snapchat, Coub, Rumble, Yandex — плюс **всё, что умеет yt-dlp**
(~1800 экстракторов, best effort). Приватные/возрастные посты — через `COOKIES_FILE`.

**Фото-карусели — отдельная история** (это важное открытие при исследовании):

* `yt-dlp` **не скачивает слайды TikTok**: на photo-пост он отдаёт только звуковую дорожку.
  Поэтому бот читает hydration-JSON страницы (`__UNIVERSAL_DATA_FOR_REHYDRATION__` /
  `SIGI_STATE`) и достаёт `imagePost.images[].imageURL.urlList`, а как второй источник зовёт
  `gallery-dl -g`.
* У Instagram фото-посты вообще не имеют `formats`; разведка идёт с
  `ignore_no_formats_error=True`, картинки берутся из `thumbnails` каждого `entries`.
* Слайды качаются в 4 потока с лимитом размера, режутся до `MAX_SLIDES`, dedupe по имени файла
  (у TikTok одно фото приходит с двух CDN-зеркал), хранятся до `KEEP_FILES_MIN`.

## Размеры файлов (гибрид)

| Ситуация | Что делает |
|---|---|
| ≤ 48 МБ | `sendVideo`/`editMessageMedia` как есть, `supports_streaming` |
| > 48 МБ | `ffmpeg` считает битрейт из длительности (`ffprobe`), жмёт в H.264/AAC под 48 МБ, `-fs` как жёсткий потолок, `nice 10` |
| Не влезает / нет ffmpeg | документ ≤50 МБ, либо кнопка «Источник» (`openLink` на оригинал) |
| Оригинал > `MAX_ORIGINAL_MB` (400) | `max_filesize` у yt-dlp рубит скачку на стороне HTTP-загрузчика |

Лимиты Bot API: 50 МБ загрузка файлом, 20 МБ если Telegram тянет по URL, 10 МБ для photo.
Свой local Bot API server (до 2000 МБ) не требуется — код к нему готов, но по умолчанию
используется официальный endpoint.

## Чтобы не легло

* `asyncio.Semaphore(MAX_PARALLEL=2)` на скачку **и** перекодировку; `MAX_QUEUE=40`, дальше —
  вежливое «очередь занята» в виджете, а не бесконечные таски.
* `max_filesize`, `socket_timeout`, `retries`, таймауты `inspect/download/encode`.
* `run_job` всегда снимает счётчик очереди в `finally`; `tmp/` чистится по TTL; видео-файлы
  удаляются сразу после получения `file_id`.
* SSRF-фильтр: только http(s), без кредов/нестандартных портов, DNS-резолв с блокировкой
  private/loopback/link-local/reserved; per-user rate limit 4 ссылки / 30 с.
* yt-dlp крутится в `asyncio.to_thread`, ffmpeg — в отдельном процессе; event loop не блокируется.
* Один запрос `request()` в полёте на виджете + `busy`-флаг в JS = не ловим `RATE_LIMITED`
  (лимит Telegram: 5 запросов / 5 с на пользователя).

## Ограничения платформы (не баги, а Bot API 10.4)

* Виджеты в проде — **только приватные чаты с ботом**, всегда `protect_content`, не форвардятся,
  не копируются. Группы/инлайн/форварды — test server.
* Старые клиенты (< layer 233) видят unsupported-медиа → бот ловит ошибку `sendWidget` и
  уходит в тихий фолбэк: одно текстовое сообщение + inline-клавиатура с теми же командами.
* HTML виджета **не редактируется после отправки** — только `chat_data`. Поэтому все фазы
  (`idle → queued → meta → down → encode → up → done → carousel → error`) рисует один HTML.
* Виджет ≤ 32 КБ raw / **≤ 4 КБ после zlib-9** (у нас 4563 / **1978** Б), `chat_data` ≤ 1 КБ,
  QuickJS без сети, только `data:`-картинки, системные шрифты, ≤2 `backdrop-filter`,
  максимум 600px, без внутреннего скролла.
* Bot API 10.5: `getMe()` теперь возвращает `BotFullInfo`; `callback_query_mode` по умолчанию
  `default` (иначе callback'и приходят с `inline_message_id` без `message`). Опционально
  `ENSURE_CALLBACK_MODE_DEFAULT=1` вызывает `setMySettings(callback_query_mode="default")`.

Подробности, точные сигнатуры методов, таблица лимитов и ссылки — в
[`docs/BOT_API_WIDGETS.md`](docs/BOT_API_WIDGETS.md).

## Конфиг

См. [`.env.example`](.env.example). Ключевое: `BOT_TOKEN`, `MODE`, `MAX_PARALLEL`,
`MAX_QUEUE`, `MAX_UPLOAD_MB`, `MAX_ORIGINAL_MB`, `MAX_SLIDES`, `COOKIES_FILE`, `HTTPS_PROXY`,
`USE_GALLERY_DL`, `DB_PATH`, `TMP_DIR`.
