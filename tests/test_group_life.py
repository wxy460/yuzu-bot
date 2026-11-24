import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageColor

from maimai_bot import group_life
from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.features import build_router
from maimai_bot.services.academy import INK
from maimai_bot.services.b50_image import B50ImageService
from maimai_bot.services.chat import ChatUnavailable


def msg(text, *, user="alice", group="music", role="", scope="group", message_id=None):
    return MessageContext(user, group, scope, text, message_id or text, member_role=role)


@pytest.fixture
def life(tmp_path):
    chat = SimpleNamespace(enabled=True, group_reply=AsyncMock(return_value="一起去练歌吧。"))
    return group_life.GroupLife(tmp_path / "group.sqlite", chat)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=1_800_000_000.0, roll=0.0)
    monkeypatch.setattr(group_life, "time", SimpleNamespace(time=lambda: state.now))
    monkeypatch.setattr(
        group_life, "random", SimpleNamespace(random=lambda: state.roll, randrange=lambda n: n - 1)
    )
    return state


@pytest.fixture
def router(life):
    unused = object()
    return build_router(
        life.chat,
        unused,
        B50ImageService(
            None, background_path=Path(__file__).parents[1] / "assets/b50-background.png"
        ),
        unused,
        unused,
        unused,
        unused,
        group_life=life,
    )


def assert_image(reply):
    assert isinstance(reply, BotReply) and reply.image_png
    with Image.open(io.BytesIO(reply.image_png)) as image:
        assert image.format == "PNG" and image.height > 160
        assert image.getpixel((0, 0)) == ImageColor.getrgb(INK)
    return reply.text


async def test_default_disabled_role_checks_and_real_help(router, life):
    text = assert_image(await router.dispatch(msg("/群聊设置")))
    assert "偶尔接话：关闭" in text and "群聊记忆：关闭" in text
    assert await life.respond(msg("我们明天一起去练歌")) is None
    life.chat.group_reply.assert_not_awaited()
    assert not life.notes(msg("我"), personal=True)[2] and not life.recent
    for text in ("/群聊设置 开启", "/群记忆 开启", "/群记忆 清除", "/发言频次 12", "/群戳频次 50%"):
        for role in ("member", ""):
            assert "管理员" in await router.dispatch(msg(text, role=role))
    assert "群" in await router.dispatch(msg("/群聊设置 开启", scope="c2c", role="owner"))
    assert life.settings("music") == group_life.Options()

    full_help = await router.dispatch(msg("/help"))
    assert_image(full_help)
    assert full_help.quick_actions and "https://" in full_help.followup_text
    assert "/群戳频次" in assert_image(await router.dispatch(msg("/help 管理")))
    assert "/我的画像" in assert_image(await router.dispatch(msg("/help 群友与记忆")))


async def test_admin_change_bounds_toggle_and_restart(router, life):
    async def send(text, role="admin"):
        return await router.dispatch(msg(text, role=role))

    assert "群聊记忆：开启" in assert_image(await send("/群聊设置 开启", "owner"))
    assert "6 次" in assert_image(await send("/发言频次 活跃"))
    assert "42%" in assert_image(await send("/群戳频次 42％"))
    assert "管理员" in await send("/群戳频次 100", "member")
    assert "12" in await send("/发言频次 13")
    assert "100" in await send("/群戳频次 101")
    assert_image(await send("/群记忆 关闭"))
    assert (life.settings("music").talk, life.settings("music").memory) == (1, 0)
    assert "0 次" in assert_image(await send("/发言频次 0"))
    assert life.settings("music").talk == 0
    assert_image(await send("/群聊设置 开启"))
    restarted = group_life.GroupLife(life.path, life.chat)
    options = restarted.settings("music")
    assert (options.talk, options.memory, options.rate, options.poke) == (1, 1, 3, 42)
    assert restarted.settings("elsewhere") == group_life.Options()


async def test_random_sampling_cooldown_and_hourly_budget_survive_restarts(life, clock):
    life.settings("music", {"talk": 1, "rate": 2, "poke": 100})
    clock.roll = 0.8
    assert await life.respond(msg("大家聊得真热闹")) is None
    life.chat.group_reply.assert_not_awaited()
    clock.roll = 0.0
    first = msg("柚子一起去练歌吗", message_id="first")
    reply = await life.respond(first)
    assert reply and reply.endswith("一起去练歌吧。")
    assert life.chat.group_reply.await_args.kwargs == {"spontaneous": True, "nudge": True}

    restarted = group_life.GroupLife(life.path, life.chat)
    clock.now += 179
    assert await restarted.respond(msg("今天来了很多人")) is None
    clock.now += 1
    assert (
        await restarted.respond(first) is None
    )  # A replay after restart cannot spend the second slot.
    replies = await asyncio.gather(
        *(restarted.respond(msg(f"大家组队练歌 第{i}条")) for i in range(8))
    )
    assert sum(reply is not None for reply in replies) == 1
    clock.now += 181
    assert await restarted.respond(msg("还有人想拼机吗")) is None
    assert life.chat.group_reply.await_count == 2
    restarted.settings("elsewhere", {"talk": 1})
    assert await restarted.respond(msg("另一群也在练歌", group="elsewhere"))
    clock.now += 3240
    assert await restarted.respond(msg("这一小时再约一次"))
    assert life.chat.group_reply.await_count == 4


async def test_group_and_personal_notes_are_scoped_and_members_can_forget(router, life, clock):
    for group in ("music", "school"):
        life.settings(group, {"memory": 1})
    personal = "我最近练习单手纵连"
    shared = "大家周六一起去练歌"
    other = "我今天听了新歌"
    school = "我明天准备参加学校合唱"
    secret = "我今天的验证码是 123456"
    for ctx in [
        msg(personal),
        msg(shared),
        msg(other, user="bob"),
        msg(school, group="school"),
        msg(secret),
    ]:
        assert await life.respond(ctx) is None
    life.chat.group_reply.assert_not_awaited()
    assert shared in assert_image(await router.dispatch(msg("/群记忆")))
    visible = assert_image(await router.dispatch(msg("/我的画像")))
    assert (
        personal in visible
        and other not in visible
        and school not in visible
        and secret not in visible
    )
    assert personal not in assert_image(await router.dispatch(msg("/群记忆")))
    assert other in assert_image(await router.dispatch(msg("/我的画像", user="bob")))
    assert school in assert_image(await router.dispatch(msg("/我的画像", group="school")))

    # A fresh instance reconstructs the current member's notes, never another room's or another member's personal notes.
    restarted = group_life.GroupLife(life.path, life.chat)
    await restarted.respond(msg("想起之前的约定了吗"), explicit=True)
    transcript = json.loads(life.chat.group_reply.await_args.args[0])
    stored = [body for _, _, body in transcript["本群便签"]]
    assert set(stored) == {shared, personal}
    assert life.chat.group_reply.await_args.kwargs == {"spontaneous": False, "nudge": False}
    assert "alice" not in json.dumps(transcript) and "bob" not in json.dumps(transcript)

    assert "清除" in await router.dispatch(msg("/我的画像 清除"))
    assert (
        not restarted.notes(msg("自己"), personal=True)[2]
        and not restarted.notes(msg("群"), personal=False)[2]
    )
    assert restarted.notes(msg("其他人", user="bob"), personal=True)[2]
    await router.dispatch(msg("/我的画像 退出"))
    assert restarted.notes(msg("自己"), personal=True)[1]
    assert await restarted.respond(msg("我今天准备偷练"), explicit=True) is None
    assert not restarted.notes(msg("自己"), personal=True)[2]
    assert restarted.notes(msg("自己", group="school"), personal=True)[2]
    assert "恢复" in await router.dispatch(msg("/我的画像 恢复"))
    await life.respond(msg("我今天练习合唱"))
    assert "我今天练习合唱" in assert_image(await router.dispatch(msg("/我的画像")))
    assert "管理员" in await router.dispatch(msg("/群记忆 清除"))
    assert "已清空" in await router.dispatch(msg("/群记忆 清除", role="owner"))
    assert not restarted.notes(msg("其他人", user="bob"), personal=True)[2]
    assert not restarted.topics(msg("群"))
    assert school in assert_image(await router.dispatch(msg("/我的画像", group="school")))
    clock.now += 31 * 86400
    assert not restarted.notes(msg("自己", group="school"), personal=True)[2]


@pytest.mark.parametrize("result", ["", "[沉默]", ChatUnavailable("模拟上游不可用")])
async def test_spontaneous_model_silence_and_failure_are_quiet(life, clock, result):
    life.settings("music", {"talk": 1})
    if isinstance(result, Exception):
        life.chat.group_reply.side_effect = result
    else:
        life.chat.group_reply.return_value = result
    assert await life.respond(msg("这次聊聊新歌")) is None
    assert await life.respond(msg("第二条消息")) is None
    life.chat.group_reply.assert_awaited_once()


@pytest.mark.parametrize("withdraw", ["/群聊设置 关闭", "/我的画像 退出", "/群记忆 清除"])
async def test_inflight_reply_is_discarded_after_withdrawal(life, clock, router, withdraw):
    life.settings("music", {"talk": 1, "memory": 1})
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_model(*args, **kwargs):
        entered.set()
        await release.wait()
        return "已经失效的回复"

    life.chat.group_reply.side_effect = waiting_model
    task = asyncio.create_task(life.respond(msg("大家今天一起玩")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        await router.dispatch(msg(withdraw, role="admin"))
    finally:
        release.set()
    assert await asyncio.wait_for(task, timeout=3) is None


async def test_update_log_requires_configured_bot_owner_not_just_group_admin(router, monkeypatch):
    monkeypatch.setenv("BOT_OWNER_OPENIDS", "bot-owner")
    assert "仅供 Bot 主人" in await router.dispatch(msg("/更新日志", role="owner"))
    assert_image(await router.dispatch(msg("/更新日志", user="bot-owner", scope="c2c")))
