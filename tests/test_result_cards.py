import io
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from PIL import Image

from maimai_bot.domain import BotReply, MessageContext, QuickAction
from maimai_bot.features import build_router
from maimai_bot.services.b50_image import B50ImageService
from maimai_bot.services.divingfish import Best50
from maimai_bot.services.result_card import result_card
from maimai_bot.services.song_catalog import Chart, Song


async def test_results_render_from_real_command_routes():
    chart = Chart(
        834, "PANDORA PARADOXXX", "削除", "maimai", 19000, "standard", 3, "14+", 14.9,
        "譜面-100号", tap=560, hold=60, slide=80, break_count=20,
    )
    song = Song(834, chart.title, chart.artist, chart.genre, 150, chart.version, (chart,))
    catalog = Mock()
    catalog.resolve = AsyncMock(return_value=song)
    catalog.chart = AsyncMock(return_value=chart)
    catalog.random_chart = AsyncMock(return_value=chart)
    catalog.aliases_for = AsyncMock(return_value=(song, ["潘", "潘多拉"]))
    catalog.version_name.return_value = "FiNALE"
    scores = Mock()
    scores.best50 = AsyncMock(return_value=Best50("柚子", 15123, 0, "", [], []))
    cosmetics = Mock()
    cosmetics.describe = AsyncMock(return_value=(None, None))
    images = B50ImageService(None, background_path=Path("assets/b50-background.png"))
    images._cover_for = AsyncMock(return_value=None)
    router = build_router(Mock(), scores, images, cosmetics, catalog, Mock(), Mock())
    cases = {
        "info 潘": song.title, "紫潘": song.title, "随个14+": song.title,
        "潘有什么别名": "潘多拉", "ra 14.7 100.5": "330", "achieve 200 195 5 0 0": "99.5000%",
        "达成率反推 100.5": "0.5000%", "分数线 紫潘 100.5": "TAP GREAT",
        "牌子条件": "霸者", "b50分析": "15123", "进榜线 14.7": "目标单谱 RA",
        "/cosmetic": "QQ 头像", "/about": "默认水鱼查分器。落雪查分器启用时，可用 /lxns 独立授权、/source 切换。",
    }
    for command, expected in cases.items():
        reply = await router.dispatch(MessageContext("u", "g", "group", command, "1"))
        assert isinstance(reply, BotReply), command
        assert reply.image_png and expected in reply.text, command
        with Image.open(io.BytesIO(reply.image_png)) as image:
            image.verify()
            assert image.format == "PNG"
        if command == "/cosmetic":
            assert reply.quick_actions and "https://" in reply.followup_text
        if command == "潘有什么别名":
            assert "/song 834" in reply.followup_text


async def test_oversize_card_preserves_content_links_and_actions_as_text():
    command = QuickAction("查歌", "/song ", False)
    text = "谱面" * 5000
    link = "https://example.test/chart"
    reply = await result_card("谱面", [("说明", text)], followup=link, actions=(command,))
    assert not reply.image_png
    assert text in reply.text and link in reply.text
    assert reply.quick_actions == (command,)
