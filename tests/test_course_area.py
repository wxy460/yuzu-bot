from types import SimpleNamespace
from unittest.mock import AsyncMock

from maimai_bot.community_commands import register_community
from maimai_bot.domain import MessageContext
from maimai_bot.router import CommandRouter


async def test_area_partial_name_real_id_and_cumulative_milestones():
    data = {
        "map_ids": [{"id": 150006, "name": "咻哇乐园区域2"}],
        "map_rewards": [
            {
                "mapId": 150006,
                "treasures": [
                    {
                        "distance": i * 3000000,
                        "treasureType": "NamePlate",
                        "treasureId": {"str": f"奖励{i}"},
                    }
                    for i in range(1, 14)
                ],
            }
        ],
        "areas": [],
    }

    async def fetch(key):
        return data[key]

    router = CommandRouter(None)
    register_community(router, SimpleNamespace(fetch=fetch), None, None, None)

    async def run(cmd):
        return await router.dispatch(MessageContext("uid", "group", "group", cmd, "id"))

    results = await run("/asearch 咻哇")
    assert "m150006" in results.text
    page1 = await run("/ainfo m150006")
    assert "3,000 km" in page1.text and "奖励13" not in page1.text
    page2 = await run("/ainfo 150006 p2")
    assert "39,000 km" in page2.text and "奖励13" in page2.text and page2.image_png


async def test_fixed_course_personal_records_match_type_and_difficulty():
    scores = SimpleNamespace(
        player_records=AsyncMock(
            return_value=SimpleNamespace(
                old=[
                    {"song_id": 11460, "type": "DX", "level_index": 3, "achievements": 99.9876},
                    {"song_id": 1460, "type": "SD", "level_index": 3, "achievements": 100.9999},
                ],
                new=[],
            )
        )
    )
    router = CommandRouter(None)
    register_community(
        router, None, scores, SimpleNamespace(songs=AsyncMock(return_value=())), None
    )
    result = await router.dispatch(MessageContext("uid", "g", "group", "/段位表 真七段", "id"))
    assert result.text.count("TRACK") == 4 and result.text.count("未记录") == 3
    assert "99.9876%" in result.text and "100.9999%" not in result.text and result.image_png
