import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import httpx
import qqbot_agent_sdk as sdk

from maimai_bot.domain import QuickAction
from maimai_bot.qq_adapter import (
    QQOfficialAdapter,
    _qq_avatar_url,
    _qq_openid_avatar_url,
    _send_text_reply,
)
from maimai_bot.qq_shortcuts import (
    COSMETIC_QUICK_ACTIONS,
    HELP_QUICK_ACTIONS,
    QQShortcutKeyboard,
)


def test_reads_qq_avatar_from_author() -> None:
    assert _qq_avatar_url({"author": {"avatar": "https://q.qlogo.cn/example.png"}}) == (
        "https://q.qlogo.cn/example.png"
    )


def test_normalizes_protocol_relative_qq_avatar() -> None:
    assert _qq_avatar_url({"author": {"avatar": "//q.qlogo.cn/example.png"}}) == (
        "https://q.qlogo.cn/example.png"
    )


def test_rejects_non_https_avatar() -> None:
    assert _qq_avatar_url({"author": {"avatar": "http://example.test/avatar.png"}}) is None


def test_builds_avatar_url_from_official_bot_openid() -> None:
    assert _qq_openid_avatar_url("12345", "OPEN ID") == (
        "https://thirdqq.qlogo.cn/qqapp/12345/OPEN%20ID/640"
    )


def test_help_shortcuts_use_official_command_buttons() -> None:
    keyboard = QQShortcutKeyboard(HELP_QUICK_ACTIONS).to_dict()
    rows = keyboard["content"]["rows"]

    assert len(rows) == 2
    assert all(len(row["buttons"]) == 2 for row in rows)
    first = rows[0]["buttons"][0]
    assert first["action"] == {
        "type": 2,
        "permission": {"type": 2},
        "data": "/b50",
        "reply": False,
        "enter": True,
        "unsupport_tips": "请发送 /b50",
    }


def test_argument_shortcut_fills_input_without_auto_send() -> None:
    keyboard = QQShortcutKeyboard((QuickAction("查歌", "/song ", False),)).to_dict()
    action = keyboard["content"]["rows"][0]["buttons"][0]["action"]

    assert action["data"] == "/song "
    assert action["enter"] is False


def test_url_shortcut_uses_official_jump_action() -> None:
    keyboard = QQShortcutKeyboard(
        (QuickAction("谱面预览", url="https://v.awmc.cc/preview?song=417"),)
    ).to_dict()
    action = keyboard["content"]["rows"][0]["buttons"][0]["action"]

    assert action == {
        "type": 0,
        "permission": {"type": 2},
        "data": "https://v.awmc.cc/preview?song=417",
        "unsupport_tips": "请打开 https://v.awmc.cc/preview?song=417",
    }


def test_cosmetic_shortcuts_cover_search_select_and_reset() -> None:
    commands = [action.command for action in COSMETIC_QUICK_ACTIONS]

    assert commands == [
        "/cosmetic 搜索 头像 ",
        "/cosmetic 搜索 姓名框 ",
        "/cosmetic 头像 ",
        "/cosmetic 姓名框 ",
        "/cosmetic 头像 QQ",
        "/cosmetic 姓名框 自动",
    ]


async def test_shortcut_reply_uses_markdown_message() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.build_args = None
            self.sent = None

        def build_text_body(self, text, **kwargs):
            self.build_args = (text, kwargs)
            return object()

        async def post_group_message(self, chat_id, message, *, keyboard):
            self.sent = (chat_id, message, keyboard)

    api = FakeApi()
    await _send_text_reply(api, "group", "group-id", "message-id", "帮助", HELP_QUICK_ACTIONS)

    assert api.build_args == (
        "帮助",
        {"reply_to": "message-id", "markdown": True},
    )
    assert api.sent is not None


async def test_official_gateway_routes_group_text_roles_mentions_and_duplicate_precedence(
    monkeypatch, caplog
):
    full, at = "GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"
    sent = []
    normal, ambient, json_attachments = [], [], []
    parked, release = asyncio.Event(), asyncio.Event()

    async def normal_handler(ctx):
        normal.append((ctx.message_id, ctx.content, ctx.member_role))
        if ctx.json_urls:
            json_attachments.append(ctx.json_urls)
        return "direct"

    async def ambient_handler(ctx):
        ambient.append((ctx.message_id, ctx.content, ctx.member_role))
        if ctx.message_id == "throws":
            raise ValueError(ctx.content)
        if ctx.message_id == "silent":
            return None
        if ctx.message_id == "race":
            parked.set()
            await release.wait()
        return "ambient"

    api = Mock()
    api.ensure_token = AsyncMock()
    api.get_gateway_url = AsyncMock(return_value="wss://no-network.test")

    async def send(scope, group, text, *, reply_to, markdown):
        sent.append((reply_to, text))

    api.send_text = send
    monkeypatch.setattr(sdk, "QQApiClient", lambda **_: api)
    waiting = []

    def start(socket, url, loop):
        nonlocal gateway
        gateway = socket
        ready.set()

    # Feed actual SDK frame dispatch/parser code, collecting its main-loop callbacks.
    monkeypatch.setattr(sdk.QQWebSocket, "start", start)
    monkeypatch.setattr(sdk.QQWebSocket, "async_stop", AsyncMock())
    monkeypatch.setattr(
        sdk.QQWebSocket, "_dispatch_to_main", lambda self, coro: waiting.append(coro)
    )
    monkeypatch.setattr(sdk, "MediaUploader", lambda **_: None)
    ready, stop = asyncio.Event(), asyncio.Event()
    gateway = None
    caplog.set_level(logging.INFO)
    private_text = "聊天内容不应留在日志中"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: None)) as http:
        bot = QQOfficialAdapter(
            app_id="42",
            app_secret="unused",
            http=http,
            handler=normal_handler,
            ambient_handler=ambient_handler,
            unmentioned_context_filter=lambda c: (
                c.content == "/b50"
                or bool(c.image_urls)
                or (c.message_id == "waiting-json" and bool(c.json_urls))
            ),
        )
        runner = asyncio.create_task(bot.run(stop))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            gateway._handle_dispatch(
                "READY", {"session_id": "session", "user": {"id": "bot-openid", "bot": True}}
            )

            def frame(typ, mid, text, **fields):
                gateway._handle_dispatch(
                    typ,
                    {
                        "id": mid,
                        "group_openid": "room",
                        "content": text,
                        "author": {"id": "user", "member_openid": "user", "member_role": "member"},
                        **fields,
                    },
                )

            async def flush():
                while waiting:
                    await waiting.pop(0)

            frame(full, "cmd", "/b50", author={"member_openid": "alice", "member_role": "admin"})
            frame(at, "cmd", "<@!42>/b50")
            frame(
                full,
                "self-structured",
                "<@B_OID> " + private_text,
                mentions=[{"member_openid": "B_OID", "is_you": True, "scope": "single"}],
            )
            frame(at, "self-structured", private_text)
            frame(full, "self-ready", "<@!bot-openid> /b50")
            frame(full, "self-app", "<@42>", author={"member_openid": "a", "member_role": "owner"})
            frame(
                full,
                "other",
                "<@other> " + private_text,
                mentions=[{"member_openid": "other", "is_you": False}],
            )
            frame(full, "display", "@小明 " + private_text)
            frame(full, "everyone", "<@all> " + private_text, mentions=[{"scope": "all"}])
            frame(full, "bot-msg", private_text, author={"member_openid": "some-bot", "bot": True})
            frame(full, "self-msg", private_text, author={"member_openid": "bot-openid"})
            for key, fields in [
                ("reply", {"message_type": 103}),
                ("elements", {"msg_elements": [{"content": private_text}]}),
                ("scene", {"message_scene": {"source": "default", "ext": ["ref_msg_idx=reply"]}}),
                ("card", {"ark_data": {"ark_type": "tuwen"}}),
                (
                    "file",
                    {
                        "attachments": [
                            {
                                "content_type": "file",
                                "filename": "test",
                                "url": "https://local.invalid/file",
                            }
                        ]
                    },
                ),
            ]:
                frame(full, key, private_text, **fields)
            frame(
                full,
                "waiting-image",
                "",
                attachments=[{"content_type": "image/png", "url": "https://local.invalid/p.png"}],
            )
            frame(
                full,
                "plain",
                private_text,
                author={"member_openid": "a", "member_role": "not-valid"},
                member={"member_role": "owner"},
                message_scene={"source": "default", "ext": ["msg_idx=xx"]},
            )
            frame(full, "plain", private_text)
            frame(at, "plain", private_text)
            file = {
                "content_type": "file",
                "filename": "scores.JSON",
                "url": "//multimedia.nt.qq.com.cn/private",
            }
            frame(
                full,
                "waiting-json",
                "",
                attachments=[
                    file,
                    file,
                    {"content_type": "application/json; charset=utf-8", "url": "qq.com/2"},
                ],
            )
            frame(full, "unrequested-json", "", attachments=[file])
            frame(full, "quote-json", "", msg_elements=[{"attachments": [file]}])
            frame(full, "throws", private_text)
            frame(full, "silent", private_text)
            frame(at, "silent", private_text)
            frame(at, "silent", private_text)
            frame(full, "silent", private_text)
            await flush()
            frame(full, "race", private_text)
            pending = asyncio.create_task(waiting.pop())
            await asyncio.wait_for(parked.wait(), 1)
            frame(at, "race", private_text)
            await flush()
            release.set()
            await pending
        finally:
            release.set()
            stop.set()
            await runner
    assert normal == [
        ("cmd", "/b50", "admin"),
        ("self-structured", private_text, "member"),
        ("self-ready", "/b50", "member"),
        ("self-app", "/help", "owner"),
        ("waiting-image", "", "member"),
        ("plain", private_text, "member"),
        ("waiting-json", "", "member"),
        ("silent", private_text, "member"),
        ("race", private_text, "member"),
    ]
    assert [m for m, *_ in ambient] == ["plain", "throws", "silent", "race"]
    assert json_attachments == [("https://multimedia.nt.qq.com.cn/private", "https://qq.com/2")]
    assert ambient[0][2] == ""
    assert sent.count(("race", "direct")) == 1 and ("race", "ambient") not in sent
    assert sent.count(("plain", "ambient")) == 1 and ("throws", "ambient") not in sent
    assert ("plain", "direct") not in sent
    assert len(sent) == len(normal) and private_text not in caplog.text
