from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol
from urllib.parse import urlencode

from .router import CommandInfo

logger = logging.getLogger(__name__)

PANEL_REMARK = "maimai-bot-main-commands"
PANEL_SCOPE = "group"
PANEL_TARGET_TYPE = "all"
PANEL_API_PATH = "/v2/panels"
# The live group-panel endpoint currently rejects larger global panels with
# “超出数量限制”, even though some documentation advertises a higher ceiling.
# The application has ten command slots. Updating a panel with new command names
# while all slots are occupied is rejected by QQ, so keep this stable primary set;
# /help exposes every newer command without consuming panel slots.
PANEL_ITEM_LIMIT = 10
PANEL_DESCRIPTIONS = {
    "b50": "查询 Best 50 图片",
    "bind": "绑定水鱼查分器",
    "help": "查看完整菜单",
    "song": "搜索舞萌曲目",
    "info": "查看单曲详情",
    "random": "按条件随机谱面",
    "constant": "查询谱面定数",
    "minfo": "查询个人单曲成绩",
    "scores": "筛选个人成绩",
    "analyze": "分析 B50 构成",
    "cosmetic": "设置头像和姓名框",
    "daily": "今日舞萌推荐",
    "about": "项目与数据来源",
}


class QQPanelApi(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def build_panel(commands: Iterable[CommandInfo]) -> dict[str, Any]:
    """Build the official QQ command panel from the router registry."""
    preferred = (
        "b50",
        "bind",
        "help",
        "song",
        "info",
        "random",
        "constant",
        "minfo",
        "scores",
        "daily",
    )
    preferred_order = {name: index for index, name in enumerate(preferred)}
    ordered = sorted(
        commands,
        key=lambda item: (preferred_order.get(item.name, len(preferred_order)), item.name),
    )
    return {
        "items": [
            {
                "name": f"/{command.name}",
                "type": "command",
                "desc": PANEL_DESCRIPTIONS.get(command.name, command.description[:12]),
            }
            for command in ordered[:PANEL_ITEM_LIMIT]
        ],
        "remark": PANEL_REMARK,
    }


async def sync_group_command_panel(
    api: QQPanelApi,
    commands: Iterable[CommandInfo],
) -> str:
    """Create or update the bot's global group command panel idempotently."""
    panel = build_panel(commands)
    query = urlencode({"scope": PANEL_SCOPE, "limit": 20})
    result = await api.request("GET", f"{PANEL_API_PATH}?{query}")
    # QQ omits ``records`` entirely when the first page is empty.
    records = result.get("records", [])
    if not isinstance(records, list):
        raise TypeError("QQ 指令面板列表响应格式异常")

    existing = next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and isinstance(record.get("panel"), dict)
            and record["panel"].get("remark") == PANEL_REMARK
        ),
        None,
    )
    if existing is None:
        created = await api.request(
            "POST",
            PANEL_API_PATH,
            {
                "scope": PANEL_SCOPE,
                "target_type": PANEL_TARGET_TYPE,
                "panel": panel,
            },
        )
        panel_id = created.get("panel_id")
        if not isinstance(panel_id, str) or not panel_id:
            raise RuntimeError("QQ 创建指令面板后未返回 panel_id")
        logger.info("已创建 QQ 群指令面板：%s", panel_id)
        return panel_id

    panel_id = existing.get("panel_id")
    if not isinstance(panel_id, str) or not panel_id:
        raise RuntimeError("QQ 指令面板记录缺少 panel_id")
    await api.request("PUT", f"{PANEL_API_PATH}/{panel_id}", {"panel": panel})
    logger.info("已同步 QQ 群指令面板：%s", panel_id)
    return panel_id
