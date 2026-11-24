from __future__ import annotations

import logging
from urllib.parse import urlsplit

from aiohttp import web

from .domain import CommandRequest
from .services.divingfish import ScoreServiceError
from .services.lxns_scores import UNCONFIGURED, LxnsScores, ScoreSources, lxns_subject

logger = logging.getLogger(__name__)


async def start_callback(service: LxnsScores, port: int | str):
    if not service.enabled:
        return None

    async def callback(request):
        status = 200
        try:
            name = await service.finish(
                request.query.get("state", ""), request.query.get("code", "")
            )
            message = f"已关联落雪玩家「{name}」。回到 QQ，用 /source 落雪 切换舞萌，或用 /中二数据源 落雪 切换中二节奏。"
        except ScoreServiceError as exc:
            status, message = 400, str(exc)
        return web.Response(
            status=status,
            text=message,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app = web.Application()
    runner = web.AppRunner(app, access_log=None)
    try:
        port = int(port)
        app.router.add_get(urlsplit(service.redirect_uri).path, callback, allow_head=False)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except (ValueError, OSError, OverflowError):
        await runner.cleanup()
        service.client_id = ""
        logger.error("落雪 OAuth 回调无法启动，请检查本机监听端口和回调路径。")
        return None
    return runner


def register_source_commands(router, scores: ScoreSources):
    lxns = scores.lxns

    @router.command(
        "source",
        "查看或切换舞萌查分数据源",
        aliases=("数据源", "舞萌数据源"),
        category="设置",
        usage="/source [水鱼|落雪]",
    )
    @router.command(
        "中二数据源",
        "查看或切换中二节奏的查分数据源",
        aliases=("chusource",),
        category="中二节奏",
        usage="/中二数据源 [水鱼|落雪]",
    )
    async def select(request):
        user = lxns_subject(request.context)
        chuni = request.command == "中二数据源"
        game, command = ("中二", "/中二数据源") if chuni else ("舞萌", "/source")
        argument = request.argument.casefold()
        if not argument:
            uses_lxns = lxns.uses_chuni(user) if chuni else lxns.uses_maimai(user)
            return f"当前{game}查分源为{'落雪' if uses_lxns else '水鱼'}。发送 {command} 水鱼 或 {command} 落雪 可切换。"
        if argument not in {"水鱼", "落雪", "lxns", "divingfish"}:
            return f"用法：{command} 水鱼 或 {command} 落雪。"
        wants_lxns = argument in {"落雪", "lxns"}
        if wants_lxns and not lxns.enabled:
            return UNCONFIGURED
        try:
            change = lxns.select_chuni if chuni else lxns.select
            change(user, wants_lxns)
            return f"{game}成绩现在使用{'落雪' if wants_lxns else '水鱼'}。"
        except ScoreServiceError as exc:
            return str(exc)

    @router.command(
        "lxns",
        "授权落雪舞萌与中二成绩，或关联群内身份",
        aliases=("落雪绑定",),
        category="设置",
        usage="/lxns [状态|解绑]",
    )
    async def bind(request):
        ctx, argument = request.context, request.argument.strip()
        user = lxns_subject(ctx)
        if not ctx.user_id or ctx.chat_scope not in {"group", "c2c"}:
            return "请在 QQ 单聊或群聊中使用落雪绑定。"
        if argument == "状态":
            identity = lxns.identity(user)
            return (
                f"此身份已关联落雪玩家「{identity['name']}」。"
                if identity
                else "此身份尚未关联落雪账号。"
            )
        if argument == "解绑":
            lxns.unlink(user, private=ctx.chat_scope == "c2c")
            return (
                "私聊落雪授权与由它关联的群身份已解除。若需撤销官网授权，可在落雪管理已授权应用。"
                if ctx.chat_scope == "c2c"
                else "此群身份的落雪关联已解除。"
            ) + "已选落雪的数据源会保留。用 /source 水鱼 切换舞萌，或用 /中二数据源 水鱼 切换中二。"
        if not lxns.enabled:
            return UNCONFIGURED
        action, _, value = argument.partition(" ")
        try:
            if ctx.chat_scope == "c2c":
                if not argument:
                    return (
                        "在落雪官网授权读取舞萌与中二节奏成绩（此链接 10 分钟内有效）。\n"
                        + lxns.begin(user)
                        + "\n授权后用 /source 落雪 切换舞萌，或用 /中二数据源 落雪 切换中二。"
                    )
                if action == "关联" and value:
                    name = lxns.offer(user, value.strip())
                    return (
                        f"回到你发起关联的群，由你本人发送 /lxns 确认 {name}。\n"
                        "确认后，该群身份可查看这个落雪账号的成绩。"
                    )
                return "私聊使用 /lxns 授权。要关联自己的群身份，发送 /lxns 关联 群内编号。"
            if not argument:
                ref = lxns.new_pair(user)
                return (
                    "请私聊本机器人发送 /lxns，并在落雪官网授权。已授权的玩家可直接在私聊发送\n"
                    f"/lxns 关联 {ref}\n再按私聊提示回本群确认。编号 10 分钟内有效。"
                )
            if action == "确认" and value:
                lxns.confirm(user, value.strip())
                return "此群身份已关联你的落雪账号。用 /source 落雪 切换舞萌，或用 /中二数据源 落雪 切换中二。"
            return "群内使用 /lxns 发起关联，授权在 QQ 私聊和落雪官网完成。"
        except ScoreServiceError as exc:
            return str(exc)

    @router.command("解绑落雪", "解除当前 QQ 身份的落雪关联", category="设置")
    async def unlink(request):
        if request.argument:
            return "仅可操作自己的绑定。发送 /解绑落雪 即可解除。"
        return await bind(CommandRequest(request.context, "lxns", "解绑"))
