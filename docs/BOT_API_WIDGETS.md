# Bot API 10.4 / 10.5 — виджеты: что реально можно, и как это влияет на downloader-бота

Исследовано 2026-11-25. Источники: официальные Bot API / Telegram Widgets / aiogram 3.34.0 (установлен с PyPI и прочитан исходниками) / `TelegramMessenger/widget-sandbox` (`docs/tgw-format.md`, `docs/profile-v1.md`, `docs/sdk-v1.md`, `packages/core/profile.json`).

## 1. Версии

| Дата | Версия | Что даёт нам |
|---|---|---|
| 09.11.2026 | **10.4** | HTML-виджеты: `Widget`, `sendWidget`, `sendSharedWidget`, `WidgetQuery`, `SharedWidgetQuery`, `answerWidgetQuery`, `editMessageWidgetData`, `editSharedWidgetData`, inline-результаты `InputWidgetMessageContent`; приватные ответы на callback'и, TON-кошелёк |
| 22.11.2026 | **10.5** | `setMySettings` (inline/privacy/groups/`has_topic_sidebar`/`callback_query_mode`), `getMe` → `BotFullInfo`, `active_usernames`, `inviter`, `blocks` в expandable-цитатах. Виджеты **не менялись** |

- `callback_query_mode` (10.5) — ловушка: при `auto`/`inline` нажатия на inline-кнопки приходят с `inline_message_id` вместо `message`. Для фолбэк-клавиатур оставляем режим `default`, чтобы не ломать `ctx.message`.
- aiogram: **3.33.0** = Bot API 10.4 (виджеты), **3.34.0** = Bot API 10.5. Это уже релизы, «сырые» HTTP-вызовы городить не нужно. В `pyproject.toml` фиксируем `aiogram>=3.34,<3.35`.

## 2. Точный синтаксис (aiogram 3.34.0, проверено в исходниках)

```python
# Отправка панели управления
msg = await bot.send_widget(
    chat_id=chat_id,
    html=WIDGET_HTML,          # str, 1..32768 байт UTF-8
    width=None,                # 200..ширина пузыря, None = вся ширина
    height=180,                # 50..600, None = авто
    chat_data='{"v":1,"p":0}', # ≤1024 байт, состояние конкретного сообщения
    public_data=None,          # ≤1024 байт, общее состояние (для is_shared)
    caption='Загрузка…',       # фолбэк для старых клиентов (0..1024 симв.)
    show_caption_above_media=True,  # чтобы время не рисовалось поверх виджета
    allow_queries=True,        # БЕЗ этого все request() из виджета отклоняются
    is_shared=False,
)

# Приём тапов
@router.widget_query()         # есть router.shared_widget_query() для forwards
async def on_tap(q: WidgetQuery, bot: Bot):
    if q.type.type != "data":        # != data ⇒ TON-подпись/транзакция
        await q.answer()             # answerWidgetQuery, ДЕДЛАЙН 15 с
        return
    cmd = q.data                     # 1..64 байт, НЕ доверяем, валидируем allow-list
    await q.answer(data="ok")       # ≤1024 байт, ответ не меняет состояние!
    # фоновая работа: скачка, editMessageMedia, потом:
    await bot.edit_message_widget_data(
        chat_id=q.chat.id, message_id=q.message_id,
        chat_data='{"v":2,"p":42}',
    )                               # пустая строка очищает поле; не переданное — не меняется

# Webhook / polling: явно просим новые типы
await bot.set_webhook(url=..., allowed_updates=[
    "message", "callback_query", "widget_query", "shared_widget_query",
])
# dp.start_polling(..., allowed_updates=[...]) — та же логика
```

`WidgetQuery` поля: `id`, `from_user` (alias `from`), `chat`, `message_id`, `inline_message_id`, `chat_instance`, `chat_type`, `type` (дискриминированный union: `data` / `sign_data` / `sent_transaction`), `data`, `widget_id`. `SharedWidgetQuery` приходит **без** `chat`/`message_id` — только `widget_id`, поэтому для карусели shared-виджеты неудобны.

Методы: `SendWidget`, `SendSharedWidget`, `AnswerWidgetQuery`, `EditMessageWidgetData`, `EditSharedWidgetData(widget_id, public_data, cache_time=5..3600)`. Шорткат `q.answer(data=...)` уже подставляет `widget_query_id`.

## 3. Ограничения виджета (жёсткие, сервер их проверяет)

| Что | Лимит / правило |
|---|---|
| HTML | ≤ 32 768 байт UTF-8 **И** ≤ 4096 байт после `zlib.compress(html, 9)` (RFC 1950). Без NUL. Проверять на билде |
| `chat_data` / `public_data` | ≤ 1024 **байт** каждое (UTF-8, не символы), без NUL; пишет только бот |
| `request(data)` из виджета | 1–64 байта, только в обработчике тапа (или фон: 1 при показе + 1 раз/5 с, только приватный чат), **один запрос в полёте**, один на тап |
| `answerWidgetQuery` | ≤ 1024 байта, **в течение 15 с**, один раз; просроченный `id` → `QUERY_ID_INVALID` |
| Частота | ≤ 5 запросов / 5 с на пользователя и бота (на все виджеты сразу) → `RATE_LIMITED` |
| `editMessageWidgetData` | считается **отправленным сообщением** по flood-лимитам → throttle; правки shared-виджета ≤ 1/с |
| Где работает в проде | **только приватные чаты с ботом**; группы, инлайн и форварды — только test server. `protect_content` выставляется автоматически, копировать/пересылать нельзя |
| Старые клиенты | layer < 233 получают `messageMediaUnsupported` → нужен осмысленный `caption` и/или текстовый фолбэк |
| HTML после отправки | **неизменяем**: `editMessage*` меняет только состояние/подпись. Новая вёрстка = новое сообщение |
| Размер пузыря | width 200..ширина пузыря, height 50..600 px, всё выше **обрезается**, внутреннего скролла нет |
| Сеть | QuickJS-песочница: `fetch`/XHR/WebSocket/внешние скрипты/шрифты/картинки — запрещены. Только `data:`-картинки ≤1024×1024 или inline SVG |
| DOM | нет `<input>/<textarea>/<select>`, iframe, video, WebGL; `document.getElementById` (глобальные переменные по id не работают); ≤2000 узлов, ≤400 CSS-правил, 8 MB памяти, ≤64 таймеров |
| Шрифты | только общие семейства: `system-ui`, `sans-serif`, `serif`, `monospace`, `ui-rounded`; `-apple-system` → алиас на `system-ui` |
| «Стекло» | `backdrop-filter: blur()/brightness()` и `filter` — in-profile, НО одновременно ≤16 «тяжёлых» блоков и **максимум 2 с `backdrop-filter`**, иначе виджет стопорится. `opacity<1`, размытые `box-shadow` тоже считаются. Градиенты разрешены, но по ТЗ не используем |
| CSS, которое вырезается | `@font-face`, `@import`, `:hover`, `:has()`, `color-mix()`, `oklch()`, `clip-path`, masks, 3D-трансформы, `vh`, `overflow: scroll`, `position: sticky`, `text-shadow`, `user-select` по умолчанию выключен |
| Тема | `TelegramWidget.colorScheme` (`light`/`dark`), `themeParams`, CSS-переменные `--tg-theme-*`, `--tg-color-scheme`; событие `themeChanged` |
| События SDK | `onEvent('chatDataChanged'|'publicDataChanged'|'themeChanged'|'activated'|'deactivated'|'contentSafeAreaChanged')`; `request()`, `copyText()`, `openLink()`, `setResizeAnchor()`, `watchPublicData()` |
| Прочее | `localStorage` 4 KB на сообщение (на устройстве, не сервер); виджет может быть убит и пересоздан в любой момент → первый кадр рисуем синхронно из `chatData` |

## 4. Что это значит для нашего бота (архитектурные выводы)

1. **Фото/видео НЕЛЬЗЯ положить внутрь виджета.** Пейлоад ≤4 КБ после zlib, а base64-фото почти не жмётся. Значит схема ровно как в ТЗ: отдельное сообщение с медиа + отдельное сообщение-виджет снизу (панель: прогресс, стрелки ‹ 2/7 ›, качество, формат). `editMessageMedia` меняет фото наверху, `editMessageWidgetData` — счётчик/прогресс внизу. `file_id` кэшируем, чтобы стрелки не перезаливали файл.
2. **Скачка асинхронная:** тап → мгновенный `q.answer()` (<15 с) → фоновый воркер (очередь + semaphore, yt-dlp в `asyncio.to_thread`/процессе) → пуш прогресса в `chat_data` с троттлингом ~1 раз/с. НеExact-прогресс можно вообще рисовать CSS-анимацией по сигналу `phase` из состояния — это экономит лимиты отправки.
3. **Команды, не данные:** `request('n')`, `request('p')`, `request('q:720')` — короткие строки из allow-list; весь контекст (какой пост, какой слайд) берём из своей БД по `(chat_id, message_id)`, а не из `data`.
4. **Бюджет состояния:** компактные JSON-ключи, ≤1024 байт: `{v,p,ph,n,i,title}` и т.п. Кириллица = 2 байта/символ, эмодзи = 4.
5. **Фолбэк обязателен:** приватный чат + свежий клиент → виджет; группа / старый клиент / ошибка `sendWidget` → обычная inline-клавиатура и короткий текст. «Ноль сообщений» в проде attainable только в привате.
6. **Медиа-лимиты:** загрузка файла ≤50 МБ (фото ≤10 МБ), по URL ≤20 МБ, скачка ботом ≤20 МБ; свой local Bot API server даёт до 2000 МБ. Отсюда: `yt-dlp` → `ffprobe` → при необходимости перекодировка в <48 МБ, иначе документ/ссылка; временные файлы чистим, конкуренцию ограничиваем, чтобы сервер не лёг.
7. **Один HTML на версию:** вёрстка не редактируется, все состояния (idle → parsing → downloading → uploading → done → error → карусель) рендерятся из `chat_data`. Поэтому сначала проектируем схему состояния, потом CSS.
8. **Liquid Glass без градиентов:** `background: rgba(...)` + `backdrop-filter: blur(20px)` на 1–2 контейнерах, `border: 1px solid rgba(255,255,255,.18)`, `border-radius: 22–28px`, `box-shadow` без размытия, `system-ui`, цвета из `--tg-theme-*`, авто light/dark. Анимации — CSS/`requestAnimationFrame`, без перестроек DOM каждый кадр.

## 5. Что решили и как реализовано в этом репо

| Вопрос | Решение |
|---|---|
| Серверная часть | Пишем с нуля: `yt-dlp` + `ffmpeg`, очередь, SQLite-кэш `file_id`. Разные источники — через единый `inspect_plan()` |
| Площадки | Фиксированный топ-список с подписями + всё, что умеет yt-dlp; `COOKIES_FILE` для приватного |
| Большие файлы | Гибрид: ≤48 МБ как есть → ffmpeg-пережатие → документ/ссылка на источник |
| Карусель | «Фото сверху + виджет со стрелками снизу»: `editMessageMedia` + `editMessageWidgetData`, PNG-заглушка на `/start` сохраняет порядок сообщений |
| Строгость UI | Виджет-фёрст, тихий фолбэк на inline-клавиатуру для групп/старых клиентов |
| Нагрузка | Семафор, лимит очереди, `max_filesize`, TTL-cleanup, SSRF-фильтр, rate limit, `to_thread` |

Файлы: `bot.py` (вся логика), `widget.html` (один HTML на все фазы), `requirements.txt`,
`tests/test_bot.py` (27 тестов), `scripts/widget_budget.py`.

### Дополнительные находки при реализации

* **`yt-dlp` не качает фото-карусели TikTok.** Проверено исходниками экстрактора
  (`yt_dlp/extractor/tiktok.py`, 2026.11.23): `imagePost` не парсится, слайдшоу отдаётся как
  audio-only mp3 + одна обложка. Обход: hydration-JSON страницы
  (`__UNIVERSAL_DATA_FOR_REHYDRATION__` / `SIGI_STATE`) → `imagePost.images[].imageURL.urlList`,
  плюс `gallery-dl -g` вторым источником. Зеркала `p16-`/`p77-tiktokcdn` дедуплицируются по
  имени файла.
* **Instagram**: `ignore_no_formats_error=True` в `YoutubeDL` — иначе `extract_info` падает с
  `No video formats found`. Фото-слайды приходят как `thumbnails` у `entries`.
* **Ключи джобов**: `message_id` уникален только внутри чата, поэтому задачи и временные папки
  ключуются `(chat_id, widget_id)`, а не одним `widget_id`.
* **`editMessageWidgetData` = отправленное сообщение** по flood-лимитам: троттлинг 0.95 с,
  `TelegramRetryAfter` → один повтор. Прогресс-репортер пушит только изменившийся процент.
* **PNG-заглушка** генерируется чистым stdlib (`zlib` + PNG-чанки), потому что виджет не может
  содержать фото (4 КБ после zlib, base64 не жмётся), а `editMessageMedia` умеет менять
  photo → video/document/audio.
* **Python 3.11**: `aiogram>=3.33` не импортируется из-за `pydantic.FailFast` (незамороженный
  dataclass) и `typing.Annotated.__hash__` в 3.11. В `bot.py` — compat-шим; `pytest.ini`
  отключает autoload pytest11-плагина aiogram. На 3.12+ шим не нужен, но безвреден.
* 10.5 `setMySettings(callback_query_mode="default")` вызывается только при
  `ENSURE_CALLBACK_MODE_DEFAULT=1`, чтобы не менять глобальные настройки бота без запроса.

### Продакшен-гейт `INPUT_MEDIA_WIDGET_NOT_ALLOWED` (с 13.11.2026)

ОфициальнаяAvailability-таблица (`private chat: Yes`) **устарела де-факто**: с 13 ноября
`sendWidget` в проде отвечает `400 Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED` даже в личке
и даже с минимальным 42-байтовым HTML. Контрибьютор Bot API `levlam` подтвердил:
«Yes, currently, general bots aren't allowed to use widgets even in private chats»
— [tdlib/telegram-bot-api#947](https://github.com/tdlib/telegram-bot-api/issues/947).
Ошибка не описана в `/bots/widgets` (там только `TOO_LARGE` / `HTML_INVALID` /
`DATA_TOO_LARGE` / `DATA_INVALID`), но в `widget-sandbox/docs/tgw-format.md` она отнесена
к гейту «the chat is not one a bot may send widgets to». Тоггла в BotFather нет — серверный
конфиг/whitelist, срока снятия ограничения не объявляли.

Практика для репо: `TELEGRAM_TEST_SERVER=1` (aiogram `AiohttpSession(api=TEST)`, путь
`.../bot<token>/test/METHOD_NAME`, отдельный аккаунт и отдельный бот) — там виджеты доступны
любому боту; в проде `TELEGRAM_WIDGETS=auto` + фолбэк с ретраем раз в `WIDGET_RETRY_MIN`
минут. Отказ не кешируется насовсем (`Session.fallback_at`), иначе чат навсегда остаётся на
текстовой клавиатуре после возврата доступа.

### Зависание разведки и петля рестартов (кейс `l.likee.video`)

`LikeeIE._VALID_URL` в yt-dlp 2026.11.23 — `https?://(www\.)?likee\.video/...`; короткий
домен `l.likee.video` не подходит ни одному экстрактору, поэтому включается `generic`,
который грузит страницу и может надолго зависнуть на TLS. Проверено локально:
`ERROR: [generic] uc96FR: Unable to download webpage: TLS/SSL connection has been closed`,
при этом yt-dlp CLI с `-J` печатает `null` в stdout **и возвращает rc=0** — полагаться
только на returncode нельзя, надо смотреть `stderr` и `"null"`.

`asyncio.to_thread` + `asyncio.wait_for` отменяет корутину, но **не убивает поток**. Вместе с
`delete_webhook(drop_pending_updates=False)` это давало петлю: рестарт → те же pending
апдейты → две одинаковые задачи → снова зависание. Лечение: нормализация коротких доменов,
разведка в отдельном процессе с `subprocess.run(timeout=...)`, `drop_pending_updates=True`,
дедуп по `(url, quality)`, игнор устаревших `message.date`, `faulthandler.dump_traceback_later`
и логирование `SIGTERM`. `SystemExit` из потока гасится в `run_job`, иначе asyncio.Task
пробрасывает его в event loop и завершает процесс.

### Диагностика «видео не качаются»

`--doctor` / `/diag` показывают Python, aiogram, yt-dlp, `ffmpeg`/`ffprobe`/`gallery-dl`,
режим API, DNS до `youtube.com` / `www.tiktok.com` / `api.telegram.org`, `getMe`, очередь и
свободное место. Каждое решение в `on_text` и `run_job` логируется: `ссылка отклонена`,
`rate limit`, `разведка не удалась`, `скачка: старт/готово`, `job ТАЙМАУТ`, `job failed`.
Без ffmpeg `format_selector` просит только `best[...][vcodec!=none][acodec!=none]` и не
выставляет `merge_output_format` — иначе yt-dlp брал DASH video-only и падал на
`FFmpegMergerPP`. После merge берётся `info["filepath"]`, а не `max(size)` по
`requested_downloads`.

## Ссылки

- https://core.telegram.org/bots/api#sendwidget · https://core.telegram.org/bots/widgets · https://core.telegram.org/bots/api-changelog
- https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/tgw-format.md
- https://docs.aiogram.dev/en/latest/ · https://github.com/aiogram/aiogram/pull/2087 · https://github.com/aiogram/aiogram/pull/2089
