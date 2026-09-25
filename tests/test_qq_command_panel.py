from __future__ import annotations

from typing import Any

from maimai_bot.domain import MessageContext
from maimai_bot.qq_command_panel import (
    PANEL_ITEM_LIMIT,
    PANEL_REMARK,
    build_panel,
    sync_group_command_panel,
)
from maimai_bot.router import CommandRouter


def make_router() -> CommandRouter:
    async def fallback(_: MessageContext) -> str:
        return "fallback"

    router = CommandRouter(fallback)

    @router.command("help", "查看命令列表")
    async def help_command(_):
        return "help"

    @router.command("b50", "查询 Best 50")
    async def b50_command(_):
        return "b50"

    @router.command("future", "以后新增的功能")
    async def future_command(_):
        return "future"

    return router


class FakeApi:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if method == "GET":
            return {"records": self.records, "is_end": True, "next_cursor": ""}
        if method == "POST":
            return {"panel_id": "new-panel"}
        return {"version": 2}


class EmptyPageApi(FakeApi):
    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if method == "GET":
            return {"is_end": True}
        if method == "POST":
            return {"panel_id": "new-panel"}
        return {"version": 2}


def test_build_panel_prioritizes_primary_commands_and_includes_future_commands() -> None:
    panel = build_panel(make_router().commands)

    assert panel["remark"] == PANEL_REMARK
    assert [item["name"] for item in panel["items"]] == ["/b50", "/help", "/future"]


def test_build_panel_caps_items_at_live_api_limit() -> None:
    router = make_router()
    for index in range(12):

        @router.command(f"extra{index}", "额外命令")
        async def extra_command(_):
            return "extra"

    assert len(build_panel(router.commands)["items"]) == PANEL_ITEM_LIMIT


async def test_sync_creates_panel_when_missing() -> None:
    api = FakeApi([])

    panel_id = await sync_group_command_panel(api, make_router().commands)

    assert panel_id == "new-panel"
    assert api.calls[1][0:2] == ("POST", "/v2/panels")
    assert api.calls[1][2]["scope"] == "group"
    assert api.calls[1][2]["target_type"] == "all"


async def test_sync_accepts_qq_empty_page_without_records_key() -> None:
    api = EmptyPageApi([])

    assert await sync_group_command_panel(api, make_router().commands) == "new-panel"


async def test_sync_updates_its_existing_panel() -> None:
    api = FakeApi(
        [
            {
                "panel_id": "existing-panel",
                "scope": "group",
                "target_type": "all",
                "panel": {"items": [], "remark": PANEL_REMARK},
            }
        ]
    )

    panel_id = await sync_group_command_panel(api, make_router().commands)

    assert panel_id == "existing-panel"
    assert api.calls[1][0:2] == ("PUT", "/v2/panels/existing-panel")
