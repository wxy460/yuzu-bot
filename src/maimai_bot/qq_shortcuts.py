from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import QuickAction

MAX_ROWS = 5
MAX_BUTTONS_PER_ROW = 2


@dataclass(frozen=True, slots=True)
class QQShortcutKeyboard:
    """Official QQ command-button keyboard.

    ``action.type=0`` opens an HTTPS URL, while
    ``action.type=2`` asks the QQ client to insert ``@bot <command>``. This
    keeps every click on the normal, audited command-message path instead of
    adding a private-protocol or callback-only command channel.
    """

    actions: tuple[QuickAction, ...]

    def to_dict(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        visible = self.actions[: MAX_ROWS * MAX_BUTTONS_PER_ROW]
        for offset in range(0, len(visible), MAX_BUTTONS_PER_ROW):
            buttons = []
            for index, action in enumerate(
                visible[offset : offset + MAX_BUTTONS_PER_ROW], start=offset
            ):
                if action.url:
                    action_data = {
                        "type": 0,
                        "permission": {"type": 2},
                        "data": action.url,
                        "unsupport_tips": f"请打开 {action.url}",
                    }
                else:
                    action_data = {
                        "type": 2,
                        "permission": {"type": 2},
                        "data": action.command,
                        "reply": False,
                        "enter": action.auto_send,
                        "unsupport_tips": f"请发送 {action.command.strip()}",
                    }
                buttons.append(
                    {
                        "id": f"shortcut-{index + 1}",
                        "render_data": {
                            "label": action.label,
                            "visited_label": action.label,
                            "style": 1 if index < 4 else 0,
                        },
                        "action": action_data,
                    }
                )
            rows.append({"buttons": buttons})
        return {"content": {"rows": rows}}


HELP_QUICK_ACTIONS = (
    QuickAction("📊 B50", "/b50"),
    QuickAction("🎯 每日推歌", "/daily"),
    QuickAction("🔎 查歌", "/song ", auto_send=False),
    QuickAction("🎨 外观设置", "/cosmetic"),
)

COSMETIC_QUICK_ACTIONS = (
    QuickAction("🔎 搜索头像", "/cosmetic 搜索 头像 ", auto_send=False),
    QuickAction("🔎 搜索姓名框", "/cosmetic 搜索 姓名框 ", auto_send=False),
    QuickAction("🖼️ 选择头像", "/cosmetic 头像 ", auto_send=False),
    QuickAction("🏷️ 选择姓名框", "/cosmetic 姓名框 ", auto_send=False),
    QuickAction("↩️ QQ 头像", "/cosmetic 头像 QQ"),
    QuickAction("↩️ 自动姓名框", "/cosmetic 姓名框 自动"),
)
