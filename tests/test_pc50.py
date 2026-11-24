import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.features import build_router
from maimai_bot.services.community import CommunityData
from maimai_bot.services.divingfish import NotBound, PlayerRecords
from maimai_bot.services.lxns_scores import ScoreSources
from maimai_bot.services.song_catalog import Chart, Song

FILE = "https://multimedia.nt.qq.com.cn/download/player.json"
MISSING = "https://files.example.org/private.json"


def ctx(command="", *, user="alice", room="room", scope="group", attached=()):
    return MessageContext(user, room, scope, command, "msg", json_urls=attached)


def backup(*rows):
    return {"userMusicList": [{"userMusicDetailList": list(rows)}]}


def archive_row(song=11469, difficulty=3, pc=174):
    return {"musicId": song, "level": difficulty, "playCount": pc,
            "achievement": 1005000, "deluxscoreMax": 2800}


class Setup:
    def __init__(self, tmp_path, http):
        self.path = tmp_path / "community.sqlite"
        self.http = http
        self.water = SimpleNamespace(history_key=lambda user: user, player_records=AsyncMock(side_effect=NotBound("未绑定")))
        self.lxns = SimpleNamespace(uses_maimai=lambda _: False)
        self.images = SimpleNamespace(render_pc50=AsyncMock(return_value=b"pc50-image"))
        self.cosmetics = SimpleNamespace(profile=AsyncMock(return_value=None))
        chart = Chart(1469, "图鉴中的曲目", "A", "G", 23000, "dx", 3, "14", 14.3, "Designer")
        self.catalog = SimpleNamespace(songs=AsyncMock(return_value=[Song(1469, chart.title, "A", "G", 180, 23000, (chart,))]))
        self.start()

    def start(self):
        unused = object()
        self.router = build_router(
            unused, ScoreSources(self.water, self.lxns), self.images, self.cosmetics,
            self.catalog, unused, unused, community=CommunityData(self.http, self.path),
        )

    async def send(self, command, **kwargs):
        return await self.router.dispatch(ctx(command, **kwargs))

    def rows(self):
        return self.images.render_pc50.await_args.args[1]


async def test_pc50_ranks_all_charts_before_cut_and_deduplicates_across_pools(tmp_path):
    app = Setup(tmp_path, None)
    # Last chart has the lowest rating, so it is outside any old/new Best 50.
    old = [{"song_id": i, "type": "SD", "level_index": 3, "level": "14", "title": f"Old {i}",
            "play_count": i, "ra": 200-i} for i in range(1, 61)]
    old += [
        {**old[0], "song_id": 99, "play_count": True},
        {**old[0], "song_id": 98, "play_count": "invalid"},
        {**old[0], "song_id": 97, "play_count": -1},
        {**old[0], "song_id": 95, "play_count": 0},
        {**old[0], "song_id": 94, "play_count": None, "count": 9999},
    ]
    new = [
        {**old[59], "play_count": 32},
        {**old[59], "type": "DX", "song_id": 10060, "play_count": 60},
        {**old[59], "type": "DX", "song_id": 10060, "level_index": 2, "play_count": 60},
        {**old[0], "song_id": 72, "play_count": 60},
    ]
    player = PlayerRecords("测试玩家", 15000, 0, "", old, new)
    app.water.player_records.side_effect = None
    app.water.player_records.return_value = player
    reply = await app.send("/pc50")
    assert isinstance(reply, BotReply) and reply.image_png == b"pc50-image"
    assert app.images.render_pc50.await_args.args[0] is player
    rows = app.rows()
    assert len(rows) == 50
    assert [(r["song_id"], r["type"], r["level_index"]) for r in rows[:4]] == [
        (60, "DX", 2), (60, "DX", 3), (60, "SD", 3), (72, "SD", 3)]
    assert all(r["play_count"] == 60 for r in rows[:4])
    assert [r["play_count"] for r in rows[4:]] == list(range(59, 13, -1))
    assert len({(r["song_id"], r["type"], r["level_index"]) for r in rows}) == 50


async def test_unbound_file_import_scope_restart_and_bad_file_preserves_snapshot(tmp_path):
    body = backup(archive_row(), archive_row(pc=91), archive_row(1469, 3, 0))
    body["ignored_private_metadata"] = "PRIVATE_FIELD_MUST_NOT_BE_STORED"
    downloads = []

    def handler(req):
        assert str(req.url) == FILE and req.method == "GET"
        assert "authorization" not in req.headers
        downloads.append(FILE)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        app = Setup(tmp_path, http)
        assert "/pc50 导入" in (await app.send("/pc50")).text
        assert "3 分钟" in await app.send("/pc50 导入")
        file = ctx(attached=(FILE,))
        assert app.router.accepts_context(file)
        for foreign in (replace(file, user_id="bob"), replace(file, chat_id="other"), replace(file, chat_scope="c2c")):
            assert not app.router.accepts_context(foreign)
        assert not downloads
        reply = await app.router.dispatch(file)
        assert reply.image_png == b"pc50-image" and downloads == [FILE]
        assert not app.router.accepts_context(file)
        assert app.images.render_pc50.await_args.args[0] is None
        assert [(r["song_id"], r["type"], r["level_index"], r["title"], r["play_count"]) for r in app.rows()] == [
            (1469, "DX", 3, "图鉴中的曲目", 174)]
        assert b"PRIVATE_FIELD_MUST_NOT_BE_STORED" not in (tmp_path / "pc50.sqlite3").read_bytes()

        app.start()
        assert (await app.send("/pc50")).image_png == b"pc50-image"
        for other in ({"user": "bob"}, {"room": "other"}, {"scope": "c2c"}):
            reply = await app.send("/pc50", **other)
            assert not reply.image_png and "174" not in reply.text
        body = backup(archive_row(pc=55), archive_row(pc=-10))
        assert isinstance(await app.send("/pc50 导入", attached=(FILE,)), str)
        assert (await app.send("/pc50")).image_png and app.rows()[0]["play_count"] == 174
        assert isinstance(await app.send("/pc50 导入", attached=(MISSING,)), str)
        assert downloads == [FILE, FILE]
        await app.send("/pc50 导入")
        assert "取消" in await app.send("/pc50 取消")
        assert not app.router.accepts_context(file)
        assert "清除" in await app.send("/pc50 清除")
        app.start()
        assert not (await app.send("/pc50")).image_png


async def test_conflicting_count_fields_do_not_replace_good_snapshot(tmp_path):
    app = Setup(tmp_path, None)
    await app.send("/pc50 导入 " + json.dumps(backup(archive_row(pc=62))))
    conflicting = dict(archive_row(pc=12), play_count=445)
    reply = await app.send("/pc50 导入 " + json.dumps(backup(conflicting)))
    assert isinstance(reply, str) and "次数" in reply
    assert (await app.send("/pc50")).image_png and app.rows()[0]["play_count"] == 62


async def test_binding_change_isolates_snapshot_and_pending_import(tmp_path):
    app = Setup(tmp_path, None)
    binding = "water-one"
    app.water.history_key = lambda _: binding
    assert (await app.send("/pc50 导入 " + json.dumps(backup(archive_row())))).image_png
    await app.send("/pc50 导入")
    binding = "water-two"
    response = await app.send("", attached=(FILE,))
    assert isinstance(response, str) and "绑定" in response
    assert not (await app.send("/pc50")).image_png

    # A separate LXNS identity in the same QQ conversation cannot read the water snapshot.
    binding = "water-one"
    app.lxns.uses_maimai = lambda _: True
    app.lxns.identity = lambda _: {"binding": "lxns-player"}
    app.lxns.records = AsyncMock(side_effect=NotBound())
    assert not (await app.send("/pc50")).image_png
    app.lxns.uses_maimai = lambda _: False
    assert (await app.send("/pc50")).image_png

    # A render awaiting external covers must not deliver the old player's result after unbinding.
    started, resume = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        await resume.wait()
        return b"old-user-png"

    app.images.render_pc50.side_effect = delayed
    task = asyncio.create_task(app.send("/pc50"))
    await asyncio.wait_for(started.wait(), 2)
    binding = "water-new"
    resume.set()
    result = await asyncio.wait_for(task, 2)
    assert isinstance(result, str) and "绑定" in result and "174" not in result


@pytest.mark.parametrize("bad", [True, -1, "2.2", "4x", None, 3.5])
async def test_invalid_inline_import_is_atomic_and_zero_is_distinct_from_missing(tmp_path, bad):
    app = Setup(tmp_path, None)
    zero = {"userMusicDetailList": [archive_row(pc=0)]}
    await app.send("/pc50 导入 " + json.dumps(zero))
    response = await app.send("/pc50")
    assert isinstance(response, str) and "0" in response
    result = await app.send("/pc50 导入 " + json.dumps([archive_row(pc=166), archive_row(pc=bad)]))
    assert isinstance(result, str)
    response = await app.send("/pc50")
    assert isinstance(response, str) and "0" in response
    app.images.render_pc50.assert_not_called()


@pytest.mark.parametrize("newer", ["clear", "replace"])
async def test_slow_attachment_cannot_overwrite_later_clear_or_import(tmp_path, newer):
    started, resume = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        assert str(request.url) == FILE
        started.set()
        await resume.wait()
        return httpx.Response(200, json=backup(archive_row(pc=999)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        app = Setup(tmp_path, http)
        slow = asyncio.create_task(app.send("/pc50 导入", attached=(FILE,)))
        await asyncio.wait_for(started.wait(), 2)
        if newer == "clear":
            await app.send("/pc50 清除")
        else:
            await app.send("/pc50 导入 " + json.dumps(backup(archive_row(pc=21))))
        resume.set()
        await asyncio.wait_for(slow, 2)
        app.start()
        current = await app.send("/pc50")
        if newer == "clear":
            assert not current.image_png
        else:
            assert current.image_png and [r["play_count"] for r in app.rows()] == [21]
