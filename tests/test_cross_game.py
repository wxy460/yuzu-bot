from urllib.parse import parse_qs

import httpx

from maimai_bot.cross_game import CHUNI, CrossGameService, register_cross_game
from maimai_bot.domain import MessageContext
from maimai_bot.router import CommandRouter
from maimai_bot.services.divingfish import DivingFishService


async def test_chuni_records_are_isolated_and_progress_includes_unplayed():
    song = {
        "id": 317,
        "title": "FRIDAY",
        "level": ["14+", "14+"],
        "ds": [14.5, 14.9],
        "cids": [12, 32],
        "basic_info": {"from": "CHUNITHM SUN PLUS", "genre": "Original"},
    }
    records = [
        {
            "cid": 12,
            "mid": 317,
            "title": "FRIDAY",
            "level": "14+",
            "ds": 14.5,
            "level_label": "Basic",
            "score": 1000000,
            "fc": "alljustice",
            "ra": 15.5,
        }
    ]
    grants = []

    def handle(request):
        if request.url.path == "/oauth/device_authorization":
            grants.append(parse_qs(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "verification_uri_complete": "https://auth.diving-fish.com/example",
                    "user_code": "ABCD",
                    "expires_in": 300,
                },
            )
        if request.url.path == "/oauth/token":
            granted = parse_qs(request.content.decode())
            assert granted["scope"] == ["chunithm.records.read"]
            assert granted["subject"] == ["ref:" + grants[-1]["subject_ref"][0]]
            return httpx.Response(200, json={"access_token": "chuni-only"})
        if request.url.path.endswith("music_data"):
            return httpx.Response(200, json=[song])
        if request.url.path.endswith("/alias/list"):
            return httpx.Response(200, json={"aliases": [{"song_id": 317, "aliases": ["周五"]}]})
        assert request.url == f"{CHUNI}/player/records"
        assert request.headers["authorization"] == "Bearer chuni-only"
        return httpx.Response(200, json={"records": {"best": records}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        main = DivingFishService(http, client_id="id", client_secret="secret")
        service = CrossGameService(http, client_id="id", client_secret="secret")
        router = CommandRouter(None)
        register_cross_game(router, service)

        async def call(command):
            return await router.dispatch(MessageContext("uid", "g", "group", command, "msg"))

        await main.start_binding("uid")
        binding = await call("/中二绑定")
        assert "ABCD" in binding.text
        assert grants[0]["scope"] == ["prober.records.read"]
        assert grants[0]["subject_ref"] != grants[1]["subject_ref"]
        assert grants[1]["scope"] == ["chunithm.records.read"]
        for query in ("/cscore 周五", "/cscore z317", "/cscorelist aj", "/cscorelist sun plus"):
            result = await call(query)
            assert result.image_png
            assert "1,000,000" in result.text
        progress = await call("/中二进度表 14+ aj")
        assert progress.image_png and "1 / 2" in progress.text and "未游玩" in progress.text


async def test_public_b30_preserves_upstream_group_and_never_uses_oauth():
    import json

    def handle(request):
        assert "authorization" not in request.headers
        assert request.url == f"{CHUNI}/query/player"
        assert json.loads(request.content) == {"username": "alice"}
        row = {"title": "OLD", "level_label": "Master", "score": 1009000, "ra": 16, "level": "14+"}
        return httpx.Response(
            200,
            json={
                "rating": 16,
                "username": "alice",
                "records": {
                    "b30": [row] * 30,
                    "n20": [{**row, "title": "NEW"}],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        router = CommandRouter(None)
        register_cross_game(router, CrossGameService(http, client_id="", client_secret=""))
        result = await router.dispatch(MessageContext("uid", "g", "group", "/chub30 alice", "m"))
        assert result.image_png
        assert result.text.count("OLD") == 30 and "NEW" not in result.text


async def test_water_chunithm_unlink_survives_restart(tmp_path):
    def no_traffic(request):
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_traffic)) as http:
        def start():
            service = CrossGameService(http, client_id="id", client_secret="secret",
                                       binding_state=tmp_path / "chuni.sqlite")
            router = CommandRouter(None)
            register_cross_game(router, service)
            return service, router

        ctx = MessageContext("uid", "room", "group", "/chubind 解绑", "msg")
        service, router = start()
        assert service.trend_key(ctx) == "chuni:uid"
        assert "已解除" in await router.dispatch(ctx)
        new_key = service.trend_key(ctx)
        assert new_key != "chuni:uid"
        service.grant.close()
        service, router = start()
        assert service.trend_key(ctx) == new_key
        assert "授权" in await router.dispatch(MessageContext("uid", "room", "group", "/chub30", "msg"))
        service.grant.close()
