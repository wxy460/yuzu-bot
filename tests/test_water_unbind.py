import asyncio
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from maimai_bot.cross_game import _ChuniGrant
from maimai_bot.domain import MessageContext
from maimai_bot.features import build_router
from maimai_bot.services.divingfish import AUTHORIZED_APPS, DivingFishService, NotBound
from maimai_bot.services.lxns_scores import ScoreSources


class WaterServer:
    def __init__(self):
        self.consents = set()
        self.requests = []
        self.pending = None

    def __call__(self, request):
        fields = parse_qs(request.content.decode())
        self.requests.append((request.url.path, fields))
        if request.url.path.endswith("device_authorization"):
            self.pending = fields["subject_ref"][0]
            return httpx.Response(
                200,
                json={
                    "verification_uri_complete": "https://auth.test/device",
                    "user_code": "ABCDE",
                    "expires_in": 300,
                },
            )
        if request.url.path.endswith("/token"):
            ref = fields["subject"][0].removeprefix("ref:")
            if ref not in self.consents:
                return httpx.Response(400, json={"error": "consent_required"})
            return httpx.Response(200, json={"access_token": ref, "expires_in": 300})
        return httpx.Response(
            200, json={"nickname": "private-score-name", "rating": 15000, "records": []}
        )


class Chat:
    async def reply(self, conversation, text, **kwargs):
        return conversation + "\n" + kwargs["user_context"]


class Lxns:
    def uses_maimai(self, user):
        return True

    def identity(self, user):
        return {"binding": "separate", "active": 1}


async def test_self_unbind_persists_and_quarantines_all_chat_rooms(tmp_path):
    upstream = WaterServer()
    state = tmp_path / "water.sqlite"
    unused = object()
    ctx = MessageContext("alice", "group", "group", "", "msg")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        water = DivingFishService(http, client_id="test", client_secret="s", binding_state=state)
        sources = ScoreSources(water, Lxns())
        chuni = _ChuniGrant(http, client_id="test", client_secret="s")
        router = build_router(Chat(), sources, unused, unused, unused, unused, unused)

        async def run(content, **updates):
            return await router.dispatch(replace(ctx, content=content, **updates))

        original = water._subject_ref("alice")
        upstream.consents.update({original, water._subject_ref("bob"), chuni._subject_ref("alice")})
        old = await run("你好")
        assert "private-score-name" in old and old.startswith("group:group:alice\n")
        assert "private-score-name" in await run("你好", chat_id="other")
        link = await run("/unbind")
        assert link.quick_actions[1].url == AUTHORIZED_APPS
        assert "仅可操作" in await run("/unbind bob")
        assert (await water.best50("alice")).rating == 15000
        notice = await run("/unbind 确认")
        assert "水鱼官网的授权由你本人管理" in notice.text
        previous_count = len(upstream.requests)
        current = await run("你好", chat_id="other")
        assert "private-score-name" not in current and "尚未绑定" in current
        assert not current.startswith("group:other:alice\n")
        with pytest.raises(NotBound):
            await water.best50("alice")
        assert len(upstream.requests) == previous_count
        assert (await water.best50("bob")).rating == 15000
        assert sources.history_key(ctx) == "lxns:separate:group:group:alice"
        assert sources.source(ctx) == "落雪"
        assert await chuni.headers("alice") == {
            "Authorization": "Bearer " + chuni._subject_ref("alice")
        }
        water.close()
        chuni.close()

        water = DivingFishService(http, client_id="test", client_secret="s", binding_state=state)
        with pytest.raises(NotBound):
            await water.player_records("alice")
        await water.start_binding("alice")
        assert upstream.pending != original
        with pytest.raises(NotBound):
            await water.player_records("alice")
        upstream.consents.add(upstream.pending)
        assert (await water.best50("alice")).rating == 15000
        water.close()


async def test_server_revocation_invalidates_cache_at_token_expiry(monkeypatch, tmp_path):
    upstream = WaterServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        water = DivingFishService(http, client_id="test", client_secret="s")
        clock = [1000]
        monkeypatch.setattr(
            "maimai_bot.services.divingfish.time", type("Clock", (), {"time": lambda: clock[0]})
        )
        router = build_router(Chat(), water, *([object()] * 5))
        chat = MessageContext("player", "room", "group", "你好", "msg")
        original = water._subject_ref("player")
        upstream.consents.add(original)
        assert "private-score-name" in await router.dispatch(chat)
        upstream.consents.remove(original)
        clock[0] += 301
        assert "private-score-name" not in await router.dispatch(chat)
        assert "尚未绑定" in await router.dispatch(chat)
        assert water.history_key("player") != "player"
        water.close()


async def test_unbinding_while_score_request_is_in_flight():
    upstream = WaterServer()
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(request):
        if request.url.path.endswith("/records"):
            started.set()
            await release.wait()
        return upstream(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(hold)) as http:
        water = DivingFishService(http, client_id="test", client_secret="s")
        upstream.consents.add(water._subject_ref("alice"))
        pending = asyncio.create_task(water.player_records("alice"))
        await asyncio.wait_for(started.wait(), 3)
        water.unlink("alice")
        release.set()
        with pytest.raises(NotBound):
            await pending
        assert not water.has_current_token("alice")
        water.close()
