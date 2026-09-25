from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.features import HELP_SITE_URL, build_router


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
    assert images.command_count == len(router.commands)


async def test_categorized_help_remains_text_only() -> None:
    images = HelpImageStub()
    unused = object()
    router = build_router(unused, unused, images, unused, unused, unused, unused)  # type: ignore[arg-type]

    reply = await router.dispatch(_context("/help 成绩"))

    assert isinstance(reply, str)
    assert "【成绩】" in reply
    assert images.command_count == 0
