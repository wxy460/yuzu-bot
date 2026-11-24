import io
from pathlib import Path

from PIL import Image, ImageColor

from maimai_bot.domain import BotReply, MessageContext, QuickAction
from maimai_bot.features import build_router
from maimai_bot.help_catalog import group_commands, select_group
from maimai_bot.services.academy import INK
from maimai_bot.services.b50_image import B50ImageService
from maimai_bot.services.result_card import academy_card, result_card


async def test_campus_results_and_maimai_sheets_keep_distinct_themes_and_copyable_actions():
    action = QuickAction("查看礼物", "/礼物图鉴")
    url = "https://example.com/guide"
    rows = [("带有很长名字的字段用来检查自动分行", "适配窄屏\n第二行")]
    campus = await academy_card(
        "柚子的背包", rows, metric=("礼物总数", "12 份"), followup=url, actions=(action,)
    )
    maimai = await result_card("舞萌成绩", rows)
    with (
        Image.open(io.BytesIO(campus.image_png)) as card,
        Image.open(io.BytesIO(maimai.image_png)) as sheet,
    ):
        assert card.getpixel((0, 0)) == ImageColor.getrgb(INK)
        assert sheet.getpixel((0, 0)) != card.getpixel((0, 0))
        assert card.width == 880 and card.height > 400
    assert "12 份" in campus.text and "第二行" in campus.text
    assert campus.followup_text == url and campus.quick_actions == (action,)


async def test_top_level_and_legacy_groups_are_both_reachable():
    unused = object()
    images = B50ImageService(None, background_path=Path("assets/b50-background.png"))
    router = build_router(
        unused, unused, images, unused, unused, unused, unused, community=unused, cross_games=unused
    )
    groups = group_commands(router.commands)
    assert sum(map(len, groups.values())) == len(router.commands)
    assert {cmd.name for cmd in groups["礼物工坊"]} >= {"背包", "送礼", "礼物图鉴"}
    assert {cmd.name for cmd in groups["占卜与点歌"]} >= {"jrys"}
    assert select_group(router.commands, "maimaiDX")[0] == "舞萌"
    assert any(c.name == "ap50" for c in select_group(router.commands, "成绩")[1])
    for content in ("/帮助maimaiDX", "帮助maiMaiDX", "/help 礼物工坊", "/help 成绩"):
        reply = await router.dispatch(MessageContext("u", "g", "group", content, "m"))
        assert isinstance(reply, BotReply) and reply.image_png
        if "dx" in content.casefold():
            assert "b50" in reply.text and "minfo" in reply.text
    text = await router.dispatch(MessageContext("u", "g", "group", "/help 文字 成绩", "m"))
    assert "【成绩】" in text
