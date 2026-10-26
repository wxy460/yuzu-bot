import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.features import build_router
from maimai_bot.qq_adapter import QQOfficialAdapter, _image_attachment_urls
from maimai_bot.services.bilibili import ChartConfirmationResult
from maimai_bot.services.song_catalog import Song
from maimai_bot.services.song_recognition import (
    RecognitionError,
    RecognitionResult,
    SongRecognitionService,
    attachment_url_allowed,
    text_candidates,
)

SONG = Song(100, "Fragrance", "Tsukasa", "maimai", 180, 15000, ())
URL = "https://multimedia.nt.qq.com.cn/download?file=example"


def context(text="", **kwargs):
    return replace(MessageContext("alice", "group", "group", text, "message"), **kwargs)


class RecognizerStub:
    def __init__(self):
        self.calls = []

    def start_warming(self):
        pass

    async def recognize(self, url):
        self.calls.append(url)
        return RecognitionResult(SONG, method="曲名")


class CatalogStub:
    async def search(self, query):
        return [SONG]

    def version_name(self, version):
        return "maimai"


class ImagesStub:
    async def render_song_info(self, song, version):
        return b"song-details"


class BilibiliStub:
    async def confirmations(self, song):
        return ChartConfirmationResult(song.title, ())


def make_router():
    recognizer = RecognizerStub()
    unused = object()
    router = build_router(
        unused, unused, ImagesStub(), unused, CatalogStub(), unused, BilibiliStub(), recognizer
    )
    return router, recognizer


async def test_next_image_is_scoped_to_sender_chat_and_expires(monkeypatch):
    router, recognizer = make_router()
    clock = [100.0]
    monkeypatch.setattr("maimai_bot.features.time.monotonic", lambda: clock[0])
    assert "3 分钟" in await router.dispatch(context("/识图"))
    image = context(image_urls=(URL,))
    assert not router.accepts_context(replace(image, user_id="bob"))
    assert not router.accepts_context(replace(image, chat_id="other"))
    assert not router.accepts_context(context("普通群聊"))
    assert router.accepts_context(image)
    answer = await router.dispatch(image)
    assert isinstance(answer, BotReply)
    assert answer.image_png == b"song-details"
    assert "Fragrance" in answer.followup_text
    assert recognizer.calls == [URL]
    assert not router.accepts_context(image)
    await router.dispatch(context("/识图"))
    clock[0] = 281.0
    assert not router.accepts_context(image)


async def test_attached_command_and_cancel():
    router, recognizer = make_router()
    answer = await router.dispatch(context("/recognize", image_urls=(URL,)))
    assert isinstance(answer, BotReply)
    await router.dispatch(context("成绩识图"))
    assert "已取消" in await router.dispatch(context("/识图 取消"))
    assert not router.accepts_context(context(image_urls=(URL,)))
    assert recognizer.calls == [URL]
    assert "/识图" in router.help_text("曲目")


async def test_official_adapter_delivers_followup_image_without_mention(monkeypatch):
    import qqbot_agent_sdk as sdk

    router, recognizer = make_router()
    stop = asyncio.Event()
    sent = []

    class Api:
        def __init__(self, **kwargs):
            pass

        def setup(self, http):
            pass

        async def ensure_token(self):
            pass

        async def get_gateway_url(self):
            return "wss://example.test"

        async def send_text(self, *args, **kwargs):
            sent.append(args)

        def __getattr__(self, name):
            return lambda *args: None

    class Socket:
        def __init__(self, *, callbacks, **kwargs):
            self.callbacks = callbacks
            self.task = None

        def start(self, url, loop):
            async def events():
                try:
                    base = {"group_openid": "group", "author": {"member_openid": "alice"}}
                    command = {**base, "id": "command", "content": "/识图"}
                    image = {**base, "id": "image", "content": "", "attachments": [
                        {"url": URL, "content_type": "image/jpeg"}
                    ]}
                    await self.callbacks.on_message_event(
                        "GROUP_MESSAGE_CREATE", {**base, "id": "empty", "content": ""}
                    )
                    assert not sent
                    await self.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", command)
                    await self.callbacks.on_message_event("GROUP_MESSAGE_CREATE", image)
                    await self.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", image)
                finally:
                    stop.set()
            self.task = loop.create_task(events())

        async def async_stop(self):
            await self.task

    async def send_png(*args):
        sent.append("image")

    monkeypatch.setattr(sdk, "QQApiClient", Api)
    monkeypatch.setattr(sdk, "QQWebSocket", Socket)
    monkeypatch.setattr(sdk, "MediaUploader", lambda **kwargs: object())
    monkeypatch.setattr("maimai_bot.qq_adapter._send_png", send_png)
    async with httpx.AsyncClient() as http:
        adapter = QQOfficialAdapter(
            app_id="123", app_secret="unused", http=http, handler=router.dispatch,
            unmentioned_group_filter=router.recognizes_command,
            unmentioned_context_filter=router.accepts_context,
        )
        await adapter.run(stop)
    assert recognizer.calls == [URL]
    assert "image" in sent


def test_attachments_only_take_images_and_normalize_urls():
    attachments = [
        SimpleNamespace(content_type="image/jpeg", url="//gchat.qpic.cn/a"),
        SimpleNamespace(content_type="image/png", url="multimedia.nt.qq.com.cn/b"),
        SimpleNamespace(content_type="video/mp4", url=URL),
    ]
    assert _image_attachment_urls(attachments) == (
        "https://gchat.qpic.cn/a", "https://multimedia.nt.qq.com.cn/b"
    )


def test_title_matching_preserves_ambiguity_and_ignores_rating():
    other = replace(SONG, id=200, title="Fragrance Remix")
    assert text_candidates(["100.5000%", "15752", "326"], (SONG, other)) == []
    assert text_candidates(["Ｆｒａｇｒａｎｃｅ"], (SONG,))[0][0] == 1.0
    ranked = text_candidates(["Fragrance", "Fragrance Remix"], (SONG, other))
    assert ranked[0][0] == ranked[1][0]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a", "https://qq.com.attacker.test/a", "file:///etc/passwd",
    "https://user:password@qq.com/a", "https://qq.com:8080/a",
])
def test_rejects_non_attachment_urls(url):
    assert not attachment_url_allowed(url)


async def test_download_checks_redirects_and_size(tmp_path, monkeypatch):
    monkeypatch.setattr("maimai_bot.services.song_recognition.MAX_BYTES", 10)
    def handler(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        return httpx.Response(200, content=b"x" * 11)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = SongRecognitionService(http, CatalogStub(), tmp_path)
        with pytest.raises(RecognitionError, match="地址"):
            await service._download("https://qq.com/redirect")
        with pytest.raises(RecognitionError, match="12 MB"):
            await service._download("https://qq.com/large")


def test_real_ocr_reads_title_and_rejects_invalid_image(tmp_path):
    image = Image.new("RGB", (900, 500), "#ded0ff")
    draw = ImageDraw.Draw(image)
    # Pillow's built-in font avoids a platform-specific font dependency in CI.
    draw.text((70, 70), "Fragrance", font=ImageFont.load_default(size=60), fill="black")
    draw.text((70, 200), "ACHIEVEMENT 100.5000%", font=ImageFont.load_default(size=30))
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    service = SongRecognitionService(None, None, tmp_path)
    result = service._recognize(raw.getvalue(), (SONG,))
    assert result.song == SONG
    with pytest.raises(RecognitionError, match="图片"):
        service._recognize(b"not an image", (SONG,))


def test_visual_matches_scaled_jacket_and_rejects_blank(tmp_path):
    import cv2
    import numpy as np

    rng = np.random.default_rng(42)
    jacket = np.full((256, 256, 3), 220, dtype=np.uint8)
    for _ in range(65):
        center = tuple(int(x) for x in rng.integers(10, 246, 2))
        color = tuple(int(x) for x in rng.integers(0, 255, 3))
        cv2.circle(jacket, center, int(rng.integers(3, 22)), color, -1)
    cv2.imwrite(str(tmp_path / "100.jpg"), jacket)
    service = SongRecognitionService(None, None, tmp_path)
    service._build_visual()
    screenshot = np.full((900, 650, 3), 240, dtype=np.uint8)
    screenshot[100:420, 120:440] = cv2.resize(jacket, (320, 320))
    assert service._match_visual(screenshot) == 100
    assert service._match_visual(np.zeros((600, 600, 3), dtype=np.uint8)) is None
