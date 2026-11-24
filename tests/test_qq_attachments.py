import httpx
import pytest

from maimai_bot.services import qq_attachments


async def test_qq_json_checks_each_hop_and_enforces_stream_limit(monkeypatch):
    monkeypatch.setattr(qq_attachments, "MAX_ATTACHMENT_BYTES", 10)
    hits = []

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"[12345"
            yield b"67890]"

    def handle(request):
        hits.append(request.url.host)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://gchat.qpic.cn/result"})
        if request.url.path == "/result":
            return httpx.Response(200, content=b'{"ok":1}')
        if request.url.path == "/large":
            return httpx.Response(200, stream=ChunkedBody())
        if request.url.path == "/cycle":
            return httpx.Response(302, headers={"Location": "/cycle"})
        return httpx.Response(302, headers={"Location": "https://outside.example/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert await qq_attachments.download_qq_json(http, "https://qq.com/start") == b'{"ok":1}'
        for url in (
            "https://qq.com.evil.test/file",
            "https://qq.com/external",
            "https://qq.com:444/test",
            "https://qq.com/large",
            "https://qq.com/cycle",
        ):
            with pytest.raises(ValueError):
                await qq_attachments.download_qq_json(http, url)
    assert set(hits) == {"qq.com", "gchat.qpic.cn"}
