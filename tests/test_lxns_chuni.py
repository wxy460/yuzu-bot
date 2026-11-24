import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from maimai_bot.cross_game import CHUNI, CrossGameService
from maimai_bot.domain import MessageContext
from maimai_bot.features import build_router
from maimai_bot.services.community import CommunityData
from maimai_bot.services.divingfish import ScoreServiceError
from maimai_bot.services.lxns_scores import LxnsScores, ScoreSources


def song(sid, title, version):
    return {"id": sid, "title": title, "artist": "Composer", "genre": "原创",
            "bpm": 180, "version": version, "difficulties": [
                {"difficulty": 3, "level": "14+", "level_value": 14.8, "version": version}
            ]}


async def test_chuni_only_oauth_and_dual_source_through_commands(tmp_path):
    old = {"id": 317, "level_index": 3, "song_name": "Old song", "level": "14+",
           "score": 1009000, "rating": 16.95, "full_combo": "alljustice", "clear": "catastrophy"}
    new = {**old, "id": 418, "song_name": "New song", "clear": "brave"}
    removed = {**old, "id": 197, "song_name": "Archived", "clear": "hard"}
    wait_started, resume = asyncio.Event(), asyncio.Event()
    delay_records = False
    profile = {"name": "CHUNI player", "rating": 16.6}
    calls = []

    async def upstream(request):
        path = request.url.path
        calls.append(path)
        if path == "/api/chunithmprober/query/player":
            assert "authorization" not in request.headers
            assert request.content == b'{"username":"public"}'
            return httpx.Response(200, json={"username": "Other", "rating": 10.0, "records": {"b30": []}})
        assert request.url.host == "maimai.lxns.net"
        if path == "/api/v0/chunithm/song/list":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={
                "versions": [{"version": 23000, "title": "VERSE"}, {"version": 24000, "title": "X-VERSE"}],
                "songs": [song(317, "Old song", 23000), song(418, "New song", 24000), song(519, "Unplayed", 24000)],
            })
        if path == "/api/v0/chunithm/alias/list":
            return httpx.Response(200, json={"aliases": [{"song_id": 317, "aliases": ["冬日周五"]}]})
        if path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh",
                                            "expires_in": 3600, "scope": "read_player"})
        assert request.headers["Authorization"] == "Bearer access"
        if path == "/api/v0/user/maimai/player":
            return httpx.Response(404)
        if path == "/api/v0/user/chunithm/player":
            data = profile
        elif path == "/api/v0/user/chunithm/player/bests":
            data = {"bests": [old], "new_bests": [new], "selections": [removed]}
        elif path == "/api/v0/user/chunithm/player/scores":
            if delay_records:
                wait_started.set()
                await resume.wait()
            data = [new, old, removed]
        else:
            pytest.fail(path)
        return httpx.Response(200, json={"code": 200, "success": True, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        lxns = LxnsScores(http, None, tmp_path / "lxns.db", client_id="id",
                          redirect_uri="https://example.org/oauth")
        cross = CrossGameService(http, lxns=lxns, client_id="", client_secret="")
        community = CommunityData(http, tmp_path / "trends.db")
        from unittest.mock import AsyncMock

        catalog = AsyncMock()
        catalog.alias_matches.return_value = []
        catalog.search.return_value = []
        router = build_router(None, ScoreSources(None, lxns), None, None, catalog, None, None,
                              community=community, cross_games=cross)

        def ctx(command):
            return MessageContext("uid", "uid", "c2c", command, "msg")

        async def call(command):
            return await router.dispatch(ctx(command))

        assert "先" in await call("/中二数据源 落雪")
        login = await call("/lxns")
        auth = next(line for line in login.splitlines() if line.startswith("https:"))
        qs = parse_qs(urlsplit(auth).query)
        assert qs["scope"] == ["read_player"]
        assert await lxns.finish(qs["state"][0], "code") == "CHUNI player"
        assert "落雪" in await call("/chusource 落雪")
        assert "水鱼" in await call("/舞萌数据源")

        for command, expected, excluded in [
            ("/chub30", "Old song", "New song"), ("/chun20", "New song", "Old song"),
            ("/cscore z317", "1,009,000", "New song"), ("/cscorelist catastrophy", "Old song", "New song"),
            ("/cscorelist hard", "Archived", "New song"), ("/中二进度表 14+ aj", "2 / 3", "Archived"),
            ("/中二定数表 14+", "New song", "水鱼"), ("/秋什么 14+", "落雪", "水鱼"),
            ("/id z317", "Old song", "None"), ("/trend 中二", "落雪", "水鱼"),
            ("冬日周五是什么歌", "中二 z317", "暂无匹配"),
        ]:
            reply = await call(command)
            assert reply.image_png and expected in reply.text and excluded not in reply.text, command
        assert "显式" in await call("/chub30 public")
        assert "Other" in (await call("/chub30 水鱼 public")).text
        assert all("/api/chunithmprober/" not in p for p in calls[:-1])
        assert calls[-1] == urlsplit(CHUNI + "/query/player").path

        delay_records = True
        prev_history = cross.trend_key(ctx(""))
        pending = asyncio.create_task(cross.records(ctx("")))
        await asyncio.wait_for(wait_started.wait(), 2)
        assert "解除" in await call("/lxns 解绑")
        resume.set()
        with pytest.raises(ScoreServiceError, match="发生变化"):
            await pending
        count = len(calls)
        assert "绑定" in await call("/chub30")
        assert len(calls) == count
        assert "水鱼" in await call("/中二数据源 水鱼")
        assert cross.trend_key(ctx("")) != prev_history
        cross.grant.close()
