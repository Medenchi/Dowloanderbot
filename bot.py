#!/usr/bin/env python3
"""
Dowloanderbot — Telegram-бот-даунлоадер на HTML-виджетах Bot API 10.4/10.5.

Один Python-файл + widget.html + requirements.txt.

Как это работает
  * На /start бот отправляет ДВА своих сообщения: «фото-заглушку» и под ней —
    виджет. Больше никаких текстовых сообщений от бота нет (кроме фолбэка).
  * Пользователь присылает ссылку (поле ввода в виджете запрещено профилем 1,
    поэтому ссылка — это сообщение пользователя, а не бота).
  * Вся информация (фаза, прогресс, качество, стрелки карусели, ошибки) живёт
    в chat_data виджета; HTML после отправки НЕ меняется (лимит 10.4).
  * Тап -> widget_query (1..64 байт) -> мгновенный answerWidgetQuery (<15 c) ->
    фоновая работа -> editMessageWidgetData с троттлингом.
  * Фото/видео меняются через editMessageMedia у верхнего сообщения; file_id
    кэшируются, стрелки карусели не перезаливают файлы.

Защита от «больших файлов»: общий семафор, ограниченная очередь, max_filesize
у yt-dlp, перекодировка ffmpeg под <48 МБ, TTL-очистка tmp, SSRF-фильтр URL,
per-user rate limit, таймауты на всех сетевых операциях.

Запуск:  cp .env.example .env && ... && python bot.py
Проверка без токена: python bot.py --check
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import importlib.metadata
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _compat_python311_pydantic() -> None:
    """aiogram >=3.33 + pydantic 2.x не импортируются на Python 3.11:
    pydantic.FailFast — незамороженный dataclass, а typing в 3.11 хеширует
    metadata у Annotated[...] (TypeError: unhashable type: 'FailFast').
    На 3.12+ typing это переживает. Делаем объект хешируемым — безопасно,
    меняется только __hash__ у неизменяемой по смыслу метки."""
    try:
        from pydantic import FailFast
        if FailFast.__hash__ is None:
            FailFast.__hash__ = lambda self: hash(self.fail_fast)  # type: ignore[assignment]
    except Exception:
        pass


_compat_python311_pydantic()

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TEST
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
import aiogram
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    SharedWidgetQuery,
    WidgetQuery,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

log = logging.getLogger("dowloander")


def ytdlp_version() -> str:
    """Версия пакета БЕЗ импорта yt-dlp (он съедает ~13 МБ RSS в родителе)."""
    try:
        return importlib.metadata.version("yt-dlp")
    except Exception:
        return "?"


def detect_memory_limit_bytes() -> int | None:
    """Реальный лимит контейнера: cgroup важнее, чем MemTotal хоста."""
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = Path(p).read_text().strip()
            if v.isdigit() and 0 < int(v) < (1 << 50):
                return int(v)
        except (OSError, ValueError):
            pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def gallery_dl_bin() -> str | None:
    """pip кладёт консольный скрипт в .venv/bin, который часто не в PATH."""
    found = shutil.which("gallery-dl")
    if found:
        return found
    cand = Path(sys.executable).with_name("gallery-dl")
    return str(cand) if cand.is_file() else None


def current_rss_mb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0

# ---------------------------------------------------------------------------
# Константы и конфиг
# ---------------------------------------------------------------------------

ALLOWED_UPDATES = ["message", "callback_query", "widget_query", "shared_widget_query"]
ALLOWED_QUALITIES = {"360", "720", "1080", "a"}
WIDGET_HEIGHT = 250
WIDGET_CAPTION = "Отправь ссылку — скачаю видео, аудио или фото-карусель."
PUSH_MIN_INTERVAL = 0.95          # editMessageWidgetData считается отправкой сообщения
MAX_CHAT_DATA = 1024
MAX_HTML_RAW = 32_768
MAX_HTML_ZLIB = 4_096
MAX_REQUEST = 64
MAX_ANSWER = 1024

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav"}
IMAGE_URL_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.I)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

SOURCE_MAP = (
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("tiktok.com", "TikTok"), ("douyin.com", "Douyin"),
    ("instagram.com", "Instagram"),
    ("twitter.com", "X"), ("x.com", "X"),
    ("vk.com", "VK"), ("vk.ru", "VK"), ("ok.ru", "OK"),
    ("reddit.com", "Reddit"), ("redd.it", "Reddit"),
    ("twitch.tv", "Twitch"), ("pinterest.com", "Pinterest"), ("pin.it", "Pinterest"),
    ("rutube.ru", "Rutube"), ("soundcloud.com", "SoundCloud"),
    ("bilibili.com", "Bilibili"), ("facebook.com", "Facebook"), ("fb.watch", "Facebook"),
    ("dailymotion.com", "Dailymotion"), ("vimeo.com", "Vimeo"),
    ("threads.net", "Threads"), ("snapchat.com", "Snapchat"),
    ("coub.com", "Coub"), ("rumble.com", "Rumble"), ("yandex.ru", "Yandex"),
    ("likee.video", "Likee"), ("l.likee.video", "Likee"),
)

# Короткие домены, которых нет в _VALID_URL экстракторов yt-dlp. Без замены
# l.likee.video уходит в generic IE и виснет на загрузке страницы вместо LikeeIE.
SHORT_HOST_MAP = {
    "l.likee.video": "likee.video",
    "m.likee.video": "likee.video",
    "likee.com": "likee.video",
    "share.likee.video": "likee.video",
}


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def load_dotenv(path: Path | None = None) -> int:
    """Минимальный .env-загрузчик на stdlib: без python-dotenv в зависимостях.
    Реальный env хоста имеет приоритет — не перетираем уже выставленное."""
    p = path or Path(".env")
    if not p.is_file():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) > 1 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()   # inline-комментарий в .env.example
        if key and key not in os.environ:
            os.environ[key] = val
            n += 1
    return n


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _env_choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
    v = _env(key, default).lower()
    return v if v in allowed else default


def _env_json(key: str) -> dict[str, Any]:
    try:
        v = json.loads(_env(key, "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        log.warning("%s: невалидный JSON, игнорирую", key)
        return {}


@dataclass(frozen=True)
class Config:
    token: str
    mode: str
    webhook_url: str
    webhook_secret: str
    webhook_path: str
    webhook_host: str
    webhook_port: int
    db_path: Path
    tmp_dir: Path
    max_parallel: int
    max_queue: int
    max_upload_mb: int
    max_original_mb: int
    max_image_mb: int
    max_slides: int
    inspect_timeout: int
    download_timeout: int
    encode_timeout: int
    keep_files_min: int
    cookies_file: str
    proxy: str
    use_gallery_dl: bool
    ensure_callback_mode_default: bool
    rate_per_30s: int
    log_level: str
    widget_mode: str            # auto | on | off
    test_server: bool           # /test/ endpoint, где виджеты разрешены всем
    widget_retry_min: int       # TTL фолбэка, чтобы не залипнуть на нём навсегда
    ytdlp_extractor_args: dict[str, Any]
    isolate_inspect: bool       # разведка в отдельном ПРОЦЕССЕ с kill по таймауту
    drop_pending_updates: bool  # не перечитывать старые апдейты после рестарта
    faulthandler_interval: int  # 0 = выкл; дамп стеков, если бот завис
    ytdlp_cli_extra: str        # доп. флаги yt-dlp CLI через shlex
    memory_profile: str         # auto | low | normal
    memory_limit_bytes: int | None
    low_memory: bool
    ytdlp_max_as_mb: int        # ulimit -v для дочернего yt-dlp, 0 = без лимита

    @classmethod
    def from_env(cls) -> "Config":
        memory_profile = _env_choice("MEMORY_PROFILE", "auto", ("auto", "low", "normal"))
        limit = detect_memory_limit_bytes()
        low = (memory_profile == "low" or
               (memory_profile == "auto" and limit is not None
                and limit < 384 * 1024 * 1024))
        raw_max_as = _env_int("YTDLP_MAX_AS_MB", -1)
        # LOW — жёсткий потолок, даже если в .env скопировали дефолты из .env.example
        if low:
            max_parallel = 1
            max_queue = min(max(1, _env_int("MAX_QUEUE", 8)), 8)
            max_original_mb = min(_env_int("MAX_ORIGINAL_MB", 150), 150)
            max_slides = min(max(2, _env_int("MAX_SLIDES", 10)), 10)
            keep_files_min = min(_env_int("KEEP_FILES_MIN", 10), 10)
        else:
            max_parallel = max(1, _env_int("MAX_PARALLEL", 2))
            max_queue = max(1, _env_int("MAX_QUEUE", 40))
            max_original_mb = _env_int("MAX_ORIGINAL_MB", 400)
            max_slides = max(2, min(20, _env_int("MAX_SLIDES", 20)))
            keep_files_min = _env_int("KEEP_FILES_MIN", 30)
        return cls(
            token=_env("BOT_TOKEN"),
            mode=_env("MODE", "polling").lower(),
            webhook_url=_env("WEBHOOK_URL"),
            webhook_secret=_env("WEBHOOK_SECRET"),
            webhook_path=_env("WEBHOOK_PATH", "/tg"),
            webhook_host=_env("WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=_env_int("WEBHOOK_PORT", 8080),
            db_path=Path(_env("DB_PATH", "state.sqlite3")),
            tmp_dir=Path(_env("TMP_DIR", "tmp")),
            max_parallel=max_parallel,
            max_queue=max_queue,
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 48),
            max_original_mb=max_original_mb,
            max_image_mb=_env_int("MAX_IMAGE_MB", 9),
            max_slides=max_slides,
            inspect_timeout=_env_int("INSPECT_TIMEOUT", 70),
            download_timeout=_env_int("DOWNLOAD_TIMEOUT", 600),
            encode_timeout=_env_int("ENCODE_TIMEOUT", 600),
            keep_files_min=keep_files_min,
            cookies_file=_env("COOKIES_FILE"),
            proxy=_env("HTTPS_PROXY") or _env("HTTP_PROXY"),
            use_gallery_dl=_env_bool("USE_GALLERY_DL", True),
            ensure_callback_mode_default=_env_bool("ENSURE_CALLBACK_MODE_DEFAULT", False),
            rate_per_30s=_env_int("RATE_PER_30S", 4),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            widget_mode=_env_choice("TELEGRAM_WIDGETS", "auto", ("auto", "on", "off")),
            test_server=_env_bool("TELEGRAM_TEST_SERVER", False),
            widget_retry_min=max(1, _env_int("WIDGET_RETRY_MIN", 60)),
            ytdlp_extractor_args=_env_json("YTDLP_EXTRACTOR_ARGS"),
            isolate_inspect=_env_bool("YTDLP_ISOLATE_INSPECT", True),
            drop_pending_updates=_env_bool("DROP_PENDING_UPDATES", True),
            faulthandler_interval=max(0, _env_int("FAULTHANDLER_INTERVAL", 0)),
            ytdlp_cli_extra=_env("YTDLP_CLI_EXTRA"),
            memory_profile=memory_profile,
            memory_limit_bytes=limit,
            low_memory=low,
            ytdlp_max_as_mb=raw_max_as if raw_max_as >= 0 else (128 if low else 0),
        )

    @property
    def memory_limit_mb(self) -> int:
        return (self.memory_limit_bytes or 0) // (1024 * 1024)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_original_bytes(self) -> int:
        return self.max_original_mb * 1024 * 1024

    @property
    def max_image_bytes(self) -> int:
        return self.max_image_mb * 1024 * 1024

    @property
    def ffmpeg(self) -> str | None:
        return shutil.which("ffmpeg")

    @property
    def ffprobe(self) -> str | None:
        return shutil.which("ffprobe")

    @property
    def gallery_dl(self) -> str | None:
        return gallery_dl_bin()

    @property
    def widget_retry_seconds(self) -> int:
        return self.widget_retry_min * 60


# ---------------------------------------------------------------------------
# Виджет: валидатор бюджета (тот же счёт, что у сервера: zlib level 9)
# ---------------------------------------------------------------------------

def widget_size(html: str) -> tuple[int, int]:
    raw = html.encode("utf-8")
    return len(raw), len(zlib.compress(raw, 9))


def validate_widget_html(html: str) -> tuple[int, int]:
    raw, packed = widget_size(html)
    if not html or raw > MAX_HTML_RAW or packed > MAX_HTML_ZLIB or "\x00" in html:
        raise ValueError(f"Виджет вне бюджета: {raw} Б raw / {packed} Б zlib-9")
    for bad in ("<input", "<textarea", "<select", "<iframe", "<video", "@import", "@font-face",
                ":disabled", "gradient(", "fetch(", "XMLHttpRequest"):
        if bad in html:
            raise ValueError(f"Виджет содержит запрещённый профилем 1 элемент: {bad}")
    return raw, packed


def dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


FALLBACK_ONLY_KEYS = {"d", "h", "b", "low"}   # тех. детали только в текстовом фолбэке


def compact_state(state: dict[str, Any]) -> str:
    """chat_data <= 1024 UTF-8 байт. Резерв: сначала режем/выкидываем второстепенное."""
    d = {k: v for k, v in state.items() if k not in FALLBACK_ONLY_KEYS}
    for key, limit in (("t", 64), ("m", 70), ("e", 90), ("u", 300), ("s", 22)):
        if isinstance(d.get(key), str):
            d[key] = d[key][:limit]
    d = {k: v for k, v in d.items() if v not in (None, "", 0) or k in ("v", "p", "i", "n")}

    def dump(x: dict) -> str:
        return json.dumps(x, ensure_ascii=False, separators=(",", ":"))

    s = dump(d)
    for key in ("u", "m", "e", "s"):
        if len(s.encode("utf-8")) <= MAX_CHAT_DATA:
            break
        d.pop(key, None)
        s = dump(d)
    if len(s.encode("utf-8")) > MAX_CHAT_DATA:
        d["t"] = d.get("t", "")[:24]
        s = dump(d)
    if len(s.encode("utf-8")) > MAX_CHAT_DATA:
        # байтобезопасное усечение: кириллица/эмодзи стоят 2–4 байта
        t = d.get("t", "")
        while t and len(dump({**d, "t": t}).encode("utf-8")) > MAX_CHAT_DATA:
            t = t[:-1]
        d["t"] = t
        s = dump(d)
    return s


def idle_state() -> dict[str, Any]:
    return {
        "v": 1, "ph": "idle", "p": 0, "s": "DOWNLOADER",
        "t": "Отправь ссылку", "m": "YouTube · TikTok · Instagram · X · VK · и другие",
        "i": 0, "n": 0, "q": "720", "e": "", "u": "",
    }


# ---------------------------------------------------------------------------
# Заглушка-картинка (чистый PNG, без внешних ассетов)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _inside_round(x: int, y: int, x0: int, y0: int, x1: int, y1: int, r: int) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def write_placeholder_png(path: Path, w: int = 640, h: int = 360) -> None:
    """Тёмная «стеклянная» карточка — видна, пока идёт скачка."""
    rows = []
    for y in range(h):
        row = bytearray([0])  # filter: none
        for x in range(w):
            if _inside_round(x, y, 56, 52, w - 56, h - 52, 28):
                color = (31, 32, 36)
            else:
                color = (17, 18, 21)
            if 92 <= x <= 320 and 244 <= y <= 256 and _inside_round(x, y, 56, 52, w - 56, h - 52, 28):
                color = (58, 60, 67)
            row.extend(color)
        rows.append(bytes(row))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


# ---------------------------------------------------------------------------
# URL: нормализация, SSRF-фильтр, подписи площадок
# ---------------------------------------------------------------------------

TRAILING = ".,;:!?)]}\"'»…"


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(TRAILING)


def normalize_url(url: str) -> str:
    """Привести короткий домен к тому, что реально знает yt-dlp."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    new = SHORT_HOST_MAP.get(host)
    if not new:
        return url
    netloc = new + (f":{u.port}" if u.port else "")
    fixed = urllib.parse.urlunsplit((u.scheme or "https", netloc, u.path, u.query, u.fragment))
    log.info("нормализовал ссылку: %s -> %s", host, new)
    return fixed


def source_label(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for needle, label in SOURCE_MAP:
        if host == needle or host.endswith("." + needle):
            return label
    return host[:22] or "Ссылка"


def original_link(url: str) -> str:
    """URL для TelegramWidget.openLink: только https, не t.me, <=256 символов."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or len(url) > 256:
        return ""
    if any(host == h or host.endswith("." + h) for h in ("t.me", "telegram.me", "telegram.dog")):
        return ""
    return url


def _resolve_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False  # DNS не ответил — yt-dlp всё равно упадёт с внятной ошибкой
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


async def validate_url(url: str) -> tuple[bool, str]:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        return False, "Нужен http(s)-link"
    if u.username or u.password:
        return False, "Ссылка с логином не принимается"
    if u.port and u.port not in (80, 443):
        return False, "Нестандартный порт не принимается"
    host = (u.hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, "Локальные адреса запрещены"
    if host.endswith((".local", ".internal", ".localhost", ".home.arpa")):
        return False, "Внутренние адреса запрещены"
    if await asyncio.to_thread(_resolve_private, host):
        return False, "Приватные адреса запрещены"
    return True, ""


def friendly_error(exc: BaseException) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "unsupported url" in low or "no video formats" in low:
        return "Ссылка не поддерживается"
    if ("unable to download webpage" in low or "tls/ssl" in low
            or "connection has been closed" in low or "nameresolution" in low):
        return "Сайт не ответил (проверь доступ или прокси)"
    if "private" in low or "login" in low or "cookie" in low or "sign in" in low:
        return "Нужен доступ к посту (куки)"
    if "429" in low or "too many requests" in low or "rate limit" in low:
        return "Сервис ограничил запросы, попробуй позже"
    if ("memoryerror" in low or "cannot allocate memory" in low
            or "killed" in low or "out of memory" in low):
        return "Мало памяти на сервере, попробуй другое качество"
    if "requested format is not available" in low or "no suitable formats" in low:
        return "Нет подходящего формата, попробуй другое качество"
    if "403" in low or "401" in low or "forbidden" in low:
        return "Сервис запретил доступ (часто нужны куки)"
    if "ffmpeg" in low or "ffprobe" in low:
        return "Нужен ffmpeg для этого формата"
    if "timeout" in low or "timed out" in low or "истёк" in low:
        return "Таймаут, попробуй ещё раз"
    if "отменено" in low:
        return "Отменено"
    if "больш" in low or "too large" in low:
        return "Файл слишком большой"
    return msg[:88] + ("…" if len(msg) > 90 else "")


class FriendlyError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


def hint_for(reason: str) -> str:
    low = (reason or "").lower()
    if "памят" in low:
        return "Поставь MEMORY_PROFILE=low или дай хосту 512 МБ"
    if "сайт не ответил" in low or "таймаут" in low:
        return "Проверь доступ к площадке, HTTPS_PROXY или COOKIES_FILE"
    if "куки" in low or "доступ" in low:
        return "Нужен COOKIES_FILE с cookies.txt из браузера"
    if "формат" in low or "ffmpeg" in low:
        return "Выбери другое качество кнопкой ниже или поставь ffmpeg"
    if "ссылка не поддерживается" in low:
        return "Проверь, что ссылка полная и начинается с https://"
    if "50 мб" in low or "больш" in low:
        return "Выбери 360p или открой источник кнопкой"
    return "Попробуй «Ещё раз», другое качество или /diag"


def error_patch(exc: BaseException, detail: str = "") -> dict[str, Any]:
    raw = getattr(exc, "detail", "") or str(exc) or type(exc).__name__
    full = (raw + (f" · {detail}" if detail else ""))[:220]
    return {
        "ph": "error", "p": 0,
        "e": friendly_error(exc),
        "d": full,
        "h": hint_for(friendly_error(exc)),
    }


# ---------------------------------------------------------------------------
# Хранилище: in-memory сессии + SQLite (переживает рестарт)
# ---------------------------------------------------------------------------

@dataclass
class Session:
    chat_id: int
    widget_id: int
    media_id: int | None
    mode: str                      # "widget" | "fallback"
    state: dict[str, Any] = field(default_factory=idle_state)
    url: str = ""
    quality: str = "720"
    slides: list[dict[str, Any]] = field(default_factory=list)
    idx: int = 0
    fallback_at: float = 0.0        # НЕ обновляется push_state: TTL отказа виджета
    retired: bool = False            # карточку заменили новой — старый job молчит
    updated: float = field(default_factory=time.time)
    last_push: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id, "widget_id": self.widget_id, "media_id": self.media_id,
            "mode": self.mode, "state": self.state, "url": self.url, "quality": self.quality,
            "slides": self.slides, "idx": self.idx,
            "fallback_at": self.fallback_at, "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(
            chat_id=d["chat_id"], widget_id=d["widget_id"], media_id=d.get("media_id"),
            mode=d.get("mode", "widget"), state=d.get("state") or idle_state(),
            url=d.get("url", ""), quality=d.get("quality", "720"),
            slides=d.get("slides") or [], idx=d.get("idx", 0),
            fallback_at=float(d.get("fallback_at") or 0),
            updated=d.get("updated", time.time()),
        )


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (chat_id INTEGER, widget_id INTEGER, data TEXT, PRIMARY KEY(chat_id, widget_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, file_id TEXT, kind TEXT, ts REAL)")
        self.db.commit()
        self.sessions: dict[tuple[int, int], Session] = {}
        for (raw,) in self.db.execute("SELECT data FROM sessions"):
            try:
                s = Session.from_dict(json.loads(raw))
                self.sessions[(s.chat_id, s.widget_id)] = s
            except Exception:
                log.warning("не прочитал сессию из БД")

    def get(self, chat_id: int, widget_id: int) -> Session | None:
        return self.sessions.get((chat_id, widget_id))

    def latest(self, chat_id: int) -> Session | None:
        found = [s for (c, _), s in self.sessions.items() if c == chat_id]
        return max(found, key=lambda s: s.updated) if found else None

    def save(self, s: Session) -> None:
        s.updated = time.time()
        self.sessions[(s.chat_id, s.widget_id)] = s
        self.db.execute(
            "INSERT OR REPLACE INTO sessions(chat_id,widget_id,data) VALUES(?,?,?)",
            (s.chat_id, s.widget_id, json.dumps(s.to_dict(), ensure_ascii=False)),
        )
        self.db.commit()

    def cache_file(self, key: str, file_id: str, kind: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO files(key,file_id,kind,ts) VALUES(?,?,?,?)",
                        (key, file_id, kind, time.time()))
        self.db.commit()

    def get_file(self, key: str) -> tuple[str, str] | None:
        row = self.db.execute("SELECT file_id,kind FROM files WHERE key=?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def drop(self, s: Session) -> None:
        self.sessions.pop((s.chat_id, s.widget_id), None)
        self.db.execute("DELETE FROM sessions WHERE chat_id=? AND widget_id=?",
                        (s.chat_id, s.widget_id))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# ---------------------------------------------------------------------------
# yt-dlp / gallery-dl / media
# ---------------------------------------------------------------------------

class _Quiet:
    def debug(self, *a: Any) -> None: pass
    def info(self, *a: Any) -> None: pass
    def warning(self, *a: Any) -> None: pass
    def error(self, *a: Any) -> None: pass


def format_selector(quality: str, has_ffmpeg: bool) -> str:
    """Без ffmpeg просим ТОЛЬКО прогрессив (видео+аудио в одном файле).
    Иначе yt-dlp берёт DASH video-only и падает на FFmpegMergerPP — это и
    выглядело как «видео не качается» при отсутствии бинарника."""
    if quality == "a":
        return "bestaudio[ext=m4a]/bestaudio/best[vcodec=none]/best"
    h = int(quality) if quality.isdigit() else 720
    progressive = (
        f"best[height<={h}][vcodec!=none][acodec!=none][ext=mp4]/"
        f"best[height<={h}][vcodec!=none][acodec!=none]/"
        "best[vcodec!=none][acodec!=none]"
    )
    if not has_ffmpeg:
        return progressive
    return f"bestvideo[height<={h}]+bestaudio/{progressive}"


def ydl_opts(cfg: Config, quality: str, *, download: bool,
             dest: Path | None = None, job: "Job | None" = None) -> dict[str, Any]:
    o: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "logger": _Quiet(),
        "noplaylist": False, "ignore_no_formats_error": True,
        "playlistend": cfg.max_slides,
        "socket_timeout": 15, "retries": 2, "fragment_retries": 2, "file_access_retries": 2,
        "max_filesize": cfg.max_original_bytes,       # режется в downloader/http.py
        "concurrent_fragment_downloads": 1 if cfg.low_memory else 2,
        "overwrites": True, "restrictfilenames": True,
    }
    has_merge = bool(cfg.ffmpeg) and not cfg.low_memory
    if has_merge:                   # без ffmpeg ИЛИ при 256 МБ не просить merge/convert
        o["merge_output_format"] = "mp4"
    if cfg.ytdlp_extractor_args:
        o["extractor_args"] = cfg.ytdlp_extractor_args
    if cfg.cookies_file and Path(cfg.cookies_file).is_file():
        o["cookiefile"] = cfg.cookies_file
    if cfg.proxy:
        o["proxy"] = cfg.proxy
    if not download:
        return o
    assert dest is not None
    o["outtmpl"] = str(dest / "%(autonumber)05d-%(id)s.%(ext)s")
    o["format"] = format_selector(quality, bool(cfg.ffmpeg) and not cfg.low_memory)

    def hook(d: dict[str, Any]) -> None:
        if job is not None and job.cancel.is_set():
            raise RuntimeError("Отменено")   # не импортируем yt_dlp.utils в родителе
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if job is not None:
                job.phase = "down"
                job.percent = min(85, 12 + int(64 * got / total)) if total else 25
        elif d.get("status") == "finished" and job is not None:
            job.percent = max(job.percent, 80)

    o["progress_hooks"] = [hook]
    return o


def collect_thumbs(entry: dict[str, Any]) -> list[str]:
    """Фото-карусели Instagram/TikTok отдают картинки как thumbnails."""
    found: list[tuple[int, str]] = []
    for t in (entry.get("thumbnails") or []):
        if isinstance(t, dict) and isinstance(t.get("url"), str):
            found.append((int(t.get("width") or 0), t["url"]))
    if isinstance(entry.get("thumbnail"), str):
        found.append((0, entry["thumbnail"]))
    found.sort(key=lambda x: -x[0])
    return dedupe([u for _, u in found if u.startswith("http")])


def build_inspection(info: Any, cfg: Config) -> dict[str, Any]:
    """Общий разбор info-словаря и для библиотечного, и для CLI-режима."""
    if not isinstance(info, dict):
        return {"kind": "error", "error": "Пустой ответ", "images": [], "has_video": False}

    entries = info.get("entries")
    if entries is None:
        entries = [info]
    elif callable(entries):
        entries = list(entries)
    entries = [e for e in entries if isinstance(e, dict)][: cfg.max_slides + 2]

    images: list[str] = []
    has_video = False
    audio_only = False
    duration = info.get("duration")
    for e in entries:
        fmts = [f for f in (e.get("formats") or []) if isinstance(f, dict)]
        video_fmts = [f for f in fmts if f.get("vcodec") != "none"]
        if video_fmts:
            has_video = True
            duration = duration or e.get("duration")
        elif fmts:
            audio_only = True
        elif not has_video:
            thumbs = collect_thumbs(e)
            if thumbs:
                images.append(thumbs[0])

    title = info.get("title") or (entries[0].get("title") if entries else "") or "Без названия"
    parts: list[str] = []
    if duration:
        parts.append(f"{int(duration) // 60}:{int(duration) % 60:02d}")
    if len(entries) > 1 and has_video:
        parts.append(f"плейлист, беру 1 из {len(entries)}")
    kind = "video" if has_video else ("images" if images else ("audio" if audio_only else "error"))
    log.info("разведка: kind=%s video=%s audio=%s images=%d title=%r",
             kind, has_video, audio_only, len(images), str(title)[:60])
    return {
        "kind": kind, "images": dedupe(images)[: cfg.max_slides],
        "has_video": has_video, "audio_only": audio_only and not has_video,
        "title": str(title)[:90], "meta": " · ".join(parts),
        "duration": duration, "many": len(entries) > 1,
        "error": "" if kind != "error" else "Не нашёл медиа по ссылке",
    }


def inspect_error(msg: str, detail: str = "") -> dict[str, Any]:
    return {"kind": "error", "error": msg, "detail": detail[:300], "images": [],
            "has_video": False, "audio_only": False, "title": "", "meta": "",
            "duration": None, "many": False}


def inspect_media(url: str, quality: str, cfg: Config) -> dict[str, Any]:
    """Fallback: разведка в потоке (YTDLP_ISOLATE_INSPECT=0)."""
    log.info("разведка: поток, старт %s", url[:90])
    import yt_dlp   # ленивый импорт: только в режиме YTDLP_ISOLATE_INSPECT=0
    try:
        with yt_dlp.YoutubeDL(ydl_opts(cfg, quality, download=False)) as ydl:
            info = ydl.extract_info(url, download=False)
    except BaseException as exc:      # SystemExit из экстрактора не должен ронять бота
        log.warning("разведка не удалась (%s): %s: %s", url[:90], type(exc).__name__, exc)
        return inspect_error(friendly_error(exc))
    return build_inspection(info, cfg)


def ydl_cli_cmd(url: str, cfg: Config) -> list[str]:
    """Команда для изолированной разведки: -J = simulate + один JSON."""
    cmd = [sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", "--no-progress",
           "--no-color", "--ignore-no-formats-error", "--skip-download",
           "--playlist-end", str(cfg.max_slides), "--socket-timeout", "15", "--retries", "1"]
    if cfg.cookies_file and Path(cfg.cookies_file).is_file():
        cmd += ["--cookies", cfg.cookies_file]
    if cfg.proxy:
        cmd += ["--proxy", cfg.proxy]
    if cfg.low_memory:
        cmd += ["--concurrent-fragments", "1", "--extractor-retries", "0"]
    if cfg.ytdlp_cli_extra:
        try:
            cmd += shlex.split(cfg.ytdlp_cli_extra)
        except ValueError:
            log.warning("YTDLP_CLI_EXTRA: невалидная строка, игнорирую")
    cmd.append(url)
    return cmd


def wrap_memory_limit(cmd: list[str], cfg: Config) -> list[str]:
    """Жёсткий RLIMIT_AS для дочернего yt-dlp через shell, НЕ через preexec_fn
    (тот небезопасен в многопоточном asyncio-процессе). При превышении падает
    ребёнок, а не весь бот/контейнер."""
    mb = cfg.ytdlp_max_as_mb
    if mb <= 0 or os.name != "posix":
        return cmd
    kb = mb * 1024
    return ["/bin/sh", "-c",
            f'ulimit -v {kb} 2>/dev/null || true; exec "$@"', "sh", *cmd]


def inspect_media_cli(url: str, quality: str, cfg: Config) -> dict[str, Any]:
    """Разведка в ОТДЕЛЬНОМ ПРОЦЕССЕ.

    asyncio.to_thread + wait_for не убивает зависший поток: после рестарта
    бот получал те же pending-апдейты и уходил в тот же тупик. subprocess.run
    с timeout гарантированно kill'ит дочерний yt-dlp, а segfault/OOM внутри
    него больше не валит сам бот.
    """
    cmd = wrap_memory_limit(ydl_cli_cmd(url, cfg), cfg)
    started = time.monotonic()
    log.info("разведка: subprocess старт, timeout=%d с, rss=%d МБ, as_limit=%d МБ, url=%s",
             cfg.inspect_timeout, current_rss_mb(), cfg.ytdlp_max_as_mb, url[:90])
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=cfg.inspect_timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        log.warning("разведка: subprocess ТАЙМАУТ %d с — процесс убит", cfg.inspect_timeout)
        return inspect_error("Таймаут разведки, попробуй ещё раз")
    except BaseException as exc:
        log.warning("разведка: subprocess не запустился: %s: %s", type(exc).__name__, exc)
        return inspect_error(friendly_error(exc))

    err = (r.stderr or "").strip()
    log.info("разведка: subprocess rc=%d за %.1f с, stdout=%d Б, parent rss=%d МБ, "
             "as_limit=%d МБ, stderr=%r",
             r.returncode, time.monotonic() - started, len(r.stdout or ""),
             current_rss_mb(), cfg.ytdlp_max_as_mb, err[-160:])
    # yt-dlp с -J может вернуть rc=0 и напечатать "null" при фатальной ошибке
    if r.returncode != 0 or "ERROR:" in err or (r.stdout or "").strip() in ("", "null"):
        detail = next((ln for ln in err.splitlines() if "ERROR:" in ln), err[-200:] or "Пустой ответ")
        clean = detail.replace("ERROR: ", "", 1)
        return inspect_error(friendly_error(Exception(clean)), clean)
    try:
        info = json.loads(r.stdout)
    except (ValueError, TypeError) as exc:
        log.warning("разведка: невалидный JSON: %s", exc)
        return inspect_error("Не прочитал ответ экстрактора")
    return build_inspection(info, cfg)


def _dedupe_images(urls: list[str]) -> list[str]:
    """У TikTok один слайд приходит с двух CDN-зеркал — считаем по имени файла."""
    seen, out = set(), []
    for u in urls:
        key = urllib.parse.urlparse(u).path.rsplit("/", 1)[-1] or u
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _looks_image(u: str) -> bool:
    return bool(IMAGE_URL_RE.search(u)) or any(
        x in u for x in ("photomode", "tiktokcdn", "cdninstagram", "pinimg", "/image/")
    )


def _find_image_urls(obj: Any, out: list[str], depth: int = 0) -> None:
    if depth > 14 or len(out) >= 60:
        return
    if isinstance(obj, dict):
        ul = obj.get("urlList")
        if isinstance(ul, list):
            for u in ul:
                if isinstance(u, str) and u.startswith("http") and _looks_image(u):
                    out.append(u)
        for v in obj.values():
            _find_image_urls(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _find_image_urls(v, out, depth + 1)


async def tiktok_images(svc: "Service", url: str) -> list[str]:
    """yt-dlp не умеет photo-карусели TikTok (только звук) -> читаем hydration JSON."""
    try:
        async with svc.http.get(url, headers=BROWSER_HEADERS,
                                timeout=ClientTimeout(total=25), allow_redirects=True) as r:
            if r.status != 200:
                return []
            text = (await r.content.read(4 * 1024 * 1024)).decode("utf-8", "ignore")
    except Exception as exc:
        log.debug("tiktok page: %s", exc)
        return []
    for sid in ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "__NEXT_DATA__"):
        m = re.search(r"<script[^>]*id=['\"]" + re.escape(sid) + r"['\"][^>]*>(.*?)</script>", text, re.S | re.I)
        if not m:
            continue
        try:
            data = json.loads(html_lib.unescape(m.group(1)))
        except Exception:
            continue
        urls: list[str] = []
        _find_image_urls(data, urls)
        if urls:
            return _dedupe_images(urls)[: svc.cfg.max_slides]
    # запасной вариант: хотя бы обложка
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', text, re.I)
    return [html_lib.unescape(m.group(1))] if m else []


async def gallery_dl_images(url: str, cfg: Config) -> list[str]:
    """Опциональный второй источник каруселей (TikTok / Instagram / Pinterest)."""
    exe = cfg.gallery_dl
    if not cfg.use_gallery_dl or cfg.low_memory or not exe:
        return []

    def run() -> list[str]:
        try:
            r = subprocess.run(
                [exe, "-g", "--range", f"1-{cfg.max_slides}", url],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            return []
        urls = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("http")]
        return [u for u in urls if _looks_image(u)]

    return _dedupe_images(await asyncio.to_thread(run))[: cfg.max_slides]


async def inspect_plan(svc: "Service", url: str, quality: str) -> dict[str, Any]:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if IMAGE_URL_RE.search(url):
        return {"kind": "single", "images": [url], "title": "Изображение", "meta": source_label(url)}
    inspector = inspect_media_cli if svc.cfg.isolate_inspect else inspect_media
    try:
        meta = await asyncio.wait_for(
            asyncio.to_thread(inspector, url, quality, svc.cfg),
            timeout=svc.cfg.inspect_timeout + 10,
        )
    except asyncio.TimeoutError:
        log.warning("план: разведка зависла дольше %d с: url=%s", svc.cfg.inspect_timeout, url[:100])
        meta = inspect_error("Таймаут разведки, попробуй ещё раз")
    log.info("план: host=%s kind=%s video=%s audio=%s err=%r", host or "?",
             meta.get("kind"), meta.get("has_video"), meta.get("audio_only"), meta.get("error"))
    images = list(meta.get("images") or [])
    if "tiktok" in host and (meta.get("audio_only") or len(images) < 2):
        extra = await tiktok_images(svc, url)
        if len(extra) > len(images):
            images = extra
    if len(images) < 2 and any(k in host for k in ("tiktok", "instagram", "pinterest")):
        extra = await gallery_dl_images(url, svc.cfg)
        if len(extra) > len(images):
            images = extra
    if len(images) > 1:
        return {"kind": "carousel", "images": images[: svc.cfg.max_slides],
                "title": meta.get("title", "Карусель"), "meta": f"{len(images)} фото"}
    if len(images) == 1:
        return {"kind": "single", "images": images, "title": meta.get("title", "Фото"), "meta": source_label(url)}
    if meta.get("has_video"):
        return {"kind": "video", "images": [], "title": meta.get("title", "Видео"),
                "meta": meta.get("meta", ""), "many": meta.get("many", False)}
    if meta.get("audio_only"):
        return {"kind": "audio", "images": [], "title": meta.get("title", "Аудио"), "meta": meta.get("meta", "")}
    reason = meta.get("error") or "Не нашёл медиа по ссылке"
    log.warning("план: медиа не найдено (%s) url=%s", reason, url[:100])
    raise FriendlyError(reason, meta.get("detail") or "")


def download_media(url: str, quality: str, cfg: Config, job: "Job", chat_id: int, wid: int,
                   *, single: bool = False) -> tuple[Path, Path, dict[str, Any]]:
    dest = cfg.tmp_dir / f"v-{chat_id}-{wid}-{int(time.time() * 1000)}"
    dest.mkdir(parents=True, exist_ok=True)
    opts = ydl_opts(cfg, quality, download=True, dest=dest, job=job)
    if single:
        opts["playlist_items"] = "1"
    import yt_dlp   # ленивый импорт: разведка в subprocess не грузит его в родителя
    log.info("скачка: старт q=%s ffmpeg=%s format=%s rss=%d МБ",
             quality, bool(cfg.ffmpeg), opts.get("format"), current_rss_mb())
    started = time.monotonic()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        log.warning("скачка не удалась (%s): %s: %s", url[:90], type(exc).__name__, exc)
        shutil.rmtree(dest, ignore_errors=True)
        raise FriendlyError(friendly_error(exc))
    info = info if isinstance(info, dict) else {}
    # После merge итоговый mp4 лежит в info["filepath"], а requested_downloads
    # всё ещё указывает на video-only/audio-only куски. Берём именно итог,
    # иначе уезжает «видео без звука» или вообще исходник без контейнера.
    files: list[Path] = []
    primary: Path | None = None
    for raw in (info.get("filepath"), info.get("_filename")):
        if isinstance(raw, str) and Path(raw).is_file():
            primary = Path(raw)
            files.append(primary)
            break
    for f in (info.get("requested_downloads") or []):
        if not isinstance(f, dict):
            continue
        for k in ("filepath", "filename"):
            p = f.get(k)
            if isinstance(p, str) and Path(p).is_file():
                files.append(Path(p))
    if not files:
        files = [p for p in dest.iterdir() if p.is_file()
                 and not p.name.endswith((".part", ".ytdl", ".temp", ".tmp"))]
    files = list(dict.fromkeys(files))
    if not files:
        shutil.rmtree(dest, ignore_errors=True)
        raise FriendlyError("Не удалось получить файл")
    if primary is not None:
        path = primary                       # итог после merge, даже если исходник крупнее
    else:
        mp4 = [p for p in files if p.suffix.lower() == ".mp4"]
        path = max(mp4 or files, key=lambda p: p.stat().st_size)
    log.info("скачка: готово за %.1f с, файлов=%d, выбран %s (%d Б)",
             time.monotonic() - started, len(files), path.name, path.stat().st_size)
    if path.stat().st_size > cfg.max_original_bytes:
        raise FriendlyError("Оригинал слишком большой")
    job.percent = 82
    return path, dest, {
        "duration": (info or {}).get("duration"),
        "width": (info or {}).get("width"),
        "height": (info or {}).get("height"),
    }


def ffprobe_duration(path: Path, cfg: Config) -> float | None:
    if not cfg.ffprobe:
        return None
    try:
        r = subprocess.run(
            [cfg.ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def transcode(path: Path, dest: Path, cfg: Config, quality: str, duration: float | None) -> Path:
    """Жмём под <48 МБ, считая битрейт из длительности. Всё в списке аргументов."""
    if not cfg.ffmpeg:
        raise FriendlyError("Файл больше 50 МБ, а ffmpeg не найден")
    dur = max(1.0, float(duration or ffprobe_duration(path, cfg) or 60))
    target = cfg.max_upload_bytes
    audio_bitrate = 128_000
    video_bitrate = max(200_000, int(target * 8 / dur) - audio_bitrate)
    height = min(int(quality) if quality.isdigit() else 720, 1080)
    out = dest / "compressed.mp4"

    def lower_priority() -> None:
        try:
            if hasattr(os, "nice"):
                os.nice(10)
        except Exception:
            pass

    cmd = [
        cfg.ffmpeg, "-y", "-nostdin", "-i", str(path),
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
        "-b:v", str(video_bitrate), "-maxrate", str(int(video_bitrate * 1.3)),
        "-bufsize", str(video_bitrate * 2),
        "-vf", f"scale=-2:min({height},ih)",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-fs", str(target), str(out),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=cfg.encode_timeout,
                       preexec_fn=lower_priority if hasattr(os, "nice") else None)
    except Exception as exc:
        raise FriendlyError(f"Не удалось пережать: {friendly_error(exc)}")
    if not out.is_file() or out.stat().st_size < 10_000:
        raise FriendlyError("Перекодировка не дала файл")
    return out


async def fetch_images(svc: "Service", urls: list[str], chat_id: int, wid: int) -> list[dict[str, Any]]:
    dest = svc.cfg.tmp_dir / f"c-{chat_id}-{wid}-{int(time.time() * 1000)}"
    dest.mkdir(parents=True, exist_ok=True)
    limit = min(20 * 1024 * 1024, svc.cfg.max_original_bytes)
    sem = asyncio.Semaphore(2 if svc.cfg.low_memory else 4)

    async def one(i: int, u: str) -> dict[str, Any] | None:
        host = (urllib.parse.urlparse(u).hostname or "").lower()
        headers = dict(BROWSER_HEADERS)
        headers["Referer"] = "https://www.instagram.com/" if "instagram" in host else "https://www.tiktok.com/"
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        try:
            async with sem:
                async with svc.http.get(u, headers=headers, timeout=ClientTimeout(total=30),
                                        allow_redirects=True) as r:
                    if r.status != 200:
                        return None
                    ct = r.headers.get("Content-Type", "")
                    data = await r.content.read(limit + 1)
            if len(data) > limit or not data:
                return None
            ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ""
            if ext not in PHOTO_EXTS:
                m = IMAGE_URL_RE.search(urllib.parse.urlparse(u).path)
                ext = (m.group(0).split("?")[0] if m else ".jpg")
            if ext == ".jpe":
                ext = ".jpg"
            p = dest / f"{i:03d}{ext}"
            p.write_bytes(data)
            return {"path": str(p), "kind": classify_path(p)}
        except Exception as exc:
            log.debug("image %s: %s", u, exc)
            return None

    results = await asyncio.gather(*(one(i, u) for i, u in enumerate(urls[: svc.cfg.max_slides])))
    slides = [x for x in results if x]
    if not slides:
        shutil.rmtree(dest, ignore_errors=True)
        raise FriendlyError("Не удалось скачать фото")
    return slides


def classify_path(path: Path, quality: str = "720") -> str:
    ext = path.suffix.lower()
    if quality == "a" or ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        # sendVideo принимает только mp4; webm/mkv/mov сразу отдаём документом
        return "video" if ext == ".mp4" else "document"
    if ext in PHOTO_EXTS and (not path.exists() or path.stat().st_size <= 10 * 1024 * 1024):
        return "photo"
    return "document"


def media_object(kind: str, obj: Any, info: dict[str, Any] | None = None) -> Any:
    info = info or {}
    if kind == "photo":
        return InputMediaPhoto(media=obj)
    if kind == "audio":
        return InputMediaAudio(media=obj)
    if kind == "video":
        return InputMediaVideo(
            media=obj, supports_streaming=True,
            width=info.get("width"), height=info.get("height"),
            duration=int(info["duration"]) if info.get("duration") else None,
        )
    return InputMediaDocument(media=obj)


def extract_file_id(msg: Any) -> str | None:
    if not isinstance(msg, Message):
        return None
    for attr in ("photo", "video", "audio", "document", "animation"):
        val = getattr(msg, attr, None)
        if val:
            return val[-1].file_id if isinstance(val, list) else val.file_id
    return None


def media_key(url: str, quality: str) -> str:
    return hashlib.sha256(f"{url}|{quality}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Состояние / доставка
# ---------------------------------------------------------------------------

@dataclass
class Job:
    widget_id: int
    url: str = ""
    quality: str = ""
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    percent: int = 0
    phase: str = "queued"
    done: bool = False


class Service:
    def __init__(self, cfg: Config, bot: Bot, store: Store, widget_html: str, placeholder: Path):
        self.cfg = cfg
        self.bot = bot
        self.store = store
        self.widget_html = widget_html
        self.placeholder = placeholder
        self.sem = asyncio.Semaphore(1 if cfg.low_memory else cfg.max_parallel)
        self.jobs: dict[tuple[int, int], Job] = {}
        self.pending = 0
        self.pending_lock = asyncio.Lock()
        self.rate: dict[int, deque[float]] = defaultdict(deque)
        self.http: ClientSession | None = None
        self.started_at: float = time.time()
        cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
        write_placeholder_png(placeholder)

    async def start(self) -> None:
        self.http = ClientSession(
            headers={"User-Agent": BROWSER_HEADERS["User-Agent"]},
            timeout=ClientTimeout(total=40),
            connector=TCPConnector(limit=4 if self.cfg.low_memory else 8, ttl_dns_cache=300),
        )

    async def close(self) -> None:
        if self.http:
            await self.http.close()
        self.store.close()

    def allow_user(self, user_id: int) -> bool:
        dq = self.rate[user_id]
        now = time.monotonic()
        while dq and now - dq[0] > 30:
            dq.popleft()
        if len(dq) >= self.cfg.rate_per_30s:
            return False
        dq.append(now)
        return True


PHASES = (("meta", "Разбор"), ("down", "Скачка"), ("encode", "Обработка"), ("up", "Отправка"))
PHASE_INDEX = {"idle": -1, "queued": 0, "meta": 0, "down": 1, "encode": 2,
               "up": 3, "done": 4, "carousel": 4, "error": -2}
PHASE_TITLE = {"idle": "Готов к работе", "queued": "В очереди", "meta": "Разбираю ссылку",
               "down": "Скачиваю", "encode": "Обрабатываю", "up": "Отправляю в чат",
               "done": "Готово", "carousel": "Карусель", "error": "Не получилось"}
HELP_TEXT = ("YouTube, TikTok, Instagram/Reels, X, VK, Reddit, Twitch, Pinterest, "
             "Rutube, SoundCloud, Likee и всё, что умеет yt-dlp. Просто пришли ссылку.")


def progress_bar(p: int, width: int = 18) -> str:
    p = max(0, min(100, int(p or 0)))
    filled = round(p * width / 100)
    return "█" * filled + "░" * (width - filled)


def render_fallback(st: dict[str, Any]) -> str:
    """Полноценная карточка: прогресс, этапы, детали и подсказка. Не «полтора предложения»."""
    ph = str(st.get("ph", "idle"))
    p = int(st.get("p") or 0)
    src = str(st.get("s") or "DOWNLOADER")
    err = str(st.get("e") or "")
    if ph == "error":
        p = 0
    cur = PHASE_INDEX.get(ph, -1)

    def step(i: int, name: str) -> str:
        if cur >= 4 or (cur >= 0 and i < cur):
            return "✓ " + name
        if i == cur:
            return "▶ " + name
        return "· " + name

    lines: list[str] = [
        f"🎬  {src}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"{'⚠' if ph == 'error' else '▶'}  {PHASE_TITLE.get(ph, ph)}"
        + (f" · {p}%" if ph not in ("idle", "error", "done", "carousel") else ""),
        f"[{progress_bar(p)}]  {p}%",
        "  ".join(step(i, name) for i, (_, name) in enumerate(PHASES)),
    ]
    body = [x for x in (str(st.get("t") or ""), str(st.get("m") or "")) if x]
    if ph == "error" and body and body[-1] == idle_state().get("m"):
        body[-1] = ("↗ " + str(st.get("u") or ""))[:110] if st.get("u") else ""
        body = [x for x in body if x]
    q = str(st.get("q") or "720")
    meta_line = f"⚙ {q if q != 'a' else 'Аудио'}"
    if st.get("b"):
        meta_line += f" · {st['b']}"
    n = int(st.get("n") or 0)
    if n > 1:
        meta_line += f" · слайд {int(st.get('i') or 0) + 1}/{n}"
    body.append(meta_line)
    if body:
        lines += ["──────────────────────"] + body
    if err:
        lines += ["──────────────────────", "⚠ " + err]
        if st.get("d"):
            lines.append("↳ " + str(st["d"])[:220])
        lines.append("💡 " + (str(st.get("h") or hint_for(err))))
    elif ph == "idle":
        lines += ["──────────────────────", "💡 " + HELP_TEXT]
    lines += ["──────────────────────",
              f"yt-dlp {ytdlp_version()} · ffmpeg {'✓' if shutil.which('ffmpeg') else '✗'} · "
              f"{'LOW' if st.get('low') else 'normal'} · /diag"]
    return "\n".join(lines)[:1500]


def fallback_kb(st: dict[str, Any]) -> InlineKeyboardMarkup:
    q = str(st.get("q", "720"))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=("✓ " if q == x else "") + label,
                              callback_data="q:" + x)
         for x, label in (("360", "360p"), ("720", "720p"), ("1080", "1080p"), ("a", "🎵 mp3"))]
    ]
    if int(st.get("n") or 0) > 1:
        rows.append([
            InlineKeyboardButton(text="‹ назад", callback_data="p"),
            InlineKeyboardButton(text=f"{int(st.get('i') or 0) + 1}/{st['n']}", callback_data="noop"),
            InlineKeyboardButton(text="вперёд ›", callback_data="n"),
        ])
    rows.append([
        InlineKeyboardButton(text="🔁 Ещё раз", callback_data="r"),
        InlineKeyboardButton(text="✕ Отмена", callback_data="x"),
    ])
    link = st.get("u")
    extra: list[InlineKeyboardButton] = [InlineKeyboardButton(text="❓ Помощь", callback_data="h"),
                                         InlineKeyboardButton(text="🩺 Диагностика", callback_data="g")]
    if isinstance(link, str) and link.startswith("https://") and len(link) <= 256:
        extra.append(InlineKeyboardButton(text="↗ Источник", url=link))
    rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _do_push(svc: Service, s: Session, data: str) -> None:
    if s.mode == "widget":
        await svc.bot.edit_message_widget_data(chat_id=s.chat_id, message_id=s.widget_id, chat_data=data)
    else:
        try:
            await svc.bot.edit_message_text(text=render_fallback(s.state), chat_id=s.chat_id,
                                            message_id=s.widget_id, reply_markup=fallback_kb(s.state))
        except TelegramBadRequest as exc:
            low = str(exc).lower()
            if not any(x in low for x in ("message to edit not found", "message_id_invalid",
                                          "message can't be edited", "message to edit not found")):
                raise
            log.info("карточка удалена пользователем — пересоздаю (chat=%s)", s.chat_id)
            msg = await svc.bot.send_message(chat_id=s.chat_id, text=render_fallback(s.state),
                                             reply_markup=fallback_kb(s.state))
            s.widget_id = msg.message_id
            s.fallback_at = s.fallback_at or time.time()
            svc.store.save(s)


async def push_state(svc: Service, s: Session, patch: dict[str, Any] | None = None,
                     *, force: bool = True) -> None:
    if getattr(s, "retired", False):
        return                         # карточку заменили — не плодим дубли
    if patch:
        s.state.update(patch)
    s.state["v"] = (int(s.state.get("v", 0)) + 1) % 1_000_000
    now = time.monotonic()
    if not force and now - s.last_push < PUSH_MIN_INTERVAL:
        return
    data = compact_state(s.state)
    s.last_push = now
    try:
        await _do_push(svc, s, data)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(min(float(exc.retry_after) + 0.2, 4))
        try:
            await _do_push(svc, s, data)
        except TelegramAPIError as exc2:
            log.warning("push_state retry: %s", exc2)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("push_state: %s", exc)
    except TelegramAPIError as exc:
        log.warning("push_state: %s", exc)
    svc.store.save(s)


async def ensure_card(svc: Service, chat_id: int, *, fresh: bool = False) -> Session:
    """Карточка с ответом. В фолбэке по /start и каждой ссылке создаётся НОВОЕ
    сообщение внизу чата: старое «далеко в начале переписки» больше не
    редактируется. Старая сессия помечается retired, чтобы её джоб молчал.

    Отказ Telegram не кешируется насовсем: проба sendWidget раз в
    WIDGET_RETRY_MIN минут; между пробами сразу шлём текстовую карточку.
    """
    cfg = svc.cfg
    old = svc.store.latest(chat_id)
    carry_media: int | None = None
    carry_fallback_at = 0.0
    if old:
        retry_due = (old.mode == "fallback" and old.fallback_at > 0
                     and time.time() - old.fallback_at > cfg.widget_retry_seconds)
        if fresh and old.mode == "fallback":
            carry_media, carry_fallback_at = old.media_id, old.fallback_at
            log.info("новая карточка внизу чата: chat=%s старая=%s", chat_id, old.widget_id)
            old.retired = True
            for key, job in list(svc.jobs.items()):
                if key[0] == chat_id:
                    job.cancel.set()
                    if job.task:
                        job.task.cancel()
                    svc.jobs.pop(key, None)
            try:
                await svc.bot.delete_message(chat_id=chat_id, message_id=old.widget_id)
            except TelegramAPIError:
                pass
            svc.store.drop(old)
        elif not (cfg.widget_mode == "auto" and retry_due):
            return old
        else:
            log.info("повторная попытка sendWidget: chat=%s (отказ %.0f с назад)",
                     chat_id, time.time() - old.fallback_at)
            try:
                await svc.bot.delete_message(chat_id=chat_id, message_id=old.widget_id)
            except TelegramAPIError:
                pass
            svc.store.drop(old)

    state = idle_state()
    state["low"] = cfg.low_memory
    probe = (cfg.widget_mode == "on" or
             (cfg.widget_mode == "auto" and
              (carry_fallback_at == 0 or
               time.time() - carry_fallback_at > cfg.widget_retry_seconds)))
    media = None
    if probe:
        try:
            media = await svc.bot.send_photo(chat_id=chat_id, photo=FSInputFile(svc.placeholder),
                                             disable_notification=True)
            w = await svc.bot.send_widget(
                chat_id=chat_id, html=svc.widget_html, height=WIDGET_HEIGHT,
                chat_data=compact_state(state), caption=WIDGET_CAPTION,
                show_caption_above_media=True, allow_queries=True, is_shared=False,
                disable_notification=True,
            )
            s = Session(chat_id=chat_id, widget_id=w.message_id, media_id=media.message_id,
                        mode="widget", state=state)
            svc.store.save(s)
            log.info("виджет отправлен: chat=%s msg=%s", chat_id, w.message_id)
            return s
        except (TelegramBadRequest, TelegramAPIError) as exc:
            text = str(exc)
            carry_fallback_at = time.time()
            if "INPUT_MEDIA_WIDGET_NOT_ALLOWED" in text.upper():
                log.warning(
                    "Telegram отказал в виджете: INPUT_MEDIA_WIDGET_NOT_ALLOWED. "
                    "С 13.11.2026 обычным ботам в проде виджеты закрыты (tdlib/telegram-bot-api#947). "
                    "Следующая проба через %d мин; полный UI — TELEGRAM_TEST_SERVER=1.",
                    cfg.widget_retry_min)
            else:
                log.warning("sendWidget не удался (%s): %s", type(exc).__name__, text)
            if media is not None:
                try:
                    await svc.bot.delete_message(chat_id=chat_id, message_id=media.message_id)
                except TelegramAPIError:
                    pass
    else:
        log.info("виджет не пробуем: throttle %d мин / TELEGRAM_WIDGETS=%s",
                 cfg.widget_retry_min, cfg.widget_mode)
    msg = await svc.bot.send_message(chat_id=chat_id, text=render_fallback(state),
                                     reply_markup=fallback_kb(state))
    s = Session(chat_id=chat_id, widget_id=msg.message_id, media_id=carry_media,
                mode="fallback", state=state,
                fallback_at=carry_fallback_at or time.time())
    svc.store.save(s)
    log.info("карточка: chat=%s msg=%s mode=fallback", chat_id, msg.message_id)
    return s


async def edit_media_with_retry(svc: Service, s: Session, media: Any) -> Any:
    for attempt in range(3):
        try:
            return await svc.bot.edit_message_media(media=media, chat_id=s.chat_id, message_id=s.media_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(float(exc.retry_after) + 0.3, 5))
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            # экзотический контейнер: Telegram отказал — пробуем отдать документом
            if not isinstance(media, InputMediaDocument) and not isinstance(media.media, str):
                try:
                    return await svc.bot.edit_message_media(
                        media=InputMediaDocument(media=media.media),
                        chat_id=s.chat_id, message_id=s.media_id,
                    )
                except TelegramAPIError:
                    pass
            raise
    return None


async def send_new_media(svc: Service, s: Session, media: Any) -> Message:
    obj = media.media
    try:
        if isinstance(media, InputMediaPhoto):
            return await svc.bot.send_photo(chat_id=s.chat_id, photo=obj, disable_notification=True)
        if isinstance(media, InputMediaVideo):
            return await svc.bot.send_video(chat_id=s.chat_id, video=obj, supports_streaming=True,
                                            disable_notification=True)
        if isinstance(media, InputMediaAudio):
            return await svc.bot.send_audio(chat_id=s.chat_id, audio=obj, disable_notification=True)
    except TelegramBadRequest:
        if isinstance(obj, str):
            raise
    return await svc.bot.send_document(chat_id=s.chat_id, document=obj, disable_notification=True)


async def deliver_media(svc: Service, s: Session, path: Path, dest: Path, quality: str,
                        info: dict[str, Any]) -> None:
    size = path.stat().st_size
    if size > svc.cfg.max_upload_bytes:
        await push_state(svc, s, {"ph": "encode", "p": 90})
        if svc.cfg.ffmpeg and not svc.cfg.low_memory:
            try:
                path = await asyncio.wait_for(
                    asyncio.to_thread(transcode, path, dest, svc.cfg, quality, info.get("duration")),
                    timeout=svc.cfg.encode_timeout + 10,
                )
                size = path.stat().st_size
            except Exception as exc:
                log.warning("пережатие не удалось: %s: %s", type(exc).__name__, exc)
        if size > 50 * 1024 * 1024:
            # «extreme = link»: не валим бота загрузкой, даём кнопку на источник
            await push_state(svc, s, {
                "ph": "error", "p": 0,
                "e": ("Мало памяти для пережатия — открой источник кнопкой ниже"
                      if svc.cfg.low_memory else
                      "Файл больше 50 МБ — открой источник кнопкой ниже"),
                "d": f"размер {size // (1024 * 1024)} МБ > лимита загрузки 50 МБ",
                "h": "Выбери 360p или открой источник кнопкой",
                "u": original_link(s.url),
            })
            shutil.rmtree(dest, ignore_errors=True)
            return
    key = media_key(s.url, quality)
    cached = svc.store.get_file(key)
    kind = cached[1] if cached else classify_path(path, quality)
    obj = cached[0] if cached else FSInputFile(path)
    media = media_object(kind, obj, info)
    if s.media_id:
        msg = await edit_media_with_retry(svc, s, media)
    else:
        msg = await send_new_media(svc, s, media)
        s.media_id = msg.message_id
    fid = extract_file_id(msg)
    if fid and not cached:
        svc.store.cache_file(key, fid, kind)
    log.info("доставка: kind=%s size=%d Б media_id=%s", kind, size, s.media_id)
    shutil.rmtree(dest, ignore_errors=True)


async def show_slide(svc: Service, s: Session, idx: int) -> None:
    if not s.slides:
        return
    idx %= len(s.slides)
    async with s.lock:
        slide = s.slides[idx]
        path = Path(slide["path"])
        cached = slide.get("file_id")
        if not path.is_file() and not cached:
            raise FriendlyError("Файлы карусели уже удалены, повтори ссылку")
        obj = cached or FSInputFile(path)
        media = media_object(slide.get("kind", "photo"), obj)
        if s.media_id:
            msg = await edit_media_with_retry(svc, s, media)
        else:
            msg = await send_new_media(svc, s, media)
            s.media_id = msg.message_id
        fid = extract_file_id(msg)
        if fid and not cached:
            slide["file_id"] = fid
        s.idx = idx
        svc.store.save(s)


def cleanup_old(svc: Service) -> None:
    cutoff = time.time() - svc.cfg.keep_files_min * 60
    for d in svc.cfg.tmp_dir.iterdir():
        if not d.is_dir() or not d.name.startswith(("v-", "c-")):
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Фоновая задача
# ---------------------------------------------------------------------------

async def progress_reporter(svc: Service, s: Session, job: Job) -> None:
    last = (-1, "")
    try:
        while not job.done:
            await asyncio.sleep(PUSH_MIN_INTERVAL)
            cur = (job.percent, job.phase)
            if cur != last and not job.done:
                last = cur
                await push_state(svc, s, {"p": job.percent, "ph": job.phase}, force=False)
    except asyncio.CancelledError:
        pass


async def run_job(svc: Service, s: Session, url: str, quality: str, job: Job) -> None:
    started = time.monotonic()
    log.info("job старт: chat=%s q=%s url=%s", s.chat_id, quality, url[:100])
    reporter = asyncio.create_task(progress_reporter(svc, s, job))
    try:
        async with svc.sem:
            if job.cancel.is_set():
                raise asyncio.CancelledError
            await push_state(svc, s, {"ph": "meta", "p": 5}, force=True)
            job.phase, job.percent = "meta", 5
            plan = await inspect_plan(svc, url, quality)
            log.info("job план: %s · %s (%.1f с)", plan["kind"],
                     str(plan.get("title", ""))[:60], time.monotonic() - started)

            if plan["kind"] in ("carousel", "single"):
                await push_state(svc, s, {
                    "ph": "down", "p": 15, "t": plan["title"], "m": plan["meta"],
                    "n": len(plan["images"]), "i": 0,
                })
                job.phase, job.percent = "down", 35
                slides = await fetch_images(svc, plan["images"], s.chat_id, s.widget_id)
                job.percent = 85
                if job.cancel.is_set():
                    raise asyncio.CancelledError
                s.slides, s.idx = slides, 0
                svc.store.save(s)
                await show_slide(svc, s, 0)
                await push_state(svc, s, {
                    "ph": "carousel" if len(slides) > 1 else "done", "p": 100,
                    "n": len(slides), "i": 0,
                    "m": (f"{len(slides)} фото · листай стрелками" if len(slides) > 1 else plan["meta"]),
                })
                return

            # видео / аудио
            await push_state(svc, s, {"ph": "down", "p": 12, "t": plan["title"], "m": plan["meta"]})
            job.phase, job.percent = "down", 12
            path, dest, info = await asyncio.wait_for(
                asyncio.to_thread(download_media, url, quality, svc.cfg, job, s.chat_id, s.widget_id,
                                  single=bool(plan.get("many"))),
                timeout=svc.cfg.download_timeout,
            )
            if job.cancel.is_set():
                raise asyncio.CancelledError
            job.phase, job.percent = "up", 88
            await push_state(svc, s, {"ph": "up", "p": 88})
            log.info("job: качка завершена за %.1f с, отдаём в чат", time.monotonic() - started)
            await deliver_media(svc, s, path, dest, quality, info)
            await push_state(svc, s, {"ph": "done", "p": 100, "m": plan["meta"] or "Готово · выбери качество"})
            log.info("job готово за %.1f с: chat=%s", time.monotonic() - started, s.chat_id)

    except asyncio.CancelledError:
        log.info("job отменён: chat=%s", s.chat_id)
        try:
            await push_state(svc, s, {"ph": "idle", "p": 0, "e": "", "d": "", "h": "", "b": "",
                                      "t": "Отправь ссылку"})
        except (asyncio.CancelledError, Exception):
            pass
    except asyncio.TimeoutError:
        log.warning("job ТАЙМАУТ за %.1f с: chat=%s url=%s",
                    time.monotonic() - started, s.chat_id, url[:100])
        await push_state(svc, s, error_patch(Exception(
            f"Таймаут {svc.cfg.inspect_timeout} с: разведка или скачка не ответила")))
    except (SystemExit, KeyboardInterrupt) as exc:
        # yt-dlp в to_thread теоретически может бросить SystemExit; asyncio.Task
        # пробрасывает его в event loop и роняет ВЕСЬ процесс — гасим здесь
        log.error("job прерван системным исключением %s: %r", type(exc).__name__, exc)
        try:
            await push_state(svc, s, error_patch(exc))
        except Exception:
            pass
    except Exception as exc:
        log.exception("job failed за %.1f с: %s: %s",
                      time.monotonic() - started, type(exc).__name__, exc)
        if "Отменено" not in str(exc):
            await push_state(svc, s, error_patch(exc))
    finally:
        job.done = True
        reporter.cancel()
        async with svc.pending_lock:
            svc.pending = max(0, svc.pending - 1)
        key = (s.chat_id, s.widget_id)
        if svc.jobs.get(key) is job:      # не удаляем задачу-преемника
            svc.jobs.pop(key, None)
        try:
            await asyncio.to_thread(cleanup_old, svc)
        except (asyncio.CancelledError, Exception):
            pass


async def start_job(svc: Service, s: Session, url: str, quality: str) -> None:
    if not url:
        return
    key = (s.chat_id, s.widget_id)
    old = svc.jobs.get(key)
    if old and not old.done:
        if old.url == url and old.quality == quality:
            # рестарт с drop_pending_updates=False присылал один и тот же апдейт
            # дважды: две разведки -> две сети -> ручной рестарт по кругу
            log.info("дубль ссылки, пропускаю: chat=%s url=%s", s.chat_id, url[:100])
            return
        old.cancel.set()
        if old.task:
            old.task.cancel()
    async with svc.pending_lock:
        if svc.pending >= svc.cfg.max_queue:
            await push_state(svc, s, {"ph": "error", "e": "Очередь занята, попробуй позже"})
            return
        svc.pending += 1
    s.url, s.quality, s.slides, s.idx = url, quality, [], 0
    await push_state(svc, s, {
        "ph": "queued", "p": 0, "e": "", "s": source_label(url), "t": "Разбираю ссылку…",
        "m": "", "n": 0, "i": 0, "q": quality, "u": original_link(url),
    })
    job = Job(widget_id=s.widget_id, url=url, quality=quality)
    job.task = asyncio.create_task(run_job(svc, s, url, quality, job))
    svc.jobs[key] = job
    log.info("job поставлен: chat=%s q=%s url=%s", s.chat_id, quality, url[:100])


async def handle_command(svc: Service, s: Session, cmd: str) -> None:
    cmd = (cmd or "").strip()
    if len(cmd.encode("utf-8")) > MAX_REQUEST or cmd in ("", "noop", "o"):
        return
    if cmd in ("n", "p"):
        if not s.slides:
            return
        idx = (s.idx + (1 if cmd == "n" else -1)) % len(s.slides)
        try:
            await show_slide(svc, s, idx)
            await push_state(svc, s, {"i": idx})
        except Exception as exc:
            await push_state(svc, s, {"ph": "error", "e": friendly_error(exc)})
        return
    if cmd == "h":
        await push_state(svc, s, {"h": HELP_TEXT})
        return
    if cmd == "g":
        lines = await diag_lines(svc, svc.bot)
        await svc.bot.send_message(chat_id=s.chat_id, text="\n".join(lines)[:3800],
                                   disable_notification=True)
        return
    if cmd == "x":
        job = svc.jobs.get((s.chat_id, s.widget_id))
        if job:
            job.cancel.set()
            if job.task:
                job.task.cancel()
        await push_state(svc, s, {"ph": "idle", "p": 0, "e": "", "t": "Отправь ссылку"})
        return
    if cmd == "r":
        if s.url:
            await start_job(svc, s, s.url, s.quality)
        return
    if cmd.startswith("q:"):
        q = cmd[2:]
        if q not in ALLOWED_QUALITIES:
            return
        if q == s.quality and s.state.get("ph") == "done":
            return                     # не перекачиваем то же самое
        if s.url:
            await start_job(svc, s, s.url, q)
        else:
            # качество можно выбрать заранее, до первой ссылки
            s.quality = q
            await push_state(svc, s, {"q": q, "h": "Теперь пришли ссылку — скачаю в этом качестве"})


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

router = Router()


@router.message(CommandStart())
async def on_start(message: Message, bot: Bot, svc: Service) -> None:
    s = await ensure_card(svc, message.chat.id, fresh=True)
    await push_state(svc, s, {"ph": "idle", "p": 0, "e": "", "d": "", "h": "", "b": "",
                              "t": "Отправь ссылку", "n": 0, "i": 0, "low": svc.cfg.low_memory,
                              "m": "Покажу прогресс, результат и детали прямо здесь"})


@router.message(Command("diag"))
async def on_diag(message: Message, bot: Bot, svc: Service) -> None:
    """Явная команда пользователя: бот молчит, пока её не попросили."""
    lines = await diag_lines(svc, bot)
    await bot.send_message(chat_id=message.chat.id, text="\n".join(lines)[:3800],
                           disable_notification=True)


@router.message(F.text)
async def on_text(message: Message, bot: Bot, svc: Service) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if message.date and message.date.timestamp() < svc.started_at - 5:
        log.info("пропускаю устаревший update: chat=%s date=%s", message.chat.id, message.date)
        return
    url = extract_url(text)
    if not url:
        log.info("сообщение без http(s)-ссылки: chat=%s text=%r",
                 message.chat.id, text[:60])
        s = await ensure_card(svc, message.chat.id, fresh=True)
        await push_state(svc, s, {"ph": "idle", "e": "Нужна ссылка http(s)",
                                  "d": f"получено: {text[:120]!r}", "h": HELP_TEXT})
        return
    ok, why = await validate_url(url)
    if ok:
        url = normalize_url(url)
    s = await ensure_card(svc, message.chat.id, fresh=True)
    if not ok:
        log.info("ссылка отклонена: chat=%s url=%s причина=%s", message.chat.id, url[:100], why)
        await push_state(svc, s, error_patch(Exception(why), url[:120]))
        return
    if not svc.allow_user(message.chat.id):
        log.info("rate limit: chat=%s (%d ссылок / 30 с)", message.chat.id, svc.cfg.rate_per_30s)
        await push_state(svc, s, error_patch(Exception("Слишком часто, подожди 30 секунд"),
                                             f"лимит {svc.cfg.rate_per_30s} ссылок / 30 с"))
        return
    await start_job(svc, s, url, s.quality or "720")


@router.widget_query()
async def on_widget_query(q: WidgetQuery, bot: Bot, svc: Service) -> None:
    # answerWidgetQuery — строго в течение 15 секунд, ДО любой работы
    try:
        await bot.answer_widget_query(widget_query_id=q.id, data="ok")
    except TelegramAPIError as exc:
        log.warning("answerWidgetQuery: %s", exc)
    if getattr(q.type, "type", None) != "data" or not q.chat or not q.message_id:
        return
    s = svc.store.get(q.chat.id, q.message_id)
    if not s:
        log.info("widget_query: сессия не найдена chat=%s msg=%s", q.chat.id, q.message_id)
        return
    try:
        await handle_command(svc, s, q.data)
    except Exception:
        log.exception("widget command %r", q.data)


@router.shared_widget_query()
async def on_shared_widget_query(q: SharedWidgetQuery, bot: Bot, svc: Service) -> None:
    # у форварда нет chat/message_id — он не может управлять скачкой
    try:
        await bot.answer_widget_query(widget_query_id=q.id, data="ok")
    except TelegramAPIError:
        pass


@router.callback_query()
async def on_callback(c: CallbackQuery, bot: Bot, svc: Service) -> None:
    try:
        await bot.answer_callback_query(callback_query_id=c.id)
    except TelegramAPIError:
        pass
    if not c.message:
        return                         # callback_query_mode=inline (10.5)
    s = svc.store.get(c.message.chat.id, c.message.message_id)
    if s:
        try:
            await handle_command(svc, s, c.data or "")
        except Exception:
            log.exception("callback command %r", c.data)
    else:
        log.info("callback: сессия не найдена chat=%s msg=%s data=%r",
                 c.message.chat.id, c.message.message_id, c.data)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def load_widget_html() -> str:
    return Path(__file__).with_name("widget.html").read_text(encoding="utf-8")


def _dns_lines_sync(hosts: tuple[str, ...] = ("youtube.com", "www.tiktok.com", "api.telegram.org")) -> list[str]:
    """DNS-проверка с жёстким таймаутом (daemon-поток, без asyncio.run)."""
    import threading
    out: list[str] = []
    for host in hosts:
        box: dict[str, Any] = {}

        def work(h: str = host, b: dict[str, Any] = box) -> None:
            try:
                b["ips"] = sorted({i[4][0] for i in socket.getaddrinfo(h, 443)})[:2]
            except Exception as exc:
                b["err"] = f"{type(exc).__name__}: {exc}"

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(5)
        if "ips" in box:
            out.append(f"DNS {host}: OK {', '.join(box['ips'])}")
        elif "err" in box:
            out.append(f"DNS {host}: ОШИБКА {box['err']}")
        else:
            out.append(f"DNS {host}: ТАЙМАУТ 5 с")
    return out


def environment_lines(cfg: Config) -> list[str]:
    return [
        f"Python {sys.version.split()[0]} · aiogram {aiogram.__version__} · yt-dlp {ytdlp_version()}",
        f"ffmpeg: {cfg.ffmpeg or 'НЕ НАЙДЕН'} · ffprobe: {cfg.ffprobe or 'НЕ НАЙДЕН'}",
        f"gallery-dl: {cfg.gallery_dl or 'НЕ НАЙДЕН (опционально)'}",
        f"API: {'TEST SERVER' if cfg.test_server else 'production'} · виджеты: {cfg.widget_mode}",
        f"ретрай виджета: {cfg.widget_retry_min} мин · BOT_TOKEN: "
        f"{'задан' if cfg.token and 'AAExampleTokenReplaceMe' not in cfg.token else 'НЕ ЗАДАН (пример из .env.example)'}",
        f"разведка: {'subprocess (изолировано)' if cfg.isolate_inspect else 'поток'} · "
        f"drop_pending_updates={int(cfg.drop_pending_updates)} · faulthandler={cfg.faulthandler_interval} с",
        f"память: лимит={cfg.memory_limit_mb} МБ · профиль={'LOW' if cfg.low_memory else 'normal'} "
        f"({cfg.memory_profile}) · RSS={current_rss_mb()} МБ · yt-dlp ulimit={cfg.ytdlp_max_as_mb} МБ",
    ]


def doctor() -> int:
    """Проверка хоста БЕЗ токена: python bot.py --doctor."""
    load_dotenv()
    cfg = Config.from_env()
    raw, packed = validate_widget_html(load_widget_html())
    lines = [f"widget.html: {raw} Б raw / {packed} Б zlib-9 (лимит {MAX_HTML_ZLIB})"]
    lines += environment_lines(cfg)
    lines += _dns_lines_sync()
    if cfg.low_memory:
        lines.append("⚠ LOW-MEMORY: parallel=1, пережатие ffmpeg отключено, "
                     "yt-dlp ограничен ulimit -v, карусели урезаны")
    elif not Path("/sys/fs/cgroup/memory.max").is_file():
        lines.append("ℹ cgroup memory.max не читается: лимит взят из MemTotal хоста. "
                     "Если стенд на 256 МБ — выставь MEMORY_PROFILE=low вручную")
    print("\n".join(lines))
    return 0


async def diag_lines(svc: Service, bot: Bot) -> list[str]:
    """То же, но из чата: /diag."""
    lines = environment_lines(svc.cfg)
    lines.append(f"очередь: {svc.pending}/{svc.cfg.max_queue} · слотов: {svc.cfg.max_parallel}")
    lines.append(f"сессий в БД: {len(svc.store.sessions)} · активных job: {len(svc.jobs)}")
    try:
        import resource
        import threading
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        lines.append(f"память maxRSS: {rss} МБ · сейчас VmRSS: {current_rss_mb()} МБ · "
                     f"потоков: {threading.active_count()}")
    except Exception:
        pass
    lines += await asyncio.to_thread(_dns_lines_sync)
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=10)
        lines.append(f"getMe: OK @{getattr(me, 'username', '?')}")
    except Exception as exc:
        lines.append(f"getMe: ОШИБКА {type(exc).__name__}: {exc}")
    try:
        disk = shutil.disk_usage(svc.cfg.tmp_dir)
        lines.append(f"диск tmp/: свободно {disk.free // (1024 * 1024)} МБ")
    except Exception:
        pass
    return lines


def selfcheck() -> int:
    raw, packed = validate_widget_html(load_widget_html())
    print(f"widget.html: {raw} Б raw / {packed} Б zlib-9 (лимит {MAX_HTML_ZLIB}) — OK")
    import aiogram
    print(f"aiogram {aiogram.__version__} (Bot API 10.5), yt-dlp: {ytdlp_version()}")
    print(f"ffmpeg: {shutil.which('ffmpeg') or 'НЕ НАЙДЕН (без пережатия >50 МБ)'} · "
          f"gallery-dl: {gallery_dl_bin() or 'НЕ НАЙДЕН (опционально)'}")
    return 0


def install_signal_diagnostics(cfg: Config) -> None:
    """Понять, КТО остановил процесс: хост, OOM-killer или зависший поток."""
    import signal
    import threading
    if cfg.faulthandler_interval > 0:
        try:
            import faulthandler
            faulthandler.enable()
            faulthandler.dump_traceback_later(cfg.faulthandler_interval, repeat=True)
            log.info("faulthandler: дамп стеков каждые %d с", cfg.faulthandler_interval)
        except Exception as exc:
            log.warning("faulthandler: %s", exc)

    def on_term(signum: int, frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        log.warning("получен сигнал %s — процесс останавливают извне; "
                    "потоков=%d, активных job проверь в /diag", name, threading.active_count())

    try:
        signal.signal(signal.SIGTERM, on_term)   # SIGINT оставляем Python (Ctrl-C)
    except (ValueError, OSError) as exc:
        log.warning("не поставил обработчик SIGTERM: %s", exc)


async def main() -> None:
    load_dotenv()
    cfg = Config.from_env()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "--check" in sys.argv:
        raise SystemExit(selfcheck())
    if "--doctor" in sys.argv:
        raise SystemExit(doctor())
    if not cfg.token:
        raise SystemExit("Задай BOT_TOKEN в .env / окружении. Проверка без токена: python bot.py --check")
    install_signal_diagnostics(cfg)
    widget_html = load_widget_html()
    raw, packed = validate_widget_html(widget_html)
    log.info("widget.html: %s Б raw / %s Б zlib-9", raw, packed)
    for line in environment_lines(cfg):
        log.info("старт: %s", line)
    if cfg.low_memory:
        log.warning("LOW-MEMORY (лимит %d МБ): parallel=1, ffmpeg-пережатие отключено, "
                    "yt-dlp ulimit=%d МБ, MAX_ORIGINAL_MB=%d, MAX_SLIDES=%d",
                    cfg.memory_limit_mb, cfg.ytdlp_max_as_mb,
                    cfg.max_original_mb, cfg.max_slides)
    if not cfg.ffmpeg:
        log.warning("ffmpeg не найден: слияние дорожек и пережатие >48 МБ недоступны")

    session = AiohttpSession(api=TEST) if cfg.test_server else AiohttpSession()
    bot = Bot(token=cfg.token, session=session, default=DefaultBotProperties())
    dp = Dispatcher()
    dp.include_router(router)
    store = Store(cfg.db_path)
    svc = Service(cfg, bot, store, widget_html, cfg.tmp_dir / "placeholder.png")
    await svc.start()
    dp["svc"] = svc                       # инъекция в хендлеры
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть панель"),
            BotCommand(command="diag", description="Диагностика бота"),
        ])
    except TelegramAPIError:
        pass
    if cfg.ensure_callback_mode_default and hasattr(bot, "set_my_settings"):
        # 10.5: гарантируем, что у callback_query будет поле message
        try:
            await bot.set_my_settings(callback_query_mode="default")
        except (TelegramAPIError, AttributeError) as exc:
            log.warning("setMySettings: %s", exc)

    try:
        if cfg.mode == "webhook":
            if not cfg.webhook_url:
                raise SystemExit("Для MODE=webhook нужен WEBHOOK_URL")
            await bot.set_webhook(url=cfg.webhook_url, secret_token=cfg.webhook_secret or None,
                                  allowed_updates=ALLOWED_UPDATES)
            app = web.Application()
            handler = SimpleRequestHandler(dispatcher=dp, bot=bot,
                                           secret_token=cfg.webhook_secret or None)
            handler.register(app, path=cfg.webhook_path)
            setup_application(app, dp, bot=bot, svc=svc)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=cfg.webhook_host, port=cfg.webhook_port)
            await site.start()
            log.info("webhook на %s:%s%s", cfg.webhook_host, cfg.webhook_port, cfg.webhook_path)
            await asyncio.Event().wait()
        else:
            try:
                await bot.delete_webhook(drop_pending_updates=cfg.drop_pending_updates)
            except TelegramAPIError:
                pass
            log.info("polling, allowed_updates=%s, drop_pending_updates=%s",
                     ALLOWED_UPDATES, cfg.drop_pending_updates)
            await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await svc.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstop")
