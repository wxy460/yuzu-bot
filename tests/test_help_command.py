from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.features import COSMETIC_SITE_URL, HELP_SITE_URL, _cosmetic_help, build_router


class HelpImageStub:
    def __init__(self) -> None:
        self.command_count = 0

    def render_help(self, commands: tuple[object, ...]) -> bytes:
        self.command_count = len(commands)
        return b"preview-png"


def _context(content: str) -> MessageContext:
    return MessageContext("user", "group", "group", content, "message")


async def test_help_returns_preview_image_and_site_link() -> None:
    images = HelpImageStub()
    unused = object()
    router = build_router(unused, unused, images, unused, unused, unused, unused)  # type: ignore[arg-type]

    reply = await router.dispatch(_context("/help"))

    assert isinstance(reply, BotReply)
    assert reply.image_png == b"preview-png"
    assert HELP_SITE_URL in reply.followup_text
    assert "网络代理" in reply.followup_text
    assert [action.command for action in reply.quick_actions] == [
        "/b50",
        "/daily",
        "/song ",
        "/cosmetic",
    ]
    assert images.command_count == len(router.commands)


async def test_categorized_help_includes_copyable_examples_and_explicit_text_mode() -> None:
    images = HelpImageStub()
    unused = object()
    router = build_router(unused, unused, images, unused, unused, unused, unused)  # type: ignore[arg-type]

    reply = await router.dispatch(_context("/help 成绩"))

    assert isinstance(reply, BotReply) and reply.image_png
    assert "/ap50" in reply.followup_text
    text = await router.dispatch(_context("/help 文字 成绩"))
    assert isinstance(text, str)
    assert "【成绩】" in text
    assert images.command_count == 0


def test_cosmetic_help_links_to_searchable_catalog() -> None:
    text = _cosmetic_help(None, None)

    assert COSMETIC_SITE_URL in text
    assert "点击复制 ID" in text


def _complete_router(images=None):
    from maimai_bot.services.lxns_scores import ScoreSources

    unused = object()
    return build_router(
        unused,
        ScoreSources(unused, unused),
        images or HelpImageStub(),
        unused,
        unused,
        unused,
        unused,
        recognition=unused,
        community=unused,
        cross_games=unused,
        group_life=unused,
    )


async def test_section_topic_and_individual_help_cover_recent_commands():
    router = _complete_router()
    for query, required in (
        ("舞萌/成绩", ["/pc50", "/scoretable", "/b50"]),
        ("maimaiDX 牌子", ["镜将进度", "彩将进度", "/plate-conditions"]),
        ("礼物工坊 · 合成", ["/合成", "/一键合成", "/合成状态"]),
        ("管理 群设置", ["/群聊设置", "/发言频次", "/群戳频次"]),
    ):
        result = await router.dispatch(_context("/help 文字 " + query))
        assert isinstance(result, str) and all(term in result for term in required)
    index = await router.dispatch(_context("/help 文字"))
    assert isinstance(index, str) and "/help 舞萌" in index and "群设置" in index
    reply = await router.dispatch(_context("/help 游玩排行50"))
    assert isinstance(reply, BotReply) and reply.image_png.startswith(b"\x89PNG")
    assert reply.followup_text.splitlines() == ["/pc50", "/pc50 导入", "/pc50 取消", "/pc50 清除"]
    assert "3 分钟" in reply.text and "快照" in reply.text
    plate = await router.dispatch(_context("/help 文字 plate"))
    assert isinstance(plate, str) and "彩将进度" in plate and "霸进度" in plate


def test_registered_commands_have_sendable_examples_and_discoverable_topics():
    from maimai_bot.help_catalog import (
        command_examples,
        command_usage,
        group_commands,
        group_topics,
        select_group,
    )

    router = _complete_router()
    groups = group_commands(router.commands)
    for section, commands in groups.items():
        for topic, children in group_topics(commands).items():
            for item in children:
                assert item in select_group(router.commands, f"{section} {topic}")[1]
        for command in commands:
            assert command_usage(command).startswith("/" + command.name)
            for example, meaning in command_examples(command):
                assert meaning and router.recognizes_command(example.removeprefix("/"))
                assert "[" not in example and " 或 " not in example
