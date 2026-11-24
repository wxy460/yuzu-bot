from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from maimai_bot import social, social_craft
from maimai_bot.community_commands import register_community
from maimai_bot.domain import MessageContext
from maimai_bot.router import CommandRouter
from maimai_bot.services.community import CommunityData


def stock(campus, player, quantities):
    with campus._db(write=True) as db:
        for day, (gift, count) in enumerate(quantities.items(), 1):
            db.execute("INSERT INTO campus_checkins VALUES(?,?,?,?,?,?)",
                       (player, f"2026-08-{day:02d}", gift, count, 1, day))


def test_crafting_and_cooldown_are_idempotent_across_workers_and_restarts(tmp_path, monkeypatch):
    path = tmp_path / "social.db"
    c = social.Campus(path)
    stock(c, "p", {"1": 2, "2": 2, "3": 1, "4": 1})
    now = 1800000000
    clock = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(social, "time", clock)
    with ThreadPoolExecutor(max_workers=6) as workers:
        orders = list(workers.map(lambda _: list(map(tuple, social_craft.queue(
            social.Campus(path), "p", "order", None, now))), range(12)))
        assert all(x == [("6", 2, now + 1200), ("7", 1, now + 2100)] for x in orders)
        assert not c.bag("p")
        assert not social_craft.queue(c, "p", "second", None, now)
        assert sum(first == "好感 +1" for first, _ in workers.map(
            lambda i: social_craft.pet(social.Campus(path), "p", f"m{i}", now), range(8))) == 1
    assert c.profile("p")[-1] == 5
    assert social_craft.state(c, "p", now + 1)[1] == social_craft.PET_COOLDOWN - 1
    now += 600
    assert c.bag("p") == {"6": 1}
    assert c.give("p", "send", "6", 1)[1]
    now += 1500
    assert social.Campus(path).bag("p") == {"6": 1, "7": 1}
    assert not social_craft.state(c, "p", now)[0]
    assert social_craft.pet(c, "p", "early", now)[0] == "还在休息"
    now += social_craft.PET_COOLDOWN
    assert social_craft.pet(c, "p", "next", now)[0] == "好感 +1"
    assert not c.bag("other") and c.profile("other") == (0, 0, 0)


async def test_router_gift_list_name_number_queue_and_status(tmp_path, monkeypatch):
    database = tmp_path / "social.sqlite"
    now = 1800000000
    clock = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(social, "time", clock)
    monkeypatch.setattr(social_craft, "time", clock)
    router = CommandRouter(None)
    register_community(router, CommunityData(None, database), None, None, None)

    def ctx(text):
        return MessageContext("u", "room", "group", text, text)

    async def send(text):
        return await router.dispatch(ctx(text))

    campus, player = social.Campus(database), social.player_key(ctx(""))
    stock(campus, player, {"1": 4, "2": 2, "3": 1, "4": 1, "5": 1})
    assert "放学糖果礼盒" in (await send("/送礼")).text
    assert "柚子糖 × 2" in (await send("/送礼 1*2")).text
    assert "好感 +1" in (await send("摸头")).text
    assert "还需 180 分钟" in (await send("/状态")).text
    assert "材料" in (await send("/合成")).text
    assert "配方" in await send("/合成 汽水+汽水")
    assert "星糖小袋 × 1" in (await send("/合成 星星贴纸＋柚子糖")).text
    auto = await send("/一键合成")
    assert "星糖小袋 × 1" in auto.text and "跳跳汽水 × 1" in auto.text
    assert "已完成 0/1" in (await send("/合成状态")).text
    assert "材料" in await send("/合成 5+6")
    now += 600
    assert "放学糖果礼盒 × 1" in (await send("/合成 5+6")).text
    now += 3300
    assert campus.bag(player) == {"6": 1, "7": 1, "8": 1}
    assert "队列已空" in (await send("/合成状态")).text
