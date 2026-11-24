import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import stat
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from maimai_bot.domain import MessageContext
from maimai_bot.features import build_router
from maimai_bot.lxns_commands import start_callback
from maimai_bot.services.divingfish import ScoreServiceError
from maimai_bot.services.lxns_scores import LxnsScores, ScoreSources
from maimai_bot.services.song_catalog import Chart, Song


class Catalog:
    def version_name(self, version):
        return str(version // 1000)

    async def songs(self):
        def song(sid, ver, kind):
            chart = Chart(
                sid, f"Song {sid}", "Artist", "Game", ver, kind, 3, "14", 14.2, "designer"
            )
            return Song(sid, chart.title, "Artist", "Game", 170, ver, (chart,))

        return song(1, 10000, "dx"), song(2, 17000, "standard")


class Water:
    enabled = True

    async def best50(self, user):
        return "water-best"

    async def player_records(self, user):
        return "water-records"

    async def start_binding(self, user):
        return "water-bind"


class Upstream:
    def __init__(self):
        self.verifiers = []
        self.refresh = []
        self.paths = []

    def __call__(self, request):
        path = request.url.path
        assert request.url.host == "maimai.lxns.net"
        if path.endswith("/oauth/token"):
            body = json.loads(request.content)
            if body["grant_type"] == "authorization_code":
                assert body["code"] == "browser-code"
                self.verifiers.append(body["code_verifier"])
            else:
                self.refresh.append(body["refresh_token"])
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 900,
                    "scope": "read_player",
                },
            )
        assert request.headers["Authorization"] == "Bearer access"
        self.paths.append(path)
        old = {
            "id": 1,
            "type": "dx",
            "level_index": 3,
            "dx_rating": 307.6,
            "achievements": 100.02,
            "fc": "ap",
            "rate": "sss",
            "fs": "fsd",
            "dx_score": 1234,
            "playCount": "17",
        }
        new = {
            "id": 2,
            "type": "standard",
            "level_index": 3,
            "dx_rating": 302,
            "achievements": 99.9,
            "fc": "fcp",
            "rate": "ssp",
            "count": 23,
            "total_play_count": 349,
        }
        if path == "/api/v0/user/maimai/player":
            data = {
                "name": "Test player",
                "rating": 14555,
                "icon": {"id": 100},
                "name_plate": {"name": "Never use LXNS cosmetics"},
            }
        elif path.endswith("/bests"):
            data = {"standard": [old], "dx": [new]}
        elif path.endswith("/scores"):
            data = [old, new]
        elif path.endswith("/recents"):
            data = [dict(old, play_time="2026-01-01T10:40:00Z")] * 2
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "code": 200, "data": data})


def service(http, path):
    return LxnsScores(
        http,
        Catalog(),
        path,
        client_id="test-app",
        redirect_uri="https://bot.example/authorize/lxns",
    )


def state(url):
    return parse_qs(urlsplit(url).query)["state"][0]


async def test_private_authorization_cross_scope_pairing_persistence_and_unlink(tmp_path):
    up = Upstream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(up)) as http:
        lxns = service(http, tmp_path / "scores.sqlite")
        sources = ScoreSources(Water(), lxns)
        unused = object()
        images, cosmetics = AsyncMock(), AsyncMock()
        images.render.return_value = b"rendered"
        cosmetics.profile.return_value = "local choice"
        router = build_router(unused, sources, images, cosmetics, unused, unused, unused)

        group_user = MessageContext("g-a", "room", "group", "", "")
        private_user = MessageContext("u-a", "u-a", "c2c", "", "")

        async def send(who, scope, text):
            return await router.dispatch(MessageContext(who, "room", scope, text, "message"))

        assert await sources.best50(group_user) == "water-best"
        assert not lxns.path.exists()
        group = await send("g-a", "group", "/lxns")
        ref = re.search(r"关联 ([0-9a-f]+)", group)[1]
        assert "https://" not in group
        assert "先" in await send("g-a", "group", "/source 落雪")
        assert "私聊" in await send("g-a", "group", "/lxns 关联 " + ref)
        prompt = await send("u-a", "c2c", "/lxns")
        url = next(line for line in prompt.splitlines() if line.startswith("https://"))
        args = parse_qs(urlsplit(url).query)
        assert args["scope"] == ["read_player"] and "code_verifier" not in args
        assert await lxns.finish(state(url), "browser-code") == "Test player"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(up.verifiers[0].encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert args["code_challenge"] == [challenge]
        assert stat.S_IMODE(lxns.path.stat().st_mode) == 0o600
        with pytest.raises(ScoreServiceError):
            await lxns.finish(state(url), "browser-code")

        assert "Test player" in await send("u-a", "c2c", "/lxns 关联 " + ref)
        assert "尚未" in await send("g-b", "group", "/lxns 确认 Test player")
        assert "尚未" in await send("g-a", "group", "/lxns 确认 other")
        assert "已关联" in await send("g-a", "group", "/lxns 确认 Test player")
        assert "水鱼" in await send("g-a", "group", "/舞萌数据源")
        menu = await send("g-a", "group", "绑定账号")
        assert "当前舞萌查分源为水鱼" in menu.text
        assert [a.command for a in menu.quick_actions] == ["/bind", "/lxns"]
        assert "使用落雪" in await send("g-a", "group", "/source 落雪")
        assert sources.source(private_user) == "水鱼"
        assert sources.source(MessageContext("g-a", "other", "group", "", "")) == "水鱼"
        assert await sources.start_binding("g-a") == "water-bind"
        assert sources.history_key(group_user).startswith("lxns:")
        assert ScoreSources(Water(), service(http, lxns.path)).source(group_user) == "落雪"

        best = await sources.best50(group_user)
        assert best.plate == "" and best.nickname == "Test player"
        assert (best.old[0]["song_id"], best.new[0]["song_id"]) == (10001, 2)
        b50 = await send("g-a", "group", "/b50")
        assert b50.image_png == b"rendered"
        images.render.assert_awaited_once_with(best, cosmetics="local choice", avatar_url=None)
        assert best.old[0]["ra"] == 307
        assert best.old[0]["ds"] == 14.2
        assert best.old[0]["play_count"] == 17
        assert "play_count" not in best.new[0]
        assert (await sources.player_records(group_user)).new[0]["type"] == "SD"
        assert "/api/v0/user/maimai/player/bests" in up.paths
        assert "/api/v0/user/maimai/player/scores" in up.paths

        # A private rebind revokes group access even when the new name matches.
        await lxns.finish(state(lxns.begin("c2c:u-a")), "browser-code")
        assert sources.source(group_user) == "落雪"
        assert not lxns.identity("group:room:g-a")
        assert "/source 水鱼" in await send("g-a", "group", "/b50")
        with pytest.raises(ScoreServiceError, match="重新关联"):
            await sources.player_records(group_user)
        assert ScoreSources(Water(), service(http, lxns.path)).source(group_user) == "落雪"
        assert "使用水鱼" in await send("g-a", "group", "/source 水鱼")
        assert await sources.best50(group_user) == "water-best"
        assert "已解除" in await send("u-a", "c2c", "/解绑落雪")
        assert not lxns.identity("c2c:u-a")


async def test_rotation_single_use_and_callbacks(tmp_path):
    up = Upstream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(up)) as http:
        lxns = service(http, tmp_path / "scores.sqlite")
        old = state(lxns.begin("alice"))
        new = state(lxns.begin("alice"))
        with pytest.raises(ScoreServiceError):
            await lxns.finish(old, "browser-code")
        await lxns.finish(new, "browser-code")
        with lxns.db() as db:
            db.execute("UPDATE grants SET expires=0")
        tokens = await asyncio.gather(*(lxns.access_token("alice") for _ in range(4)))
        assert tokens == ["access"] * 4 and up.refresh == ["refresh"]
        assert len((await lxns.records("alice", mode="recents")).old) == 2
        # Tokens never enter route responses and cancellation invalidates pending state.
        runner = await start_callback(lxns, 0)
        try:
            port = runner.addresses[0][1]
            base = f"http://127.0.0.1:{port}/authorize/lxns"
            async with httpx.AsyncClient(trust_env=False) as browser:
                fail = await browser.get(base, params={"state": "bad", "code": "browser-code"})
                assert fail.status_code == 400
                valid = await browser.get(
                    base, params={"state": state(lxns.begin("alice")), "code": "browser-code"}
                )
                assert valid.status_code == 200 and "Test player" in valid.text
                assert "access" not in valid.text and valid.headers["Cache-Control"] == "no-store"
        finally:
            await runner.cleanup()


async def test_unconfigured_uses_only_water(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: pytest.fail(str(req.url)))
    ) as http:
        lxns = LxnsScores(http, Catalog(), tmp_path / "scores.sqlite")
        sources = ScoreSources(Water(), lxns)
        assert await start_callback(lxns, "unused") is None
        broken = service(http, tmp_path / "invalid-config.sqlite")
        assert await start_callback(broken, "invalid") is None and not broken.enabled
        assert await sources.best50(MessageContext("user", "g", "group", "", "")) == "water-best"
        unused = object()
        router = build_router(unused, sources, unused, unused, unused, unused, unused)
        msg = MessageContext("user", "g", "group", "/source 落雪", "id")
        assert "尚未启用" in await router.dispatch(msg)
        assert not lxns.path.exists()


def test_legacy_active_choice_survives_missing_grant_and_manual_switch(tmp_path):
    path = tmp_path / "scores.sqlite"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE subjects(user TEXT PRIMARY KEY,owner TEXT,active INTEGER)")
        legacy.execute("INSERT INTO subjects VALUES('group:g:a','old',1)")
    lxns = service(None, path)
    ctx = MessageContext("a", "g", "group", "", "")
    assert ScoreSources(Water(), lxns).source(ctx) == "落雪"
    assert not lxns.identity("group:g:a")
    lxns.select("group:g:a", False)
    assert ScoreSources(Water(), service(None, path)).source(ctx) == "水鱼"
