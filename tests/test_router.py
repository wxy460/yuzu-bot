from maimai_bot.domain import MessageContext
from maimai_bot.router import CommandRouter


def context(content: str) -> MessageContext:
    return MessageContext("user", "chat", "c2c", content, "message")


async def test_dispatches_registered_command() -> None:
    async def fallback(message: MessageContext) -> str:
        return message.content

    router = CommandRouter(fallback)

    @router.command("hello", "test")
    async def hello(request):
        return f"hello {request.argument}"

    assert await router.dispatch(context("/hello world")) == "hello world"


async def test_plain_text_uses_fallback() -> None:
    async def fallback(message: MessageContext) -> str:
        return f"chat:{message.content}"

    router = CommandRouter(fallback)
    assert await router.dispatch(context("你好")) == "chat:你好"


async def test_exact_bare_command_supports_qq_panel_clicks() -> None:
    async def fallback(message: MessageContext) -> str:
        return f"chat:{message.content}"

    router = CommandRouter(fallback)

    @router.command("b50", "test")
    async def b50(_):
        return "panel:b50"

    assert await router.dispatch(context("b50")) == "panel:b50"
    assert await router.dispatch(context("b50 please")) == "panel:b50"


async def test_bare_alias_with_argument_and_pattern_route() -> None:
    async def fallback(message: MessageContext) -> str:
        return f"chat:{message.content}"

    router = CommandRouter(fallback)

    @router.command("song", "test", aliases=("查歌",), category="曲目", usage="查歌 潘")
    async def song(request):
        return f"song:{request.argument}"

    router.pattern(r"^(.+)是什么歌$", "song", lambda match: match.group(1))
    assert await router.dispatch(context("查歌 潘")) == "song:潘"
    assert await router.dispatch(context("11451是什么歌")) == "song:11451"
    assert "【曲目】" in router.help_text("曲目")


async def test_unknown_command_has_hint() -> None:
    async def fallback(_: MessageContext) -> str:
        return "unused"

    router = CommandRouter(fallback)
    assert "/help" in await router.dispatch(context("/missing"))


def test_recognizes_commands_for_qq_full_group_messages() -> None:
    async def fallback(message: MessageContext) -> str:
        return message.content

    router = CommandRouter(fallback)

    @router.command("song", "test", aliases=("查歌",))
    async def song(request):
        return request.argument

    router.pattern(r"^(.+)是什么歌$", "song", lambda match: match.group(1))

    assert router.recognizes_command("/song 潘")
    assert router.recognizes_command("查歌 潘")
    assert router.recognizes_command("11451是什么歌")
    assert router.recognizes_command("/missing")
    assert not router.recognizes_command("大家晚上好")
    assert not router.recognizes_command("")
