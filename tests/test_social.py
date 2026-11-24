from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maimai_bot import social
from maimai_bot.community_commands import register_community
from maimai_bot.domain import MessageContext
from maimai_bot.router import CommandRouter
from maimai_bot.services.community import CommunityData
from maimai_bot.services.song_catalog import SongCatalogError


def test_checkin_and_gift_are_atomic_across_workers_and_restarts(tmp_path):
    path = tmp_path / "community.sqlite"
    player = "p"
    day = date(2026, 8, 1)
    with ThreadPoolExecutor(max_workers=6) as workers:
        checkins = list(workers.map(lambda _: social.Campus(path).checkin(player, day), range(16)))
    assert sum(new for _, new in checkins) == 1
    gift = checkins[0][0]["gift"]
    campus = social.Campus(path)
    assert campus.bag(player) == {gift: 1}
    with pytest.raises(ValueError):
        campus.give(player, "overdraw", gift, 2)
    assert campus.profile(player) == (1, 0, 1)
    with ThreadPoolExecutor(max_workers=6) as workers:
        gifts = list(
            workers.map(
                lambda _: social.Campus(path).give(player, "same-message", gift, 1), range(16)
            )
        )
    assert sum(new for _, new in gifts) == 1
    assert all(row["favor"] == social.GIFTS[gift].favor for row, _ in gifts)
    assert not social.Campus(path).bag(player)
    assert social.Campus(path).profile(player) == (1, 1, 1 + social.GIFTS[gift].favor)

    other = "another-player"
    item, _ = campus.checkin(other, day)

    def give(number):
        try:
            return social.Campus(path).give(other, str(number), item["gift"], 1)[1]
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=6) as workers:
        assert sum(workers.map(give, range(16))) == 1
    assert not campus.bag(other)


def test_streak_bonus_uses_consecutive_days(tmp_path):
    campus = social.Campus(tmp_path / "community.sqlite")
    start = date(2026, 8, 1)
    for n in range(14):
        result, new = campus.checkin("a", start + timedelta(days=n))
        assert result["streak"] == n + 1 and new
        assert result["quantity"] == (2 if (n + 1) % 7 == 0 else 1)
    assert sum(campus.bag("a").values()) == 16
    assert campus.checkin("a", start + timedelta(days=15))[0]["streak"] == 1


async def test_registered_social_commands_images_isolation_midnight_and_fortune(
    tmp_path, monkeypatch
):
    instant = datetime(2026, 11, 25, 15, 59, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(zone):
            return instant.astimezone(zone)

    monkeypatch.setattr(social, "datetime", Clock)
    path = tmp_path / "community.sqlite"
    catalog = AsyncMock()
    catalog.songs.side_effect = [
        SongCatalogError("down"),
        (SimpleNamespace(id=42, title="Candy Day", artist="Ya"),),
    ]

    def start():
        router = CommandRouter(None)
        register_community(router, CommunityData(None, path), None, catalog, None)
        return router

    router = start()
    group = MessageContext("alice", "room1", "group", "", "message")

    async def send(command, who=group, message="m"):
        return await router.dispatch(replace(who, content=command, message_id=message))

    first = await send("/签到")
    assert first.image_png.startswith(b"\x89PNG") and "11 月 25 日" in first.text
    assert "已经盖过章" in (await send("/checkin")).text
    assert "连续签到：1 天" in first.text
    bag = await send("/我的背包")
    assert bag.image_png and "1 份" in bag.text
    for other in (
        replace(group, user_id="bob"),
        replace(group, chat_id="room2"),
        replace(group, chat_scope="c2c"),
    ):
        assert "空背包" in (await send("/背包", other)).text
        assert "好感度：0" in (await send("/好感", other)).text
    fortune_without_song = await send("/今日运势")
    assert "暂时读不到" in fortune_without_song.text
    fortune = await send("/运势")
    assert "Candy Day" in fortune.text and fortune.quick_actions[0].command == "/song 42"
    assert (
        next(line for line in fortune_without_song.text.splitlines() if line.startswith("2026-"))
        in fortune.text
    )
    router = start()
    assert (await send("/jrys")).text == fortune.text and catalog.songs.await_count == 2
    inventory = social.Campus(path).bag(social.player_key(group))
    gift = next(iter(inventory))
    name = social.GIFTS[gift].name
    assert (await send("/送礼")).quick_actions[0].command == "/送礼 " + gift
    assert "0 份" in await send("/送礼 " + next(idx for idx in social.GIFTS if idx != gift))
    assert "用法" in await send("/送礼 柚子糖 -2")
    given = await send("/送礼 " + name, message="abc")
    assert given.image_png and name in given.text
    assert "收好啦" in (await send("/赠礼 " + gift, message="abc")).text
    assert "空背包" in (await send("/背包")).text
    assert f"好感度：{1 + social.GIFTS[gift].favor}" in (await send("/好感度")).text
    instant += timedelta(minutes=1)
    assert "连续签到：2 天" in (await send("/签到")).text
    assert (await send("/礼物图鉴")).image_png
    assert "北京时间零点" in (await send("/校园互动")).text
