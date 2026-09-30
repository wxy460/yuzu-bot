from maimai_bot.domain import QuickAction
from maimai_bot.qq_adapter import (
    _command_label,
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


def test_command_label_does_not_log_arguments() -> None:
    assert _command_label("/song Fragrance") == "song"
    assert _command_label("查歌 Fragrance") == "查歌"
