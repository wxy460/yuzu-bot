import random
import re
import time
from unittest.mock import AsyncMock

from maimai_bot import campus_leisure
from maimai_bot.domain import MessageContext
from maimai_bot.features import build_router
from maimai_bot.services.song_catalog import Chart, Song, SongCatalogService


def context(text):
    return MessageContext("a", "g", "group", text, "m")


async def test_tarot_complete_deck_orientations_and_three_distinct_positions(monkeypatch):
    names = [name for name, normal, inverse in campus_leisure.DECK if normal != inverse]
    assert len(names) == len(set(names)) == 78
    router = build_router(*(None,) * 7)
    monkeypatch.setattr(campus_leisure, "random", random.Random(123))
    positions_and_faces = set()
    for i in range(4):
        reply = await router.dispatch(context("/塔罗牌3"))
        assert reply.image_png.startswith(b"\x89PNG")
        entries = re.findall(
            r"^(过去|现在|未来)：(.+) · (正位|逆位)\n([^\n]+)", reply.text, re.MULTILINE
        )
        assert [p for p, *_ in entries] == ["过去", "现在", "未来"]
        assert len({name for _, name, *_ in entries}) == 3
        for _, name, face, reading in entries:
            table = next(record for record in campus_leisure.DECK if record[0] == name)
            assert reading == table[1 if face == "正位" else 2]
            positions_and_faces.add(face)
    assert positions_and_faces == {"正位", "逆位"}
    assert "今日一张" in (await router.dispatch(context("塔罗牌"))).text


async def test_request_song_matches_catalog_and_renders_same_detail_handler():
    catalog = SongCatalogService(None)

    def song(sid, title, artist):
        chart = Chart(sid, title, artist, "test", 16000, "dx", 3, "14+", 14.6, "artist")
        return Song(sid, title, artist, "test", 155, 16000, (chart,))

    titles = [
        song(317, "FRIDAY", "abc"),
        song(318, "FRIDAY NIGHT", "abc"),
        song(100, "ALIEN", "def"),
    ]
    catalog._songs = tuple(titles)
    catalog._loaded_at = time.monotonic()
    catalog._aliases = {317: ("周五",), 318: ("周五夜",)}
    images = AsyncMock()
    images.render_song_info.return_value = b"song-card"
    router = build_router(None, None, images, None, catalog, None, None)
    for command, expected in [
        ("/点歌 周五", {"FRIDAY"}),
        ("/推歌 FRIDAY", {"FRIDAY"}),
        ("/点歌 10317", {"FRIDAY"}),
        ("推歌 abc", {"FRIDAY", "FRIDAY NIGHT"}),
        ("/推歌", {s.title for s in titles}),
    ]:
        reply = await router.dispatch(context(command))
        rendered, _ = images.render_song_info.await_args.args
        assert rendered.title in expected
        assert reply.image_png == b"song-card" and rendered.title in reply.text
        assert images.render_song_info.await_args.kwargs["headline"] == "柚子的点歌单"
    images.render_song_info.reset_mock()
    assert "没有找到" in await router.dispatch(context("/点歌 不存在的曲名"))
    images.render_song_info.assert_not_awaited()
    assert router.recognizes_command("每日推歌") and router.recognizes_command("随个14+")
