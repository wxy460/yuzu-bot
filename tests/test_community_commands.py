from dataclasses import replace

from maimai_bot.community_commands import legacy_ra, register_community
from maimai_bot.domain import MessageContext
from maimai_bot.router import CommandRouter
from maimai_bot.services.community import CommunityData
from maimai_bot.services.divingfish import PlayerRecords


def test_historical_b40_uses_old_coefficients_and_caps_achievement():
    for achievement, expected in ((50, 35), (97, 135), (100, 196), (100.5, 211), (101, 211)):
        assert legacy_ra({"ds": 14, "achievements": achievement}) == expected


async def test_legacy_best_uses_separate_pools_and_recomputes_rating(tmp_path):
    class Scores:
        async def player_records(self, user_id):
            def records(count, offset):
                return [
                    {"title": str(i + offset), "ds": 10, "achievements": 100.5, "ra": 0}
                    for i in range(count)
                ]

            return PlayerRecords("玩家", 0, 0, "", records(27, 0), records(18, 100))

    class Images:
        async def render_record_list(self, title, rows, *, subtitle):
            assert len(rows) == 40
            assert all("B25" in r["_list_extra"] for r in rows[:25])
            assert all("B15" in r["_list_extra"] for r in rows[25:])
            assert "6000" in subtitle
            return b"test image"

    router = CommandRouter(None)
    community = CommunityData(None, tmp_path / "community.db")
    register_community(router, community, Scores(), None, Images())
    ctx = MessageContext("a", "a", "c2c", "/maib40", "m")
    assert (await router.dispatch(ctx)).image_png
    reply = await router.dispatch(replace(ctx, content="/上传成绩 水鱼"))
    assert len(reply.quick_actions) == 1
    assert reply.quick_actions[0].label == "水鱼同步教程"
    assert reply.quick_actions[0].url in reply.followup_text


def test_trend_records_latest_snapshot_per_day_and_per_source(tmp_path):
    data = CommunityData(None, tmp_path / "community.db")
    data.snapshot("alice", 13000, 250, 265)
    data.snapshot("chuni:alice", 15.13, 14.5, 14.4)
    rows = data.snapshot("alice", 13005, 251, 266)
    assert [row[1:] for row in rows] == [(13005, 251, 266)]
    assert data.snapshot("chuni:alice", 15.13, 14.5, 14.4)[0][1:] == (15.13, 14.5, 14.4)
