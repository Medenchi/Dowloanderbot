from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import bot
from aiogram.types import Chat, Message, PhotoSize


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    os.environ["BOT_TOKEN"] = "1:test"
    c = bot.Config.from_env()
    return c


def photo_message(file_id: str = "AgACAgIAAxkBAAI", mid: int = 42) -> Message:
    return Message(
        message_id=mid, date=datetime.now(timezone.utc),
        chat=Chat(id=777, type="private"),
        photo=[PhotoSize(file_id=file_id, file_unique_id="uniq", width=640, height=360)],
    )


class FakeBot:
    def __init__(self, widget_fails: bool = False):
        self.widget_fails = widget_fails
        self.media_calls: list[dict] = []
        self.state_calls: list[dict] = []
        self.sent: list[tuple[str, dict]] = []

    async def edit_message_media(self, **kw):
        self.media_calls.append(kw)
        return photo_message()

    async def edit_message_widget_data(self, **kw):
        self.state_calls.append(kw)
        return True

    async def edit_message_text(self, **kw):
        self.state_calls.append(kw)
        return True

    async def send_photo(self, **kw):
        self.sent.append(("photo", kw))
        return photo_message(mid=41)

    async def send_widget(self, **kw):
        if self.widget_fails:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method="sendWidget", message="old client")
        self.sent.append(("widget", kw))
        return photo_message(mid=99)

    async def send_message(self, **kw):
        self.sent.append(("message", kw))
        return photo_message(mid=98)

    async def delete_message(self, **kw):
        self.sent.append(("delete", kw))
        return True

    async def send_video(self, **kw):
        return photo_message()

    async def send_audio(self, **kw):
        return photo_message()

    async def send_document(self, **kw):
        return photo_message()


# --- виджет ---------------------------------------------------------------

def test_widget_budget():
    html_text = bot.load_widget_html()
    raw, packed = bot.validate_widget_html(html_text)
    assert 0 < raw <= bot.MAX_HTML_RAW
    assert packed <= 3900, "оставить запас под фичи"
    assert html_text.count("backdrop-filter") <= 2
    for bad in ("<input", "<textarea", "<select", "@import", "fetch(", "XMLHttpRequest"):
        assert bad not in html_text


def test_widget_uses_state_contract():
    html_text = bot.load_widget_html()
    assert "chatDataChanged" in html_text
    assert "window.TelegramWidget" in html_text
    assert "W.request(" in html_text
    assert "openLink" in html_text
    for cmd in ("q:360", "q:720", "q:1080", "q:a", "data-c=\"p\"", "data-c=\"n\"", "data-c=\"r\"", "data-c=\"x\""):
        assert cmd in html_text


def test_widget_size_matches_server_count():
    html_text = bot.load_widget_html()
    raw, packed = bot.widget_size(html_text)
    assert raw == len(html_text.encode("utf-8"))
    assert packed == len(zlib.compress(html_text.encode("utf-8"), 9))


# --- состояние ------------------------------------------------------------

def test_compact_state_fits_1024_bytes():
    st = bot.idle_state()
    st.update({"t": "Ы" * 500, "m": "meta" * 100, "e": "ошибка" * 100,
               "u": "https://www.tiktok.com/@someone/photo/123456?" + "x" * 400})
    data = bot.compact_state(st)
    assert len(data.encode("utf-8")) <= 1024
    assert json.loads(data)["ph"] == "idle"


def test_idle_state_is_valid_json():
    assert json.loads(bot.compact_state(bot.idle_state()))["ph"] == "idle"


def test_fallback_keyboard_callback_data_short():
    st = bot.idle_state()
    st.update({"ph": "carousel", "n": 7, "i": 2, "q": "720"})
    kb = bot.fallback_kb(st)
    for row in kb.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode("utf-8")) <= 64
    assert "‹" in [b.text for b in kb.inline_keyboard[-2]]


def test_render_fallback_contains_phase():
    st = bot.idle_state()
    st.update({"ph": "error", "e": "Таймаут"})
    txt = bot.render_fallback(st)
    assert "Ошибка" in txt and "Таймаут" in txt and len(txt) <= 900


# --- URL ------------------------------------------------------------------

def test_extract_url_strips_trailing_punctuation():
    assert bot.extract_url("глянь https://youtu.be/abc!") == "https://youtu.be/abc"
    assert bot.extract_url("привет") is None
    assert bot.extract_url("https://example.com/a),") == "https://example.com/a"


def test_source_label():
    assert bot.source_label("https://www.youtube.com/watch?v=x") == "YouTube"
    assert bot.source_label("https://vm.tiktok.com/ZMhxxxx/") == "TikTok"
    assert bot.source_label("https://www.instagram.com/reel/abc/") == "Instagram"
    assert bot.source_label("https://x.com/user/status/1") == "X"


def test_original_link_rules():
    assert bot.original_link("https://youtu.be/abc").startswith("https://")
    assert bot.original_link("http://example.com/a") == ""
    assert bot.original_link("https://t.me/chan/1") == ""
    assert bot.original_link("https://example.com/" + "a" * 300) == ""


def test_validate_url_blocks_ssrf(monkeypatch):
    # не дёргаем реальный DNS в тестах
    monkeypatch.setattr(bot, "_resolve_private", lambda host: host == "10.0.0.1")
    assert asyncio.run(bot.validate_url("http://127.0.0.1/admin"))[0] is False
    assert asyncio.run(bot.validate_url("http://localhost/x"))[0] is False
    assert asyncio.run(bot.validate_url("file:///etc/passwd"))[0] is False
    assert asyncio.run(bot.validate_url("https://user:pass@example.com/a"))[0] is False
    assert asyncio.run(bot.validate_url("https://example.com:8080/a"))[0] is False
    assert asyncio.run(bot.validate_url("https://10.0.0.1/a"))[0] is False
    assert asyncio.run(bot.validate_url("https://example.com/video"))[0] is True


# --- media / yt-dlp -------------------------------------------------------

def test_format_selector():
    assert "bestaudio" in bot.format_selector("a", True)
    no_ff = bot.format_selector("720", False)
    assert "+" not in no_ff                       # без ffmpeg только прогрессив
    assert "acodec!=none" in no_ff                # ... и обязательно со звуком
    assert "bestvideo" in bot.format_selector("1080", True)
    assert "+" in bot.format_selector("1080", True)


def test_ydl_opts_cap(tmp_path, cfg):
    o = bot.ydl_opts(cfg, "720", download=False)
    assert o["ignore_no_formats_error"] is True
    assert o["max_filesize"] == cfg.max_original_bytes
    assert o["playlistend"] == cfg.max_slides


def test_ydl_opts_merge_gated_on_ffmpeg(cfg, monkeypatch):
    monkeypatch.setattr(bot.shutil, "which", lambda name: None)
    assert "merge_output_format" not in bot.ydl_opts(cfg, "720", download=False)
    monkeypatch.setattr(bot.shutil, "which", lambda name: "/usr/bin/" + name)
    assert bot.ydl_opts(cfg, "720", download=False)["merge_output_format"] == "mp4"


def test_classify_path(tmp_path):
    assert bot.classify_path(tmp_path / "a.mp4") == "video"
    assert bot.classify_path(tmp_path / "a.webm") == "document"   # sendVideo не ест webm
    assert bot.classify_path(tmp_path / "a.mkv") == "document"
    assert bot.classify_path(tmp_path / "a.mp3") == "audio"
    assert bot.classify_path(tmp_path / "a.jpg") == "photo"
    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * (11 * 1024 * 1024))
    assert bot.classify_path(big) == "document"


def test_dedupe_images_mirrors():
    urls = [
        "https://p16-sign.tiktokcdn.com/obj/photo-1.jpeg?x=1",
        "https://p77-sign.tiktokcdn.com/obj/photo-1.jpeg?x=2",
        "https://p16-sign.tiktokcdn.com/obj/photo-2.jpeg?x=1",
    ]
    assert len(bot._dedupe_images(urls)) == 2


def test_find_image_urls_hydration():
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": {
        "imagePost": {"images": [
            {"imageURL": {"urlList": ["https://p16.tiktokcdn.com/a.jpeg"]}},
            {"imageURL": {"urlList": ["https://p16.tiktokcdn.com/b.jpeg"]}},
        ]}}}}}}
    out: list[str] = []
    bot._find_image_urls(data, out)
    assert out == ["https://p16.tiktokcdn.com/a.jpeg", "https://p16.tiktokcdn.com/b.jpeg"]


def test_inspect_media_photo_carousel(monkeypatch, cfg):
    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            return {"title": "Photo dump", "entries": [
                {"thumbnail": "https://cdn.instagram.com/1.jpg", "formats": []},
                {"thumbnail": "https://cdn.instagram.com/2.jpg", "formats": []},
                {"thumbnail": "https://cdn.instagram.com/3.jpg", "formats": []},
            ]}

    monkeypatch.setattr(bot.yt_dlp, "YoutubeDL", FakeYDL)
    info = bot.inspect_media("https://instagram.com/p/x/", "720", cfg)
    assert info["kind"] == "images"
    assert len(info["images"]) == 3
    assert info["has_video"] is False


def test_extract_file_id():
    assert bot.extract_file_id(photo_message("FILEID")) == "FILEID"
    assert bot.extract_file_id(object()) is None


def test_media_object_types():
    assert bot.media_object("photo", "id").type == "photo"
    assert bot.media_object("video", "id", {"duration": 12}).supports_streaming is True
    assert bot.media_object("audio", "id").type == "audio"


# --- PNG-заглушка ---------------------------------------------------------

def test_placeholder_is_valid_png(tmp_path):
    p = tmp_path / "ph.png"
    bot.write_placeholder_png(p)
    data = p.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(data):
        (size,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + size]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack_from(">IIBB", chunk)
            assert (w, h, depth, ctype) == (640, 360, 8, 2)
        if tag == b"IDAT":
            idat += chunk
        pos += 12 + size
    raw = zlib.decompress(idat)
    assert len(raw) == 360 * (1 + 640 * 3)


# --- команды / карусель ---------------------------------------------------

def make_service(tmp_path, cfg):
    c = bot.Config.from_env()
    object.__setattr__(c, "tmp_dir", tmp_path / "tmp")
    object.__setattr__(c, "db_path", tmp_path / "state.sqlite3")
    fake = FakeBot()
    store = bot.Store(c.db_path)
    svc = bot.Service(c, fake, store, bot.load_widget_html(), c.tmp_dir / "placeholder.png")
    s = bot.Session(chat_id=777, widget_id=42, media_id=41, mode="widget",
                    state=bot.idle_state(), url="https://tiktok.com/@u/photo/1", quality="720")
    store.save(s)
    return svc, s, fake


def test_carousel_next_cached_file_id(tmp_path, cfg):
    async def run():
        svc, s, fake = make_service(tmp_path, cfg)
        d = svc.cfg.tmp_dir / "c-test"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (d / f"{i:03d}.jpg").write_bytes(b"jpeg")
        s.slides = [{"path": str(d / f"{i:03d}.jpg"), "kind": "photo"} for i in range(3)]
        s.state.update({"ph": "carousel", "n": 3, "i": 0})
        await bot.handle_command(svc, s, "n")
        assert s.idx == 1
        assert s.slides[1]["file_id"] == "AgACAgIAAxkBAAI"
        assert fake.media_calls[-1]["message_id"] == 41
        await bot.handle_command(svc, s, "n")
        assert s.idx == 2
        await bot.handle_command(svc, s, "p")
        assert s.idx == 1
        # второй круг использует закешированный file_id, а не FSInputFile
        assert fake.media_calls[-1]["media"].media == "AgACAgIAAxkBAAI"
        assert all(len(json.loads(c["chat_data"]).get("t", "")) <= 64 for c in fake.state_calls)
        svc.store.close()

    asyncio.run(run())


def test_quality_command_validates_allowlist(tmp_path, cfg, monkeypatch):
    async def run():
        svc, s, fake = make_service(tmp_path, cfg)
        called = []

        async def fake_start(service, session, url, quality):
            called.append((url, quality))

        monkeypatch.setattr(bot, "start_job", fake_start)
        await bot.handle_command(svc, s, "q:1080")
        await bot.handle_command(svc, s, "q:9999")
        await bot.handle_command(svc, s, "q:" + "x" * 100)
        assert called == [("https://tiktok.com/@u/photo/1", "1080")]
        svc.store.close()

    asyncio.run(run())


def test_compact_state_cyrillic_is_byte_safe():
    st = bot.idle_state()
    st.update({"ph": "done", "t": "Ы" * 400, "m": "Ы" * 400,
               "e": "Ы" * 400, "u": "https://ex.com/" + "Ы" * 100})
    data = bot.compact_state(st)
    assert len(data.encode("utf-8")) <= 1024
    assert json.loads(data)["ph"] == "done"


def test_edit_media_falls_back_to_document(tmp_path, cfg):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import FSInputFile, InputMediaDocument, InputMediaPhoto

    class FailingBot(FakeBot):
        async def edit_message_media(self, **kw):
            if not isinstance(kw["media"], InputMediaDocument):
                self.media_calls.append(kw)
                raise TelegramBadRequest(method="editMessageMedia", message="PHOTO_INVALID")
            return await super().edit_message_media(**kw)

    async def run():
        svc, s, _ = make_service(tmp_path, cfg)
        svc.bot = FailingBot()
        p = svc.cfg.tmp_dir / "weird.webp"
        p.write_bytes(b"data")
        msg = await bot.edit_media_with_retry(svc, s, InputMediaPhoto(media=FSInputFile(p)))
        assert isinstance(msg, Message)
        assert svc.bot.media_calls[-1]["media"].type == "document"
        svc.store.close()

    asyncio.run(run())


def test_friendly_error_short():
    assert bot.friendly_error(Exception("Unsupported URL")) == "Ссылка не поддерживается"
    assert len(bot.friendly_error(Exception("x" * 500))) <= 92


# --- карточка и фолбэк ----------------------------------------------------

def fresh_service(tmp_path, widget_fails: bool = False):
    c = bot.Config.from_env()
    object.__setattr__(c, "tmp_dir", tmp_path / "tmp2")
    object.__setattr__(c, "db_path", tmp_path / "state2.sqlite3")
    fake = FakeBot(widget_fails=widget_fails)
    svc = bot.Service(c, fake, bot.Store(c.db_path), bot.load_widget_html(),
                      c.tmp_dir / "placeholder.png")
    return svc, fake


def test_ensure_card_widget(tmp_path, cfg):
    async def run():
        svc, fake = fresh_service(tmp_path)
        s = await bot.ensure_card(svc, 777)
        assert s.mode == "widget" and s.media_id == 41 and s.widget_id == 99
        assert [x[0] for x in fake.sent] == ["photo", "widget"]
        w = fake.sent[1][1]
        assert w["allow_queries"] is True and w["is_shared"] is False
        assert w["height"] == bot.WIDGET_HEIGHT
        assert len(w["chat_data"].encode("utf-8")) <= 1024
        assert "widget" in str(w["caption"]).lower() or len(w["caption"]) > 10
        svc.store.close()

    asyncio.run(run())


def test_ensure_card_fallback_when_widget_unavailable(tmp_path, cfg):
    async def run():
        svc, fake = fresh_service(tmp_path, widget_fails=True)
        s = await bot.ensure_card(svc, 777)
        assert s.mode == "fallback" and s.media_id is None
        assert any(x[0] == "message" for x in fake.sent)
        assert "inline_keyboard" in fake.sent[-1][1]["reply_markup"].model_dump()
        svc.store.close()

    asyncio.run(run())


# --- фоновый джоб (без сети) ----------------------------------------------

def test_run_job_carousel_flow(tmp_path, cfg, monkeypatch):
    async def run():
        svc, s, fake = make_service(tmp_path, cfg)

        async def fake_plan(service, url, quality):
            return {"kind": "carousel", "images": ["https://cdn/1.jpg", "https://cdn/2.jpg",
                                                   "https://cdn/3.jpg"],
                    "title": "Photo dump", "meta": "3 фото"}

        async def fake_images(service, urls, chat_id, wid):
            d = service.cfg.tmp_dir / "c-fake"
            d.mkdir(parents=True, exist_ok=True)
            out = []
            for i in range(len(urls)):
                p = d / f"{i:03d}.jpg"
                p.write_bytes(b"jpeg")
                out.append({"path": str(p), "kind": "photo"})
            return out

        async def fake_show(service, session, idx):
            session.idx = idx

        monkeypatch.setattr(bot, "inspect_plan", fake_plan)
        monkeypatch.setattr(bot, "fetch_images", fake_images)
        monkeypatch.setattr(bot, "show_slide", fake_show)
        monkeypatch.setattr(bot, "cleanup_old", lambda service: None)

        job = bot.Job(widget_id=s.widget_id)
        await bot.run_job(svc, s, s.url, "720", job)
        assert s.state["ph"] == "carousel"
        assert s.state["n"] == 3 and s.state["p"] == 100
        phases = [json.loads(c["chat_data"])["ph"] for c in fake.state_calls]
        assert "meta" in phases and "down" in phases and phases[-1] == "carousel"
        assert all(len(c["chat_data"].encode("utf-8")) <= 1024 for c in fake.state_calls)
        assert job.done is True and svc.pending == 0
        svc.store.close()

    asyncio.run(run())


def test_on_text_starts_job(tmp_path, cfg, monkeypatch):
    async def run():
        svc, fake = fresh_service(tmp_path)
        calls = []

        async def fake_start(service, session, url, quality):
            calls.append((url, quality))

        async def fake_validate(url):
            return True, ""

        monkeypatch.setattr(bot, "start_job", fake_start)
        monkeypatch.setattr(bot, "validate_url", fake_validate)
        msg = Message(message_id=1, date=datetime.now(timezone.utc),
                      chat=Chat(id=777, type="private"),
                      text="глянь https://www.tiktok.com/@u/photo/123!")
        await bot.on_text(msg, fake, svc)
        assert calls == [("https://www.tiktok.com/@u/photo/123", "720")]
        svc.store.close()

    asyncio.run(run())


# --- диагностика, ретрай виджета, большие файлы ---------------------------

def test_config_widget_flags(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TEST_SERVER", "1")
    monkeypatch.setenv("TELEGRAM_WIDGETS", "off")
    monkeypatch.setenv("WIDGET_RETRY_MIN", "5")
    monkeypatch.setenv("TELEGRAM_WIDGETS", "off")
    c = bot.Config.from_env()
    assert c.test_server is True
    assert c.widget_mode == "off"
    assert c.widget_retry_seconds == 300
    monkeypatch.setenv("TELEGRAM_WIDGETS", "что-то-не-то")
    assert bot.Config.from_env().widget_mode == "auto"


def test_widget_mode_off_skips_send_widget(tmp_path, cfg):
    async def run():
        svc, fake = fresh_service(tmp_path)
        object.__setattr__(svc.cfg, "widget_mode", "off")
        s = await bot.ensure_card(svc, 777)
        assert s.mode == "fallback"
        assert all(kind != "widget" for kind, _ in fake.sent)
        svc.store.close()

    asyncio.run(run())


def test_fallback_retries_widget_after_ttl(tmp_path, cfg):
    async def run():
        svc, fake = fresh_service(tmp_path, widget_fails=True)
        s = await bot.ensure_card(svc, 777)
        assert s.mode == "fallback" and s.fallback_at > 0
        # симулируем «прошло больше WIDGET_RETRY_MIN» — updated не должен сбрасывать TTL
        s.updated = time.time()
        s.fallback_at = time.time() - svc.cfg.widget_retry_seconds - 10
        fake.widget_fails = False
        s2 = await bot.ensure_card(svc, 777)
        assert s2.mode == "widget"
        assert [k for k, _ in fake.sent][-2:] == ["photo", "widget"]
        assert len(svc.store.sessions) == 1
        svc.store.close()

    asyncio.run(run())


def test_fallback_not_retried_before_ttl(tmp_path, cfg):
    async def run():
        svc, fake = fresh_service(tmp_path, widget_fails=True)
        s = await bot.ensure_card(svc, 777)
        fake.widget_fails = False
        s2 = await bot.ensure_card(svc, 777)
        assert s2 is s and s2.mode == "fallback"
        assert not any(k == "widget" for k, _ in fake.sent)
        svc.store.close()

    asyncio.run(run())


def test_push_state_recreates_deleted_card(tmp_path, cfg):
    from aiogram.exceptions import TelegramBadRequest

    class DeletedBot(FakeBot):
        def __init__(self):
            super().__init__(widget_fails=True)
            self.edit_calls = 0

        async def edit_message_text(self, **kw):
            self.edit_calls += 1
            raise TelegramBadRequest(method="editMessageText",
                                     message="Bad Request: message to edit not found")

    async def run():
        fake = DeletedBot()
        c = bot.Config.from_env()
        object.__setattr__(c, "tmp_dir", tmp_path / "tmp3")
        object.__setattr__(c, "db_path", tmp_path / "state3.sqlite3")
        svc = bot.Service(c, fake, bot.Store(c.db_path), bot.load_widget_html(),
                          c.tmp_dir / "placeholder.png")
        s = await bot.ensure_card(svc, 777)
        before = sum(1 for k, _ in fake.sent if k == "message")
        await bot.push_state(svc, s, {"ph": "error", "e": "тест"})
        after = sum(1 for k, _ in fake.sent if k == "message")
        assert before == 1 and after == 2
        assert fake.edit_calls == 1
        svc.store.close()

    asyncio.run(run())


def test_fallback_kb_has_source_button():
    st = bot.idle_state()
    st["u"] = "https://www.youtube.com/watch?v=abc"
    rows = bot.fallback_kb(st).model_dump()["inline_keyboard"]
    assert rows[-1][0]["url"].startswith("https://")
    assert not rows[-1][0].get("callback_data")


def test_download_media_prefers_merged_mp4(tmp_path, cfg, monkeypatch):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            dest = Path(self.opts["outtmpl"]).parent
            src = dest / "video.f137.mp4"
            src.write_bytes(b"x" * 500)             # исходник БОЛЬШЕ итога
            merged = dest / "final.mp4"
            merged.write_bytes(b"y" * 100)          # но нужен именно он
            return {"filepath": str(merged),
                    "requested_downloads": [{"filepath": str(src)}],
                    "duration": 12, "width": 640, "height": 360}

    monkeypatch.setattr(bot.yt_dlp, "YoutubeDL", FakeYDL)
    c = bot.Config.from_env()
    object.__setattr__(c, "tmp_dir", tmp_path / "dl")
    path, dest, info = bot.download_media("https://example.com/v", "720", c,
                                          bot.Job(widget_id=1), 777, 1)
    assert path.name == "final.mp4"
    assert info["duration"] == 12
    bot.shutil.rmtree(dest, ignore_errors=True)


def test_deliver_media_oversize_gives_link(tmp_path, cfg, monkeypatch):
    async def run():
        svc, fake = fresh_service(tmp_path)
        s = await bot.ensure_card(svc, 777)
        s.url = "https://www.youtube.com/watch?v=abc"
        dest = svc.cfg.tmp_dir / "big"
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / "huge.mp4"
        with p.open("wb") as f:
            f.truncate(51 * 1024 * 1024)           # sparse, без реальных 51 МБ
        monkeypatch.setattr(bot.shutil, "which", lambda name: None)  # ffmpeg нет
        await bot.deliver_media(svc, s, p, dest, "720", {})
        assert s.state["ph"] == "error"
        assert "источник" in s.state["e"]
        assert s.state["u"].startswith("https://")
        assert not any(k in ("video", "document") for k, _ in fake.sent)
        assert not dest.exists()                   # tmp убрали
        svc.store.close()

    asyncio.run(run())


def test_diag_command(tmp_path, cfg, monkeypatch):
    async def run():
        svc, fake = fresh_service(tmp_path)

        async def fake_diag(service, b):
            return ["OK-строка"]

        monkeypatch.setattr(bot, "diag_lines", fake_diag)
        msg = Message(message_id=7, date=datetime.now(timezone.utc),
                      chat=Chat(id=777, type="private"), text="/diag")
        await bot.on_diag(msg, fake, svc)
        assert fake.sent[-1][0] == "message"
        assert "OK-строка" in fake.sent[-1][1]["text"]
        svc.store.close()

    asyncio.run(run())


def test_doctor_prints_environment(monkeypatch, capsys):
    monkeypatch.setattr(bot, "_dns_lines_sync", lambda: ["DNS stub: OK"])
    assert bot.doctor() == 0
    out = capsys.readouterr().out
    assert "widget.html" in out
    assert "aiogram" in out and "ffmpeg" in out
    assert "DNS stub" in out


def test_friendly_error_new_cases():
    assert "качество" in bot.friendly_error(Exception("Requested format is not available"))
    assert "куки" in bot.friendly_error(Exception("HTTP Error 403: Forbidden"))
    assert "ffmpeg" in bot.friendly_error(Exception("ffmpeg not found"))


def test_load_dotenv_parses_inline_comments_and_keeps_env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(
        "# комментарий\n"
        "TESTDL_TOKEN=abc:123\n"
        "TESTDL_NUM=7            # одновременно качающих\n"
        "export TESTDL_QUOTED=\"hello world\"\n"
        "TESTDL_ARGS={\"youtube\":{\"player_client\":[\"web\"]}}\n"
        "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TESTDL_TOKEN", raising=False)
    monkeypatch.delenv("TESTDL_NUM", raising=False)
    monkeypatch.delenv("TESTDL_QUOTED", raising=False)
    monkeypatch.delenv("TESTDL_ARGS", raising=False)
    assert bot.load_dotenv(p) == 4
    assert os.environ["TESTDL_TOKEN"] == "abc:123"
    assert os.environ["TESTDL_NUM"] == "7"
    assert os.environ["TESTDL_QUOTED"] == "hello world"
    assert os.environ["TESTDL_ARGS"].startswith("{")


def test_load_dotenv_does_not_override_host_env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("TESTDL_KEEP=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TESTDL_KEEP", "from-host")
    bot.load_dotenv(p)
    assert os.environ["TESTDL_KEEP"] == "from-host"


def test_env_example_is_parseable(monkeypatch):
    """README обещает `cp .env.example .env` — файл должен реально грузиться."""
    p = Path(__file__).resolve().parents[1] / ".env.example"
    for key in ("MAX_PARALLEL", "TELEGRAM_WIDGETS", "TELEGRAM_TEST_SERVER", "WIDGET_RETRY_MIN"):
        monkeypatch.delenv(key, raising=False)
    assert bot.load_dotenv(p) > 0
    assert os.environ["MAX_PARALLEL"] == "2"
    assert os.environ["TELEGRAM_WIDGETS"] == "auto"
    assert os.environ["TELEGRAM_TEST_SERVER"] == "0"
    assert os.environ["WIDGET_RETRY_MIN"] == "60"


def test_run_job_video_flow(tmp_path, cfg, monkeypatch):
    """Сквозной путь «ссылка -> mp4 -> чат» без сети: то, что жаловалось в логах."""
    async def run():
        svc, s, fake = make_service(tmp_path, cfg)

        async def fake_plan(service, url, quality):
            return {"kind": "video", "images": [], "title": "Тест", "meta": "0:12", "many": False}

        def fake_download(url, quality, c, job, chat_id, wid, single=False):
            d = c.tmp_dir / "v-fake"
            d.mkdir(parents=True, exist_ok=True)
            p = d / "clip.mp4"
            p.write_bytes(b"mp4-bytes")
            job.percent = 82
            return p, d, {"duration": 12, "width": 640, "height": 360}

        monkeypatch.setattr(bot, "inspect_plan", fake_plan)
        monkeypatch.setattr(bot, "download_media", fake_download)
        monkeypatch.setattr(bot, "cleanup_old", lambda service: None)

        job = bot.Job(widget_id=s.widget_id)
        await bot.run_job(svc, s, s.url, "720", job)
        assert s.state["ph"] == "done" and s.state["p"] == 100
        assert job.done is True and svc.pending == 0
        assert fake.media_calls, "медиа не ушло в чат"
        from aiogram.types import InputMediaVideo
        assert isinstance(fake.media_calls[0]["media"], InputMediaVideo)
        phases = [json.loads(c["chat_data"])["ph"] for c in fake.state_calls]
        assert "meta" in phases and "down" in phases and phases[-1] == "done"
        svc.store.close()

    asyncio.run(run())


# --- изоляция разведки, дубли и короткие домены ----------------------------

def test_normalize_url_short_likee_domain():
    assert bot.normalize_url("https://l.likee.video/v/uc96FR") == "https://likee.video/v/uc96FR"
    assert bot.normalize_url("https://l.likee.video/v/uc96FR?x=1#f") == "https://likee.video/v/uc96FR?x=1#f"
    assert bot.normalize_url("https://www.youtube.com/watch?v=abc") == "https://www.youtube.com/watch?v=abc"
    assert bot.source_label("https://l.likee.video/v/uc96FR") == "Likee"


def test_ydl_cli_cmd_isolated(cfg):
    cmd = bot.ydl_cli_cmd("https://likee.video/v/uc96FR", cfg)
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"]
    for flag in ("-J", "--no-warnings", "--no-progress", "--ignore-no-formats-error",
                 "--skip-download", "--socket-timeout", "--retries"):
        assert flag in cmd
    assert cmd[-1] == "https://likee.video/v/uc96FR"


def test_inspect_media_cli_parses_json(cfg, monkeypatch):
    import subprocess
    info = {"title": "Кот", "duration": 12, "formats": [{"vcodec": "h264", "acodec": "aac"}]}
    r = subprocess.CompletedProcess(args=[], returncode=0,
                                    stdout=json.dumps(info, ensure_ascii=False), stderr="")
    monkeypatch.setattr(bot.subprocess, "run", lambda *a, **kw: r)
    out = bot.inspect_media_cli("https://example.com/v", "720", cfg)
    assert out["kind"] == "video" and out["title"] == "Кот"


def test_inspect_media_cli_null_stdout_is_error(cfg, monkeypatch):
    import subprocess
    r = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="null\n",
        stderr="ERROR: [generic] uc96FR: Unable to download webpage: TLS/SSL connection has been closed")
    monkeypatch.setattr(bot.subprocess, "run", lambda *a, **kw: r)
    out = bot.inspect_media_cli("https://l.likee.video/v/x", "720", cfg)
    assert out["kind"] == "error"
    assert "Сайт не ответил" in out["error"]


def test_inspect_media_cli_timeout(cfg, monkeypatch):
    import subprocess

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="yt-dlp", timeout=kw["timeout"])

    monkeypatch.setattr(bot.subprocess, "run", boom)
    out = bot.inspect_media_cli("https://example.com/v", "720", cfg)
    assert out["kind"] == "error" and "Таймаут" in out["error"]


def test_inspect_plan_uses_cli_when_isolated(tmp_path, cfg, monkeypatch):
    async def run():
        svc, fake = fresh_service(tmp_path)
        calls = []

        def fake_cli(url, quality, c):
            calls.append(url)
            return bot.inspect_error("тест")

        def forbidden_thread(url, quality, c):
            raise AssertionError("потоковый режим не должен вызываться")

        monkeypatch.setattr(bot, "inspect_media_cli", fake_cli)
        monkeypatch.setattr(bot, "inspect_media", forbidden_thread)
        object.__setattr__(svc.cfg, "isolate_inspect", True)
        with pytest.raises(bot.FriendlyError):
            await bot.inspect_plan(svc, "https://example.com/v", "720")
        assert calls == ["https://example.com/v"]
        svc.store.close()

    asyncio.run(run())


def test_start_job_dedup_same_url(tmp_path, cfg, monkeypatch):
    async def run():
        svc, s, fake = make_service(tmp_path, cfg)
        started = []

        async def fake_run(service, session, url, q, job):
            started.append(url)
            await asyncio.sleep(3600)

        monkeypatch.setattr(bot, "run_job", fake_run)
        await bot.start_job(svc, s, "https://likee.video/v/x", "720")
        await bot.start_job(svc, s, "https://likee.video/v/x", "720")   # дубль из pending
        await asyncio.sleep(0)                    # дать create_task стартовать
        assert started == ["https://likee.video/v/x"]
        assert len(svc.jobs) == 1
        assert svc.jobs[(s.chat_id, s.widget_id)].url == "https://likee.video/v/x"
        for job in list(svc.jobs.values()):
            job.task.cancel()
        await asyncio.sleep(0)
        svc.store.close()

    asyncio.run(run())


def test_on_text_skips_stale_update(tmp_path, cfg, monkeypatch):
    from datetime import timedelta
    async def run():
        svc, fake = fresh_service(tmp_path)
        calls = []

        async def fake_start(service, session, url, quality):
            calls.append(url)

        monkeypatch.setattr(bot, "start_job", fake_start)
        msg = Message(message_id=9, date=datetime.now(timezone.utc) - timedelta(days=1),
                      chat=Chat(id=777, type="private"),
                      text="https://likee.video/v/x")
        await bot.on_text(msg, fake, svc)
        assert calls == []
        assert not fake.sent                 # карточку тоже не трогаем
        svc.store.close()

    asyncio.run(run())


def test_run_job_does_not_pop_successor(tmp_path, cfg, monkeypatch):
    async def run():
        svc, s, fake = make_service(tmp_path, cfg)

        async def fail_plan(service, url, quality):
            raise bot.FriendlyError("тест")

        monkeypatch.setattr(bot, "inspect_plan", fail_plan)
        monkeypatch.setattr(bot, "cleanup_old", lambda service: None)
        key = (s.chat_id, s.widget_id)
        successor = bot.Job(widget_id=s.widget_id, url="next")
        svc.jobs[key] = successor
        job = bot.Job(widget_id=s.widget_id, url="old")
        await bot.run_job(svc, s, "old", "720", job)
        assert svc.jobs.get(key) is successor
        assert job.done is True
        svc.store.close()

    asyncio.run(run())


def test_config_isolation_flags(monkeypatch):
    monkeypatch.setenv("YTDLP_ISOLATE_INSPECT", "0")
    monkeypatch.setenv("DROP_PENDING_UPDATES", "0")
    monkeypatch.setenv("FAULTHANDLER_INTERVAL", "0")
    monkeypatch.setenv("YTDLP_CLI_EXTRA", "--force-ipv4 --no-check-certificates")
    c = bot.Config.from_env()
    assert c.isolate_inspect is False
    assert c.drop_pending_updates is False
    assert c.faulthandler_interval == 0
    assert c.ytdlp_cli_extra == "--force-ipv4 --no-check-certificates"


def test_friendly_error_network_cause():
    assert "Сайт не ответил" in bot.friendly_error(Exception("Unable to download webpage: TLS/SSL"))
    assert bot.friendly_error(Exception("Unsupported URL")) == "Ссылка не поддерживается"


def test_environment_lines_show_isolation(cfg):
    text = " ".join(bot.environment_lines(cfg))
    assert "subprocess" in text
    assert "drop_pending_updates" in text
