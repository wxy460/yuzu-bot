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
