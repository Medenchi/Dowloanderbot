from __future__ import annotations

import asyncio
import json
import os
import struct
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
    assert "+" not in bot.format_selector("720", False)   # без ffmpeg только прогрессив
    assert "bestvideo" in bot.format_selector("1080", True)


def test_ydl_opts_cap(tmp_path, cfg):
    o = bot.ydl_opts(cfg, "720", download=False)
    assert o["ignore_no_formats_error"] is True
    assert o["max_filesize"] == cfg.max_original_bytes
    assert o["playlistend"] == cfg.max_slides


def test_classify_path(tmp_path):
    assert bot.classify_path(tmp_path / "a.mp4") == "video"
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
