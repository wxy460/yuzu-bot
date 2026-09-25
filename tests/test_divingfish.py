import hashlib

import httpx
import pytest

from maimai_bot.services.divingfish import DivingFishService, NotBound


async def test_binding_uses_client_scoped_subject_ref() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(item.split("=", 1) for item in request.content.decode().split("&")))
        return httpx.Response(
            200,
            json={
                "verification_uri_complete": "https://auth.test/device?code=A",
                "user_code": "ABCD-EFGH",
                "expires_in": 600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DivingFishService(client, client_id="client", client_secret="secret")
        result = await service.start_binding("openid")

    expected = hashlib.sha256(b"client:openid").hexdigest()
    assert captured["subject_ref"] == expected
    assert result.user_code == "ABCD-EFGH"


async def test_best50_reports_not_bound() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "consent_required"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DivingFishService(client, client_id="client", client_secret="secret")
        with pytest.raises(NotBound):
            await service.best50("openid")


async def test_best50_splits_and_sorts_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 300})
        is_new = request.url.params.get("is_new") == "true"
        scores = [
            {
                "title": "new-low" if is_new else "old-low",
                "ra": 1,
                "achievements": 99,
                "level": "12",
                "level_label": "Master",
            },
            {
                "title": "new-high" if is_new else "old-high",
                "ra": 9,
                "ds": 14.7,
                "achievements": 100.5,
                "song_id": 30,
                "level": "13",
                "level_label": "Master",
            },
            {
                "title": "same-ra-lower-achievement",
                "ra": 9,
                "ds": 14.8,
                "achievements": 100.4,
                "song_id": 1,
                "level": "13",
                "level_label": "Master",
            },
            {
                "title": "same-ra-achievement-lower-id",
                "ra": 9,
                "ds": 14.7,
                "achievements": 100.5,
                "song_id": 12,
                "level": "13",
                "level_label": "Master",
            },
        ]
        return httpx.Response(
            200,
            json={"nickname": "Mai", "rating": 15000, "records": scores},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DivingFishService(client, client_id="client", client_secret="secret")
        result = await service.best50("openid")

    assert [item["title"] for item in result.old[:3]] == [
        "same-ra-lower-achievement",
        "same-ra-achievement-lower-id",
        "old-high",
    ]
    assert [item["title"] for item in result.new[:3]] == [
        "same-ra-lower-achievement",
        "same-ra-achievement-lower-id",
        "new-high",
    ]
    assert "15000" in result.as_text()


async def test_player_records_keeps_scores_outside_best50() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 300})
        return httpx.Response(
            200,
            json={
                "nickname": "Mai",
                "rating": 15000,
                "records": [{"song_id": index, "ra": index} for index in range(50)],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DivingFishService(client, client_id="client", client_secret="secret")
        records = await service.player_records("openid")

    assert len(records.old) == 50
    assert len(records.new) == 50
    assert len(records.best50().old) == 35
    assert len(records.best50().new) == 15
