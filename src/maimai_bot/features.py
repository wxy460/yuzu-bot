from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from urllib.parse import urlencode

import httpx

from .campus_leisure import register_campus_leisure
from .community_commands import register_community
from .cross_game import CrossGameService, register_cross_game
from .domain import BotReply, CommandRequest, MessageContext, QuickAction
from .group_life import GroupLife, register_group_commands
from .help_catalog import command_examples, command_usage, group_commands, select_group, topic_links
from .lxns_commands import register_source_commands
from .party_games import register_party_games
from .pc50 import register_pc50
from .qq_shortcuts import COSMETIC_QUICK_ACTIONS, HELP_QUICK_ACTIONS
from .router import CommandRouter
from .services.b50_image import B50ImageService, B50RenderError
from .services.bilibili import BilibiliChartService, ChartConfirmationResult, chart_label
from .services.chart_analysis import ChartAnalysisService
from .services.chat import ChatService, ChatUnavailable
from .services.community import CommunityData
from .services.cosmetics import CollectionItem, CosmeticError, CosmeticKind, CosmeticService
from .services.daily_recommendation import (
    build_daily_recommendations,
    format_daily_recommendations,
)
from .services.divingfish import (
    AUTHORIZED_APPS,
    DivingFishService,
    NotBound,
    PlayerRecords,
    ScoreServiceError,
)
from .services.gameplay import (
    DIFFICULTY_MARKS,
    FC_ORDER,
    FS_ORDER,
    PLATE_NAME_PATTERN,
    all_records,
    ap50_text,
    build_plate_progress,
    conditional_scores,
    level_progress_text,
    personal_song_text,
    plate_conditions,
    plate_progress_text,
    scoreline,
    unavailable_history,
)
from .services.lxns_scores import ScoreSources, lxns_subject, score_identity
from .services.rating import minimum_achievement, rating_for
from .services.result_card import academy_card, result_card
from .services.song_catalog import (
    Chart,
    Song,
    SongCatalogError,
    SongCatalogService,
    format_chart,
    format_song,
)
from .services.song_recognition import RecognitionError, SongRecognitionService
from .services.web_search import needs_web_search
from .sticker_commands import register_sticker_commands

logger = logging.getLogger(__name__)
HELP_SITE_URL = "https://fujisawa-yuzu-maimai-bot.chummy-chick-1306.chatgpt.site"
COSMETIC_SITE_URL = f"{HELP_SITE_URL}#cosmetics"
_COMPACT_RANDOM = (
    r"(DX|SD|标准)?\s*([初真超檄橙晓桃樱紫堇白雪辉熊华爽煌宙星祭祝双宴镜彩]代|当前版本)?"
    r"\s*([绿黄红紫白])?\s*(\d{1,2}(?:\+|\.\d)?)"
)


@dataclass(slots=True)
class GuessSession:
    song_id: int
    title: str
    aliases: tuple[str, ...]
    song: Song | None
    initial_clue: str
    hints: tuple[GuessHint, ...]
    hint_index: int = 0


@dataclass(frozen=True, slots=True)
class GuessHint:
    text: str
    cover_level: int | None = None


def build_router(
    chat: ChatService,
    scores: DivingFishService,
    b50_images: B50ImageService,
    cosmetics: CosmeticService,
    catalog: SongCatalogService,
    chart_analysis: ChartAnalysisService,
    bilibili: BilibiliChartService,
    recognition: SongRecognitionService | None = None,
    community: CommunityData | None = None,
    cross_games: CrossGameService | None = None,
    group_life: GroupLife | None = None,
) -> CommandRouter:
    score_context_cache: dict[str, tuple[float, PlayerRecords]] = {}
    water = scores.water if isinstance(scores, ScoreSources) else scores

    def water_identity(user):
        return water.history_key(user) if isinstance(water, DivingFishService) else user

    def conversation_key(context):
        return f"{context.chat_scope}:{context.chat_id}:{water_identity(context.user_id)}"
    guess_sessions: dict[str, GuessSession] = {}
    recognition_waiting: dict[tuple[str, str, str], float] = {}

    async def guess_answer_reply(session: GuessSession, lead: str) -> str | BotReply:
        if session.song is None:
            return f"{lead} {session.title}（ID {session.song_id}）。"
        version_name = catalog.version_name(session.song.version)
        details = format_song(session.song, version_name)
        text = f"{lead} {session.title}（ID {session.song_id}）。\n\n{details}"
        try:
            image_png = await b50_images.render_song_info(
                session.song,
                version_name,
                headline="猜歌答案",
            )
        except B50RenderError as exc:
            logger.warning("guess answer image render failed: %s", exc)
            return text + f"\n\n歌曲详情图生成失败，已返回文字信息：{exc}"
        return BotReply(text=text, image_png=image_png, followup_text=f"{lead} {session.title}（ID {session.song_id}）。")

    async def chat_fallback(context: MessageContext) -> str:
        if not context.content.strip():
            return "你似乎没有输入内容。发送 /help 查看功能。"
        if group_life and context.chat_scope == "group":
            if (re.search(r"群里|大家.{0,6}(?:聊|说|约)|我们(?:刚才|刚刚|在聊|约好)|刚才.{0,6}(?:话题|说了什么)", context.content)
                    and not _asks_about_scores(context.content) and not needs_web_search(context.content)):
                response = await group_life.respond(context, explicit=True)
                if response is not None:
                    return response
            else:
                await asyncio.to_thread(group_life.prepare, context, explicit=True, observe_only=True)
        return await chat_with_scores(context, context.content)

    async def chat_with_scores(context, content, *, force_web=False):
        key = water_identity(context.user_id)
        conversation_id = conversation_key(context)
        try:
            score_context = await _chat_score_context(
                water,
                catalog,
                score_context_cache,
                context.user_id,
                content,
                cache_key=key,
            )
            if key != water_identity(context.user_id):
                return "水鱼绑定在这次查询期间发生变化，请重新提问。"
            extra = {}
            if group_life and context.chat_scope == "group" and not force_web:
                _, _, notes = await asyncio.to_thread(group_life.notes, context, personal=True)
                if notes:
                    extra["speaker_notes"] = [body for _, body in notes if body != content]
            result = await chat.reply(
                conversation_id,
                content,
                user_context=score_context,
                **extra,
                **({"force_web": True} if force_web else {}),
            )
            if key != water_identity(context.user_id):
                return "水鱼绑定在这次查询期间发生变化，请重新提问。"
            return result
        except ChatUnavailable as exc:
            logger.warning("chat unavailable: %s", exc)
            return str(exc)

    router = CommandRouter(chat_fallback)

    async def list_reply(
        title: str,
        records: list[dict],
        fallback_text: str,
        *,
        subtitle: str = "",
    ) -> str | BotReply:
        try:
            image_png = await b50_images.render_record_list(title, records, subtitle=subtitle)
        except B50RenderError as exc:
            logger.warning("list image render failed: %s", exc)
            return fallback_text + f"\n\n图片生成失败，已回退到文字版：{exc}"
        return BotReply(
            text=f"{title}｜共显示 {len(records)} 项。",
            image_png=image_png,
        )

    def quick_reply(
        reply: str | BotReply,
        actions: tuple[QuickAction, ...],
    ) -> BotReply:
        if isinstance(reply, BotReply):
            return BotReply(
                text=reply.text,
                image_png=reply.image_png,
                followup_text=reply.followup_text or (reply.text if reply.image_png else ""),
                quick_actions=actions,
            )
        return BotReply(text=reply, quick_actions=actions)

    @router.command(
        "help", "查看完整命令列表", aliases=("帮助", "菜单"), category="常用", usage="/help [分类]"
    )
    async def help_command(request: CommandRequest) -> str | BotReply:
        if request.argument:
            query = request.argument
            if query == "文字" or query.startswith("文字 "):
                return router.help_text(query.removeprefix("文字").strip())
            category, items = select_group(router.commands, query)
            text = router.help_text(query)
            if not items:
                detail = router.help_command_info(query)
                if not detail:
                    return text
                examples = command_examples(detail)
                return await academy_card(
                    f"/{detail.name}", examples, section="指令用法", note=detail.description,
                    followup="\n".join(example for example, _ in examples),
                )
            topics = topic_links(category, items)
            tips = [*topics, f"发送 /help 文字 {query} 可查看文字版。"]
            if len(items) > 16:
                try:
                    image_png = await asyncio.to_thread(
                        b50_images.render_help, tuple(items), title=f"{category}指令",
                        legacy=not topics, by_topic=bool(topics),
                    )
                except B50RenderError:
                    return text
                return BotReply(text=text, image_png=image_png, followup_text="\n".join(tips))
            return await academy_card(
                f"{category}指令", [(command_usage(item), item.description) for item in items],
                section="帮助", note="发送 /help 指令名 查看所有用法和可复制示例。",
                followup="\n".join([*(command_examples(item)[0][0] for item in items), *tips]),
            )
        site_message = (
            "柚子把完整指令整理成主页啦：\n"
            f"{HELP_SITE_URL}\n\n"
            "如果页面无法打开，请先开启网络代理后重试。"
            "主页支持搜索、分类和一键复制。在 QQ 发送 帮助maimaiDX 直达舞萌，/help pc50 查看用法，/help 文字 打开文字目录。"
        )
        try:
            image_png = await asyncio.to_thread(b50_images.render_help, router.commands)
        except B50RenderError as exc:
            logger.warning("help image render failed: %s", exc)
            return BotReply(
                text=site_message + "\n\n可用分类：" + "、".join(group_commands(router.commands)),
                quick_actions=HELP_QUICK_ACTIONS,
            )
        return BotReply(
            text="柚子指令菜单预览",
            image_png=image_png,
            followup_text=site_message,
            quick_actions=HELP_QUICK_ACTIONS,
        )

    router.pattern(r"^(?:帮助|菜单|help)\s*(maimai\s*dx|maimai|舞萌DX)$", "help", lambda _: "舞萌")

    @router.command("ping", "检查机器人是否在线", category="设置")
    async def ping_command(_: CommandRequest) -> str:
        return "pong — 机器人运行正常。"

    @router.command("reset", "清除当前会话上下文和本地记忆", category="设置")
    async def reset_command(request: CommandRequest) -> str:
        context = request.context
        chat.reset(conversation_key(context))
        if group_life and context.chat_scope == "group":
            await asyncio.to_thread(group_life.forget_person, context, "清除")
        return "已清除你的对话上下文和保存在本机的长期记忆。"

    @router.command("memory", "查看或清除柚子的本地记忆", category="设置")
    async def memory_command(request: CommandRequest) -> str | BotReply:
        context = request.context
        conversation_id = conversation_key(context)
        if request.argument.casefold() in {"clear", "reset", "清除", "清空", "重置"}:
            chat.reset(conversation_id)
            if group_life and context.chat_scope == "group":
                await asyncio.to_thread(group_life.forget_person, context, "清除")
            return "这段会话的本地记忆已经清空啦。以后聊到的新事情，柚子会重新记住的。"
        count = chat.memory_count(conversation_id)
        return await academy_card(
            "会话记忆", [("范围", "你在当前会话中的消息。")],
            section="设置", metric=("已保留的用户消息", f"{count} 条"),
            followup="发送 /memory clear 可以清空当前会话记忆。",
        )

    @router.command(
        "web",
        "强制联网搜索后由柚子回答",
        aliases=("联网",),
        category="常用",
        usage="/web 要查询的问题",
    )
    async def web_command(request: CommandRequest) -> str:
        if not request.argument.strip():
            return "请在 /web 后输入要查询的问题，例如：/web 舞萌DX最近有什么更新"
        return await chat_with_scores(request.context, request.argument, force_web=True)

    @router.command("绑定账号", "查看舞萌与中二查分源及绑定方式", category="设置")
    async def binding_options(request: CommandRequest) -> BotReply:
        has_sources = isinstance(scores, ScoreSources)
        current = scores.source(request.context) if has_sources else "水鱼"
        methods = [f"当前舞萌查分源为{current}。"]
        actions = []
        if getattr(water, "enabled", False):
            methods.append("水鱼：发送 /bind 在水鱼官网授权。")
            actions.append(QuickAction("绑定水鱼", "/bind"))
        else:
            methods.append("水鱼：管理员尚未配置 OAuth 应用。")
        if has_sources and scores.lxns.enabled:
            methods.append("落雪：发送 /lxns 查看只读授权与群关联步骤。")
            actions.append(QuickAction("绑定落雪", "/lxns"))
        else:
            methods.append("落雪：管理员尚未启用 OAuth 授权。")
        if has_sources:
            methods.append("发送 /舞萌数据源 水鱼 或 /舞萌数据源 落雪 切换。")
        if cross_games is not None:
            methods.append(f"当前中二查分源为{cross_games.source(request.context)}。"
                           "水鱼中二单独使用 /chubind 授权，落雪使用 /lxns 授权。"
                           "发送 /中二数据源 水鱼 或 /中二数据源 落雪 切换。")
        return await academy_card(
            "绑定账号", [("", line) for line in methods],
            section="账号设置", actions=tuple(actions),
        )

    @router.command("bind", "绑定水鱼查分器账号", aliases=("绑定",), category="常用")
    async def bind_command(request: CommandRequest) -> str:
        try:
            binding = await scores.start_binding(request.context.user_id)
        except ScoreServiceError as exc:
            logger.warning("score binding failed: %s", exc)
            return str(exc)
        minutes = max(1, binding.expires_in // 60)
        return (
            "请用浏览器打开下面的水鱼官方授权链接，并核对页面上的绑定身份：\n"
            f"{binding.url}\n用户码：{binding.user_code}\n链接约 {minutes} 分钟内有效。"
        )

    @router.command("unbind", "停用本人当前 QQ 身份的水鱼舞萌绑定", aliases=("解绑水鱼",),
                    category="设置", usage="/unbind → /unbind 确认")
    async def unbind(request: CommandRequest) -> BotReply | str:
        ctx = request.context
        if not ctx.user_id or ctx.chat_scope not in {"group", "c2c", "channel", "direct"}:
            return "请从你发起水鱼绑定的 QQ 会话发送 /unbind。"
        if request.argument not in {"", "确认"}:
            return "仅可操作发送者自己的绑定。发送 /unbind 查看说明。"
        manage = QuickAction("水鱼授权管理", url=AUTHORIZED_APPS)
        if not request.argument:
            return BotReply(text="这会停用你当前 QQ 身份的水鱼舞萌查分，柚子也会从新的对话记忆开始。\n"
                "发送 /unbind 确认 后生效。水鱼官网的应用授权可另外由你本人管理。\n" + AUTHORIZED_APPS,
                quick_actions=(QuickAction("确认停用", "/unbind 确认"), manage))
        old_key = water_identity(ctx.user_id)
        try:
            water.unlink(ctx.user_id)
        except sqlite3.Error:
            return "暂时无法保存水鱼解绑设置，请稍后重试。"
        score_context_cache.pop(old_key, None)
        return BotReply(text="机器人已停用你当前 QQ 身份的水鱼舞萌查分，并停止使用旧成绩缓存与对话上下文。"
            "需要重新使用时，请发送 /bind 并重新授权。\n"
            "水鱼官网的授权由你本人管理。若要在官网撤销，请打开「已授权应用」，核对应用和权限后自行确认。"
            "如果页面按整个应用撤销，请同时核对其中的中二权限。\n" + AUTHORIZED_APPS,
            quick_actions=(manage,))

    @router.command("b50", "查询已绑定账号的 Best 50 图片", aliases=("maib50",), category="常用")
    async def b50_command(request: CommandRequest) -> str:
        try:
            if community is not None:
                history_key = (
                    scores.history_key(request.context)
                    if isinstance(scores, ScoreSources) else request.context.user_id
                )
            result = await scores.best50(score_identity(scores, request.context))
            if community is not None:
                try:
                    community.trend(history_key, result)
                except (OSError, sqlite3.Error) as exc:
                    logger.warning("记录底分趋势失败：%s", type(exc).__name__)
        except NotBound:
            return "你还没有授权查分。请先发送 /bind，完成授权后再发送 /b50。"
        except ScoreServiceError as exc:
            logger.warning("score query failed: %s", exc)
            return str(exc)
        try:
            cosmetic_profile = await cosmetics.profile(request.context.user_id)
        except CosmeticError as exc:
            logger.warning("cosmetic asset unavailable: %s", exc)
            cosmetic_profile = None
        try:
            image_png = await b50_images.render(
                result,
                cosmetics=cosmetic_profile,
                avatar_url=request.context.avatar_url,
            )
        except B50RenderError as exc:
            logger.warning("B50 image render failed: %s", exc)
            return result.as_text() + f"\n\n图片生成失败，已回退到文字版：{exc}"
        return BotReply(text=result.as_text(), image_png=image_png)

    @router.command("ap50", "列出已绑定账号 Rating 最高的 50 张 AP/AP+ 谱面", aliases=("maiap50",), category="成绩")
    async def ap50_command(request: CommandRequest) -> str | BotReply:
        records, error = await _get_records(scores, request)
        if error:
            return error
        selected = _ap_records(records)[:50]
        return await list_reply(
            f"{records.nickname} 的 AP50",
            selected,
            ap50_text(records),
            subtitle=f"按单谱 Rating 排序 · AP/AP+ 共 {len(selected)} 张",
        )

    @router.command(
        "r50", "查询最近 50 次游玩（数据源支持时）", aliases=("recent50",), category="成绩"
    )
    async def recent_command(request: CommandRequest) -> str | BotReply:
        if isinstance(scores, ScoreSources) and scores.source(request.context) == "落雪":
            try:
                records = await scores.lxns.records(lxns_subject(request.context), mode="recents")
            except ScoreServiceError as exc:
                return str(exc)
            if not records.old and not records.new:
                return "落雪还没有缓存最近游玩记录。请先在落雪进行增量成绩同步。"
        else:
            records, error = await _get_records(scores, request)
            if error:
                return error
        candidates = [
            item
            for item in all_records(records)
            if item.get("play_time") or item.get("last_played_time")
        ]
        if not candidates:
            return unavailable_history("r50", records)
        candidates.sort(
            key=lambda item: str(item.get("play_time") or item.get("last_played_time")),
            reverse=True,
        )
        fallback = "最近 50 次游玩：\n" + "\n".join(
            _score_line(item, index + 1, "R50") for index, item in enumerate(candidates[:50])
        )
        displayed = [
            {
                **item,
                "_list_extra": str(item.get("play_time") or item.get("last_played_time") or ""),
            }
            for item in candidates[:50]
        ]
        return await list_reply(
            f"{records.nickname} 的 Recent 50",
            displayed,
            fallback,
            subtitle="按最近游玩时间倒序排列",
        )

    register_pc50(router, scores, catalog, cosmetics, b50_images, community)

    @router.command("minfo", "查询自己的某首歌全部成绩", aliases=("score",), category="成绩", usage="minfo 潘")
    async def minfo_command(request: CommandRequest) -> str | BotReply:
        if not request.argument:
            return "用法：minfo 曲名、别名或歌曲 ID"
        try:
            song = await catalog.resolve(re.sub(r"^m(?=\d+$)", "", request.argument, flags=re.IGNORECASE))
        except SongCatalogError as exc:
            return str(exc)
        if song is None:
            return "没有唯一匹配的曲目，请先用查歌确认 ID。"
        records, error = await _get_records(scores, request)
        if error:
            return error
        matched = _records_for_song(records, song)
        if not matched:
            return personal_song_text(records, song)
        return await list_reply(
            f"{records.nickname}｜{song.title}",
            matched,
            personal_song_text(records, song),
            subtitle=f"歌曲 ID {song.id} · 全部已记录谱面",
        )

    @router.command(
        "scores",
        "按等级、难度、类型或成绩筛选个人记录",
        aliases=("成绩", "scorelist"),
        category="成绩",
        usage="成绩 14+ 紫 AP",
    )
    async def scores_command(request: CommandRequest) -> str | BotReply:
        condition = re.sub(r"\blv(?=\d)", "", request.argument.strip(), flags=re.IGNORECASE)
        page = 1
        if m := re.search(r"\s+(?:p(\d{1,4})|第(\d{1,4})页)$", condition, re.IGNORECASE):
            page = int(m[1] or m[2])
            condition = condition[:m.start()]
        elif request.context.content.lstrip("/").casefold().startswith("scorelist "):
            if m := re.fullmatch(r"(.+)\s+(\d{1,3})", condition):
                condition, page = m[1], int(m[2])
        if not condition:
            return "用法：/scores 14+ 紫 AP，支持等级、DX/SD、难度颜色、评级或 FC/AP，末尾加 p2 翻页。"
        records, error = await _get_records(scores, request)
        if error:
            return error
        selected = _filter_score_records(records, condition)
        if selected is None:
            return conditional_scores(records, condition)
        pages = max(1, (len(selected)+49)//50)
        if not 1 <= page <= pages:
            return f"本次查询共 {pages} 页。"
        displayed = selected[(page-1)*50:page*50]
        fallback = "\n".join(_score_line(v,i,"成绩") for i,v in enumerate(displayed, (page-1)*50+1))
        return await list_reply(
            f"成绩筛选「{condition}」",
            displayed,
            fallback,
            subtitle=f"{records.nickname} · 共 {len(selected)} 张 · 第 {page}/{pages} 页 · 末尾加 p2 翻页",
        )

    @router.command(
        "scoretable",
        "生成指定等级或定数的个人成绩长图",
        aliases=("分表",),
        category="成绩",
        usage="14分表 / 13+分表 / 14.7分表",
    )
    async def scoretable_command(request: CommandRequest) -> str | BotReply:
        selector = request.argument.strip()
        if not re.fullmatch(r"\d{1,2}(?:\+|\.\d)?", selector):
            return "用法：14分表、13+分表或 14.7分表；整数/加号按等级，小数按精确定数。"
        player, error = await _get_records(scores, request)
        if error:
            return error
        if "." in selector:
            target = float(selector)
            selected = [
                item
                for item in all_records(player)
                if abs(float(item.get("ds", 0) or 0) - target) < 0.001
            ]
            label = f"定数 {target:.1f}"
        else:
            selected = [
                item for item in all_records(player) if str(item.get("level") or "") == selector
            ]
            label = f"Lv.{selector}"
        selected.sort(key=_score_table_sort_key)
        selected = selected[:100]
        try:
            cosmetic_profile = await cosmetics.profile(request.context.user_id)
        except CosmeticError as exc:
            logger.warning("cosmetic asset unavailable: %s", exc)
            cosmetic_profile = None
        try:
            image_png = await b50_images.render_score_table(
                player,
                label,
                selected,
                cosmetics=cosmetic_profile,
                avatar_url=request.context.avatar_url,
            )
        except B50RenderError as exc:
            logger.warning("score table image render failed: %s", exc)
            rows = [f"{player.nickname}｜{label} 分表（图片生成失败，返回文字版）"]
            rows.extend(_score_line(item, index + 1, "分表") for index, item in enumerate(selected))
            return "\n".join(rows) + f"\n\n{exc}"
        return BotReply(
            text=f"{player.nickname}｜{label} 分表，共显示 {len(selected)} 张谱面。",
            image_png=image_png,
        )

    @router.command(
        "song", "按标题、艺人或 ID 搜索曲目", aliases=("查歌",), category="曲目", usage="查歌 潘"
    )
    async def song_command(request: CommandRequest) -> str | BotReply:
        if not request.argument:
            return "用法：/song 曲名或歌曲 ID，例如 /song 834"
        try:
            matches = await catalog.search(request.argument)
        except SongCatalogError as exc:
            return str(exc)
        if not matches:
            return "没有找到匹配曲目。可尝试缩短关键词，或直接输入歌曲 ID。"
        exact = [
            song
            for song in matches
            if request.argument.isdecimal()
            and song.id == int(request.argument)
            or song.title.casefold() == request.argument.casefold()
        ]
        if len(matches) == 1 or exact:
            song = exact[0] if exact else matches[0]
            version_name = catalog.version_name(song.version)
            text = format_song(song, version_name)
            confirmations = await bilibili.confirmations(song)
            resources, resource_actions = _song_resources(confirmations)
            try:
                image_png = await b50_images.render_song_info(song, version_name)
            except B50RenderError as exc:
                logger.warning("song detail image render failed: %s", exc)
                fallback = text + f"\n\n图片生成失败，已回退到文字版：{exc}"
                if resources:
                    fallback += f"\n\n{resources}"
                return BotReply(text=fallback, quick_actions=resource_actions)
            return BotReply(
                text=text,
                image_png=image_png,
                followup_text=resources,
                quick_actions=resource_actions,
            )
        rows = [f"找到 {len(matches)} 个候选（发送 /song ID 查看详情）："]
        rows.extend(f"{song.id} — {song.title} / {song.artist}" for song in matches)
        entries = [_song_list_record(song) for song in matches[:20]]
        return await list_reply(
            f"查歌候选｜{request.argument}",
            entries,
            "\n".join(rows),
            subtitle="发送 /song ID 查看完整谱面详情",
        )

    def recognition_key(context: MessageContext) -> tuple[str, str, str]:
        return context.chat_scope, context.chat_id, context.user_id

    async def recognize_image(context: MessageContext) -> str | BotReply:
        recognition_waiting.pop(recognition_key(context), None)
        if recognition is None:
            return "成绩识图暂未启用。"
        try:
            result = await recognition.recognize(context.image_urls[0])
        except RecognitionError as exc:
            return str(exc) + "\n发送 /识图 可重新尝试。"
        if result.song is not None:
            answer = await song_command(CommandRequest(context, "song", str(result.song.id)))
            lead = f"识图结果：{result.song.title}（ID {result.song.id}，依据：{result.method}）。"
            if isinstance(answer, BotReply):
                return BotReply(
                    text=answer.text,
                    image_png=answer.image_png,
                    followup_text=lead + "\n\n" + (answer.followup_text or answer.text),
                    quick_actions=answer.quick_actions,
                )
            return lead + "\n\n" + answer
        if result.candidates:
            return "识别到多个可能的曲目，请用 /song ID 确认：\n" + "\n".join(
                f"{song.id} — {song.title}" for song in result.candidates
            )
        suffix = (
            "曲绘库首次建立中，请稍后重试；有清晰曲名的成绩图仍可识别。"
            if result.indexing else "请尽量发送单曲成绩原图，保留清晰的曲名或完整曲绘。"
        )
        return "没有可靠识别出歌曲。" + suffix + "\n发送 /识图 可重新尝试。"

    def waiting_for_image(context: MessageContext) -> bool:
        deadline = recognition_waiting.get(recognition_key(context), 0)
        return bool(
            context.image_urls and deadline > time.monotonic()
            and not router.recognizes_command(context.content)
        )

    router.set_image_handler(recognize_image, waiting_for_image)

    @router.command(
        "recognize", "识别下一张成绩图的歌曲并返回歌曲详情", aliases=("识图", "成绩识图"),
        category="曲目", usage="/识图 → 3 分钟内发送成绩图（/识图 取消）",
    )
    async def recognize_command(request: CommandRequest) -> str | BotReply:
        context = request.context
        key = recognition_key(context)
        if request.argument.strip().casefold() in {"取消", "cancel"}:
            recognition_waiting.pop(key, None)
            return "已取消这次成绩识图。"
        if recognition is None:
            return "成绩识图暂未启用。"
        if context.image_urls:
            return await recognize_image(context)
        now = time.monotonic()
        for old_key, deadline in list(recognition_waiting.items()):
            if deadline <= now:
                recognition_waiting.pop(old_key, None)
        if len(recognition_waiting) >= 1024:
            return "目前等待识图的人较多，请稍后重试。"
        recognition_waiting[key] = now + 180
        recognition.start_warming()
        return (
            "请在 3 分钟内发送你的下一张成绩图片，柚子会识别曲名或曲绘，"
            "并返回 /song 一样的歌曲详情。每次识别一张图；发送 /识图 取消 可取消。\n"
            "若本群未开启接收全部消息，发图时仍需 @机器人；也可以把 /识图 和图片一起发送。"
        )

    @router.command("info", "查看单曲与全部谱面详情", category="曲目", usage="info 潘")
    async def info_command(request: CommandRequest) -> str | BotReply:
        if not request.argument:
            return "用法：info 曲名、别名或歌曲 ID"
        try:
            song = await catalog.resolve(request.argument)
        except SongCatalogError as exc:
            return str(exc)
        if song is None:
            return "没有唯一匹配的曲目，请先用「查歌 关键词」找到歌曲 ID。"
        return await catalog_card(song, "曲目情报")

    async def catalog_card(song: Song, title: str, chart: Chart | None = None) -> BotReply:
        version = catalog.version_name(song.version)
        text = format_chart(chart) if chart else format_song(song, version)
        try:
            image = await b50_images.render_song_info(song, version, headline=title, featured_chart=chart)
        except B50RenderError:
            return BotReply(text=text)
        return BotReply(text=text, image_png=image)

    register_campus_leisure(router, catalog, catalog_card)

    async def chart_card(chart: Chart, title: str) -> str | BotReply:
        try:
            song = await catalog.resolve(str(chart.song_id))
        except SongCatalogError:
            song = None
        return await catalog_card(song, title, chart) if song else format_chart(chart)

    @router.command("chart", "查看指定难度谱面详情和物量", category="曲目", usage="紫潘 / 紫id 834")
    async def chart_command(request: CommandRequest) -> str | BotReply:
        match = re.match(r"^([绿黄红紫白])\s*(?:id\s*)?(.+)$", request.argument, re.IGNORECASE)
        if not match:
            return "用法：紫潘、紫id 834，或 /chart 紫 潘"
        color, query = match.groups()
        try:
            chart = await catalog.chart(query, DIFFICULTY_MARKS[color])
        except SongCatalogError as exc:
            return str(exc)
        return await chart_card(chart, "谱面详情") if chart else "没有找到唯一曲目，或该曲目没有这个难度。"

    @router.command(
        "aliases", "查询一首歌的全部别名", aliases=("别名",), category="曲目", usage="潘有什么别名"
    )
    async def aliases_command(request: CommandRequest) -> str | BotReply:
        try:
            result = await catalog.aliases_for(request.argument)
        except SongCatalogError as exc:
            return str(exc)
        if result is None:
            return "没有找到唯一匹配的曲目。"
        song, aliases = result
        names = "、".join(aliases) if aliases else "公共别名库暂未收录"
        return await result_card(
            song.title, [("歌曲 ID", str(song.id)), ("别名", names)],
            section="曲目", followup=f"/song {song.id} · {names}",
        )

    @router.command("which", "用别名反查歌曲", category="曲目", usage="11451是什么歌")
    async def which_command(request: CommandRequest) -> str | BotReply:
        query = request.argument
        unavailable = []
        try:
            alias_matches = await catalog.alias_matches(query)
            matches = alias_matches or await catalog.search(query, limit=10)
        except SongCatalogError as exc:
            if cross_games is None:
                return str(exc)
            matches = []
            unavailable.append("舞萌曲库暂时无法读取。")
        if cross_games:
            try:
                chuni = await cross_games.chuni_search(query, request.context)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                chuni = []
                unavailable.append("中二曲库暂时无法读取。")
            if chuni or unavailable:
                rows = [(f"舞萌 m{song.id}", song.title) for song in matches[:10]]
                rows.extend((f"中二 z{song['id']}", song["title"]) for song in chuni[:10])
                return await academy_card(f"别名反查 · {query}", rows or [("曲库", "暂无匹配曲目")],
                                          section="曲目索引", note="".join(unavailable) + "发送 /id 编号 查看详情。")
        if not matches:
            return f"没有找到别名或 ID 为「{query}」的歌曲。"
        fallback = "你要找的可能是：\n" + "\n".join(f"{song.id} — {song.title}" for song in matches)
        return await list_reply(
            f"别名反查｜{query}",
            [_song_list_record(song) for song in matches[:20]],
            fallback,
            subtitle="候选曲目 · 使用歌曲 ID 可精确查询",
        )

    @router.command(
        "random",
        "按等级、颜色、版本和类型随机谱面",
        aliases=("随个","mai什么"),
        category="随机",
        usage="随个 DX 紫 14",
    )
    async def random_command(request: CommandRequest) -> str | BotReply:
        query = request.argument
        if query == "推分" and request.context.content.lstrip("/").startswith("mai什么"):
            return await daily_command(request)
        compact = re.fullmatch(_COMPACT_RANDOM, query, re.IGNORECASE)
        if compact:
            query = " ".join(part for part in compact.groups() if part)
        try:
            chart = await catalog.random_chart(query)
        except SongCatalogError as exc:
            return str(exc)
        return await chart_card(chart, "随机谱面")

    @router.command(
        "constant",
        "查询定数或定数范围内的谱面",
        aliases=("定数",),
        category="曲目",
        usage="定数 14.0 14.4",
    )
    async def constant_command(request: CommandRequest) -> str | BotReply:
        values = request.argument.replace("-", " ").split()
        if not 1 <= len(values) <= 2:
            return "用法：/constant 14.7，或 /constant 14.7 14.9"
        try:
            low = float(values[0])
            high = float(values[-1])
        except ValueError:
            return "定数格式不正确，例如：/constant 14.7 14.9"
        low, high = min(low, high), max(low, high)
        if high - low > 1:
            return "为避免刷屏，一次查询的定数跨度不能超过 1.0。"
        try:
            charts = await catalog.charts_in_range(low, high)
        except SongCatalogError as exc:
            return str(exc)
        if not charts:
            return "这个定数范围内没有找到谱面。"
        rows = [f"定数 {low:.1f}–{high:.1f}：共 {len(charts)} 张谱面"]
        rows.extend(
            f"{chart.constant:.1f}｜{chart.song_id} {chart.title} [{chart.type_label} {chart.difficulty_name}]"
            for chart in charts[:20]
        )
        if len(charts) > 20:
            rows.append(f"其余 {len(charts) - 20} 条未显示，请缩小定数范围。")
        displayed = [_chart_list_record(chart) for chart in charts[:50]]
        return await list_reply(
            f"定数 {low:.1f}–{high:.1f}",
            displayed,
            "\n".join(rows),
            subtitle=f"共 {len(charts)} 张谱面 · 按定数与难度排列，最多显示 50 张",
        )

    @router.command("ra", "计算单谱 Rating", category="工具", usage="ra 14.7 100.5")
    async def rating_command(request: CommandRequest) -> str | BotReply:
        values = request.argument.rstrip("%").split()
        if len(values) != 2:
            return "用法：/ra 谱面定数 达成率，例如 /ra 14.7 100.5"
        try:
            constant, achievement = map(float, values)
            rating = rating_for(constant, achievement)
        except ValueError as exc:
            return f"参数格式不正确：{exc}"
        return await result_card(
            "单谱 Rating", [("谱面定数", f"{constant:.1f}"), ("达成率", f"{achievement:.4f}%")],
            section="工具", metric=("单谱 RA", str(rating)),
        )

    @router.command(
        "scoreline",
        "计算指定谱面的 GREAT 容错",
        aliases=("分数线",),
        category="工具",
        usage="分数线 紫潘 100.5",
    )
    async def scoreline_command(request: CommandRequest) -> str | BotReply:
        match = re.match(
            r"^([绿黄红紫白])\s*(?:id\s*)?(.+?)\s+(\d+(?:\.\d+)?)%?$",
            request.argument,
            re.IGNORECASE,
        )
        if not match:
            return "用法：分数线 紫潘 100.5，或 分数线 紫id 834 100.5"
        color, query, target_text = match.groups()
        try:
            chart = await catalog.chart(query, DIFFICULTY_MARKS[color])
        except SongCatalogError as exc:
            return str(exc)
        if chart is None:
            return "没有找到唯一曲目，或该曲目没有这个难度。"
        summary = scoreline(chart, float(target_text)).splitlines()
        if len(summary) < 2:
            return summary[0]
        return await result_card(
            chart.title, [("", line) for line in summary[1:-1]], section="工具",
            metric=(f"{chart.type_label} {chart.difficulty_name} · 目标分数线", f"{float(target_text):.4f}%"),
            note=summary[-1],
        )

    @router.command(
        "achieve",
        "按判定数估算达成率",
        aliases=("达成率计算",),
        category="工具",
        usage="achieve 总物量 P G GD Miss",
    )
    async def achieve_command(request: CommandRequest) -> str | BotReply:
        values = request.argument.split()
        if len(values) != 5:
            return "用法：achieve 总物量 PERFECT GREAT GOOD MISS（按 TAP 等价估算；精确计算请用分数线）"
        try:
            total, perfect, great, good, miss = map(int, values)
        except ValueError:
            return "参数必须都是整数。"
        if (
            min(total, perfect, great, good, miss) < 0
            or perfect + great + good + miss != total
            or total == 0
        ):
            return "P + G + GD + Miss 必须正好等于总物量，且不能为负数。"
        achievement = (perfect + great * 0.8 + good * 0.5) / total * 100
        return await result_card(
            "判定成绩估算", [("总物量", str(total)), ("PERFECT / GREAT", f"{perfect} / {great}"),
                        ("GOOD / MISS", f"{good} / {miss}")],
            metric=("TAP 等价达成率", f"{achievement:.4f}%"), section="工具",
            note="各音符按 TAP 权重估算，不含 BREAK 额外 1%。",
        )

    @router.command(
        "reverse",
        "解释目标达成率的失分空间",
        aliases=("达成率反推",),
        category="工具",
        usage="达成率反推 紫潘 100.4876",
    )
    async def reverse_command(request: CommandRequest) -> str | BotReply:
        if re.fullmatch(r"\d+(?:\.\d+)?%?", request.argument):
            target = float(request.argument.rstrip("%"))
            loss = 101 - target
            if not 0 <= target <= 101:
                return "目标达成率应当在 0 到 101% 之间。"
            return await result_card(
                "达成率反推", [("目标达成率", f"{target:.4f}%"), ("理论达成率", "101.0000%")],
                section="工具", metric=("允许损失", f"{loss:.4f}%"),
                followup=f"换算具体谱面的 GREAT 容错可发「分数线 紫曲名 {target:.4f}」。",
            )
        return await scoreline_command(request)

    @router.command(
        "level", "查询等级完成表", category="等级", usage="14完成表 / 14ap / 14fc / 14sss+"
    )
    async def level_command(request: CommandRequest) -> str | BotReply:
        values = request.argument.split()
        if not values:
            return "用法：14完成表、14ap、14fc、14sss+"
        level = values[0]
        goal = values[1] if len(values) > 1 else "完成表"
        records, error = await _get_records(scores, request)
        if error:
            return error
        try:
            charts = [chart for song in await catalog.songs() for chart in song.charts]
        except SongCatalogError as exc:
            return str(exc)
        fallback = level_progress_text(records, level, goal, charts)
        unfinished, completed, total = _level_unfinished_records(records, charts, level, goal)
        if not unfinished:
            return fallback
        label = goal.casefold().replace("完成表", "") or "游玩"
        return await list_reply(
            f"Lv.{level} {label.upper()} 完成表",
            unfinished[:50],
            fallback,
            subtitle=f"{records.nickname} · 已完成 {completed}/{total} · 下列为未完成谱面",
        )

    @router.command(
        "plate", "查询版本牌子完成情况", category="牌子", usage="橙将进度 / 镜将进度 / 彩将进度"
    )
    async def plate_command(request: CommandRequest) -> str | BotReply:
        plate = request.argument.removesuffix("完成表").removesuffix("进度").strip()
        records, error = await _get_records(scores, request)
        if error:
            return error
        try:
            progress = await build_plate_progress(records, catalog, plate)
        except SongCatalogError as exc:
            return str(exc)
        except ValueError as exc:
            return str(exc)
        try:
            cosmetic_profile = await cosmetics.profile(request.context.user_id)
        except CosmeticError as exc:
            logger.warning("cosmetic asset unavailable: %s", exc)
            cosmetic_profile = None
        try:
            image_png = await b50_images.render_plate_progress(
                records,
                progress,
                cosmetics=cosmetic_profile,
                avatar_url=request.context.avatar_url,
            )
        except B50RenderError as exc:
            logger.warning("plate progress image render failed: %s", exc)
            return await plate_progress_text(records, catalog, plate) + f"\n\n图片生成失败：{exc}"
        return BotReply(
            text=progress.summary(records.nickname),
            image_png=image_png,
        )

    @router.command(
        "plate-conditions", "查看极、将、神、舞舞、霸者条件", aliases=("牌子条件",), category="牌子"
    )
    async def plate_conditions_command(_: CommandRequest) -> BotReply:
        title, *lines = plate_conditions().splitlines()
        rows = [(s.partition("：")[0], s.partition("：")[2]) for s in lines]
        return await result_card(title.rstrip("："), rows, section="牌子")

    @router.command("analyze", "分析当前 B50 构成与底分", aliases=("b50分析",), category="成绩")
    async def analyze_command(request: CommandRequest) -> str | BotReply:
        try:
            result = await scores.best50(score_identity(scores, request.context))
        except NotBound:
            return "你还没有授权查分。请先发送 /bind。"
        except ScoreServiceError as exc:
            return str(exc)
        return await _b50_analysis_card(result)

    @router.command(
        "threshold", "计算指定定数踢进 B35/B15 所需达成率", aliases=("进榜线",), category="成绩"
    )
    async def threshold_command(request: CommandRequest) -> str | BotReply:
        try:
            constant = float(request.argument)
        except ValueError:
            return "用法：/threshold 谱面定数，例如 /threshold 14.7"
        try:
            result = await scores.best50(score_identity(scores, request.context))
        except NotBound:
            return "你还没有授权查分。请先发送 /bind。"
        except ScoreServiceError as exc:
            return str(exc)
        return await _threshold_card(result, constant)

    @router.command(
        "daily",
        "按当前 Rating 获取今天的 10 首推荐谱面",
        aliases=("今日舞萌", "每日推歌","mai什么推分"),
        category="娱乐",
    )
    async def daily_command(request: CommandRequest) -> str | BotReply:
        try:
            player, songs = await asyncio.gather(
                scores.player_records(score_identity(scores, request.context)),
                catalog.songs(),
            )
        except NotBound:
            return "每日推歌需要读取你的水鱼成绩来拟合难度。请先发送 /bind。"
        except ScoreServiceError as exc:
            return str(exc)
        except SongCatalogError as exc:
            return str(exc)
        plan = build_daily_recommendations(
            player,
            songs,
            catalog.version_name,
            request.context.user_id,
        )
        if not plan.items:
            return "暂时没有找到适合推荐且尚未达到 100.0000% 的谱面。"
        bpm_by_song = {song.id: song.bpm for song in songs}
        patterns = await asyncio.gather(
            *(
                chart_analysis.brief(item.chart, bpm_by_song.get(item.chart.song_id, 0))
                for item in plan.items
            )
        )
        plan = plan.with_patterns(list(patterns))
        text = format_daily_recommendations(plan)
        try:
            image_png = await b50_images.render_daily_recommendations(plan)
        except B50RenderError as exc:
            logger.warning("daily recommendation image render failed: %s", exc)
            return text + f"\n\n图片生成失败，已回退到文字版：{exc}"
        difficulty_notes = "\n".join(
            f"{index}. {item.chart.title} · {item.pattern or '暂无公开谱面配置说明'}"
            for index, item in enumerate(plan.items, 1)
        )
        return BotReply(text=text, image_png=image_png, followup_text=difficulty_notes)

    @router.command(
        "guess",
        "按定数、版本、难度或谱面类型开始猜歌",
        aliases=("猜歌",),
        category="娱乐",
        usage="猜歌 [14.0-14.5] [桃-紫] [DX] / 曲绘猜歌 14+",
    )
    async def guess_command(request: CommandRequest) -> str | BotReply:
        if (
            community is not None
            and request.argument in {"简单", "普通", "困难"}
            and request.context.content.lstrip("/").casefold().startswith("guess ")
        ):
            return await router.dispatch(replace(request.context, content="/猜词 " + request.argument))
        if request.argument.strip().casefold() in {"help", "帮助", "用法"}:
            lines = _guess_help_text().splitlines()
            examples = [line.removeprefix("• ") for line in lines if line.startswith("• ")]
            return await result_card(
                "猜歌限定范围", [("组合示例", "\n".join(examples))],
                section="娱乐", note=lines[-1], followup="\n".join(examples),
            )
        mode, filters = _parse_guess_request(request.argument)
        chart_query = _guess_chart_query(filters)
        try:
            chart = await catalog.random_chart(chart_query)
            aliases = (await catalog.aliases_for(str(chart.song_id)) or (None, ()))[1]
            song = next(
                (item for item in await catalog.songs() if item.id == chart.song_id),
                None,
            )
        except SongCatalogError as exc:
            return str(exc)
        if mode == "cover":
            try:
                image = await catalog.cover_clue(chart.song_id, reveal_level=0)
            except SongCatalogError as exc:
                return str(exc)
            clue = "局部曲绘"
        elif mode == "chart":
            clue = f"谱师：{chart.designer or '未知'}；{chart.type_label} {chart.level}；物量 {chart.note_count or '未知'}"
        elif mode == "note":
            clue = f"TAP {chart.tap} / HOLD {chart.hold} / SLIDE {chart.slide} / TOUCH {chart.touch} / BREAK {chart.break_count}"
        elif mode == "letter":
            clue = "标题首字母：" + " ".join(
                word[0].upper() for word in re.findall(r"[A-Za-z0-9]+", chart.title)
            )
            if clue.endswith("："):
                clue += chart.title[0]
        else:
            mode = "normal"
            clue = f"艺术家：{chart.artist}；分类：{chart.genre}"
        key = f"{request.context.chat_scope}:{request.context.chat_id}"
        guess_sessions[key] = GuessSession(
            song_id=chart.song_id,
            title=chart.title,
            aliases=aliases,
            song=song,
            initial_clue=clue,
            hints=_build_guess_hints(
                mode,
                chart,
                bpm=song.bpm if song is not None else 0,
                version_name=catalog.version_name(chart.version),
                filters=filters,
            ),
        )
        if mode == "cover":
            text = "曲绘猜歌开始！" + (f"\n限定范围：{filters}" if filters else "")
            text += "\n发送「答 曲名/别名/ID」作答，或发送「提示」扩大曲绘。"
            return BotReply(text=text, image_png=image, followup_text=text)
        rows = [("限定范围", filters)] if filters else []
        rows.append(("初始线索", clue))
        return await result_card(
            "猜歌开始", rows, section="娱乐",
            note="发送「答 曲名/别名/ID」作答，或发送「提示」「答案」。",
        )

    @router.command("answer", "回答当前猜歌", aliases=("答",), category="娱乐", usage="答 曲名")
    async def answer_command(request: CommandRequest) -> str | BotReply:
        key = f"{request.context.chat_scope}:{request.context.chat_id}"
        current = guess_sessions.get(key)
        if current is None:
            return "当前会话没有正在进行的猜歌。发送「猜歌」开始。"
        answer = _normalize_text(request.argument)
        accepted = {
            str(current.song_id),
            _normalize_text(current.title),
            *(_normalize_text(item) for item in current.aliases),
        }
        if answer in accepted:
            guess_sessions.pop(key, None)
            return await guess_answer_reply(current, "答对啦！答案是")
        return "还不对，再想想看；也可以发送「提示」或「答案」。"

    @router.command("hint", "获取当前猜歌提示", aliases=("提示",), category="娱乐")
    async def hint_command(request: CommandRequest) -> str | BotReply:
        key = f"{request.context.chat_scope}:{request.context.chat_id}"
        current = guess_sessions.get(key)
        if current is None:
            return "当前没有猜歌题目。"
        if current.hint_index >= len(current.hints):
            return await _guess_hint_card(current)
        hint = current.hints[current.hint_index]
        current.hint_index += 1
        text = _format_guess_hints(current)
        if hint.cover_level is None:
            return await _guess_hint_card(current)
        try:
            image = await catalog.cover_clue(
                current.song_id,
                reveal_level=hint.cover_level,
            )
        except SongCatalogError as exc:
            logger.warning("progressive cover clue failed: %s", exc)
            return text + "\n曲绘扩大提示生成失败，本次仍保留文字提示。"
        return BotReply(text=text, image_png=image, followup_text=text)

    @router.command("reveal", "公布当前猜歌答案", aliases=("答案",), category="娱乐")
    async def reveal_command(request: CommandRequest) -> str | BotReply:
        key = f"{request.context.chat_scope}:{request.context.chat_id}"
        current = guess_sessions.pop(key, None)
        if current is None:
            return "当前没有猜歌题目。"
        return await guess_answer_reply(current, "答案是")

    @router.command(
        "listen-guess", "听歌猜曲（等待合法试听音源）", aliases=("听歌猜曲",), category="娱乐"
    )
    async def listen_guess_command(_: CommandRequest) -> str:
        return "听歌猜曲需要可合法分发的音频片段；当前公共曲库不提供试听音源，因此暂不发送来源不明的音乐。曲绘猜歌、谱面猜歌、Note 猜歌和首字母猜歌可正常使用。"

    @router.command("cosmetic", "选择 B50 头像和姓名框", aliases=("外观",), category="设置")
    async def cosmetic_command(request: CommandRequest) -> str | BotReply:
        try:
            parts = request.argument.strip().split(maxsplit=2)
            if not parts:
                icon, plate = await cosmetics.describe(request.context.user_id)
                return await result_card(
                    "我的 B50 外观", [
                        ("头像", f"{icon.name}（ID {icon.id}）" if icon else "QQ 头像"),
                        ("姓名框", f"{plate.name}（ID {plate.id}）" if plate else "自动匹配水鱼牌子"),
                    ], section="外观", note="在图鉴中点击收藏品可复制 ID，然后用按钮切换外观。",
                    actions=COSMETIC_QUICK_ACTIONS, followup=_cosmetic_help(icon, plate),
                )
            if (
                len(parts) == 3
                and parts[0].casefold() in _SEARCH_WORDS
                and parts[1].casefold() in _KIND_ALIASES
            ):
                kind = _KIND_ALIASES[parts[1].casefold()]
                items = await cosmetics.search(kind, parts[2])
                if items:
                    assets = await asyncio.gather(
                        *(cosmetics.asset(kind, item.id) for item in items),
                        return_exceptions=True,
                    )
                    entries = [
                        {
                            "song_id": 0,
                            "title": item.name,
                            "type": "DX",
                            "level_index": 3,
                            "level": "收藏品",
                            "ds": None,
                            "_image_png": asset if isinstance(asset, bytes) else None,
                            "_list_value": f"ID {item.id}",
                            "_list_detail": item.description or item.genre or _kind_label(kind),
                            "_list_extra": _kind_label(kind),
                        }
                        for item, asset in zip(items, assets, strict=True)
                    ]
                    return quick_reply(
                        await list_reply(
                            f"{_kind_label(kind)}搜索｜{parts[2]}",
                            entries,
                            _format_search(kind, items),
                            subtitle=f"发送 /cosmetic {_kind_label(kind)} ID 选择",
                        ),
                        COSMETIC_QUICK_ACTIONS,
                    )
            return quick_reply(
                await _handle_cosmetic_command(
                    cosmetics,
                    request.context.user_id,
                    request.argument,
                ),
                COSMETIC_QUICK_ACTIONS,
            )
        except CosmeticError as exc:
            logger.warning("cosmetic command failed: %s", exc)
            return str(exc)

    @router.command("about", "查看项目与数据来源说明", category="设置")
    async def about_command(_: CommandRequest) -> BotReply:
        return await academy_card(
            "关于藤泽柚子", [
                ("机器人接入", "QQ 官方开放平台"),
                ("舞萌成绩", "默认水鱼查分器。落雪查分器启用时，可用 /lxns 独立授权、/source 切换。"),
                ("曲目目录", "落雪的公开曲库提供歌曲、谱面和别名。"),
                ("对话服务", "中国科学技术大学大模型公共服务平台。"),
            ], section="帮助", followup=f"机器人主页 {HELP_SITE_URL}",
        )

    router.pattern(
        rf"^{PLATE_NAME_PATTERN}(?:完成表|进度)?$",
        "plate",
    )
    router.pattern(r"^([舞霸覇])进度$", "plate", lambda m: m.group(1))
    router.pattern(
        r"^([绿黄红紫白])\s*(?:id\s*)?(.+)$", "chart", lambda m: f"{m.group(1)} {m.group(2)}"
    )
    router.pattern(r"^(.+?)有什么别名[？?]?$", "aliases", lambda m: m.group(1))
    router.pattern(r"^随个" + _COMPACT_RANDOM + "$", "random", lambda m: m.group()[2:])
    router.pattern(r"^(.+?)是(?:什么|啥)歌[？?]?$", "which", lambda m: m.group(1))
    router.pattern(
        r"^(\d{1,2}\+?)(完成表|ap\+?|fc\+?|sss\+?)$",
        "level",
        lambda m: f"{m.group(1)} {m.group(2)}",
    )
    router.pattern(
        r"^(\d{1,2}(?:\+|\.\d)?)分表$",
        "scoretable",
        lambda m: m.group(1),
    )
    router.pattern(r"^(\d+(?:\.\d+)?)%?怎么打[？?]?$", "reverse", lambda m: m.group(1))
    router.pattern(
        r"^曲绘猜歌(?:\s+(.+))?$",
        "guess",
        lambda match: _mode_pattern_argument("cover", match.group(1)),
    )
    router.pattern(
        r"^谱面猜歌(?:\s+(.+))?$",
        "guess",
        lambda match: _mode_pattern_argument("chart", match.group(1)),
    )
    router.pattern(
        r"^(?:note音?|音符)猜歌(?:\s+(.+))?$",
        "guess",
        lambda match: _mode_pattern_argument("note", match.group(1)),
    )
    router.pattern(
        r"^开字母(?:猜歌)?(?:\s+(.+))?$",
        "guess",
        lambda match: _mode_pattern_argument("letter", match.group(1)),
    )
    if isinstance(scores, ScoreSources):
        register_source_commands(router, scores)
    if community is not None:
        register_community(router, community, scores, catalog, b50_images, cross_games)
        register_party_games(router, community)
    router.pattern(
        r"#(.{1,120})", "answer", lambda match: match[1],
        when=lambda ctx: f"{ctx.chat_scope}:{ctx.chat_id}" in guess_sessions,
    )
    register_sticker_commands(router, cosmetics)
    if cross_games is not None:
        register_cross_game(router, cross_games)
    if group_life is not None:
        register_group_commands(router, group_life)
    return router


_GUESS_MODES = {
    "cover": "cover",
    "曲绘": "cover",
    "chart": "chart",
    "谱面": "chart",
    "note": "note",
    "音符": "note",
    "letter": "letter",
    "字母": "letter",
}
_GUESS_DIFFICULTY_PATTERN = re.compile(
    r"^(?:绿|黄|红|紫|白|basic|advanced|expert|master|mas|remaster|re:master|remas)$",
    re.IGNORECASE,
)


def _parse_guess_request(argument: str) -> tuple[str, str]:
    tokens = argument.strip().split()
    if not tokens:
        return "normal", ""
    mode = _GUESS_MODES.get(tokens[0].casefold())
    if mode is None:
        return "normal", " ".join(tokens)
    return mode, " ".join(tokens[1:])


def _guess_chart_query(filters: str) -> str:
    words = filters.split()
    if any(_GUESS_DIFFICULTY_PATTERN.fullmatch(word) for word in words):
        return filters
    return f"master {filters}".strip()


def _mode_pattern_argument(mode: str, filters: str | None) -> str:
    return f"{mode} {filters or ''}".strip()


def _guess_help_text() -> str:
    return (
        "猜歌限定范围用法（条件可以叠加）：\n"
        "• 猜歌 14.0-14.5\n"
        "• 猜歌 桃-紫 DX\n"
        "• 猜歌 13+-14+ 白 SD\n"
        "• 曲绘猜歌 当前版本 14+\n"
        "• 谱面猜歌 宴\n"
        "• note猜歌 14.2 紫 DX\n"
        "• 开字母 橙代 MASTER\n\n"
        "可限定：精确定数/定数区间、等级/等级区间、版本/版本区间、"
        "当前版本、DX/SD、绿黄红紫白难度。未指定难度时默认 MASTER。"
    )


def _build_guess_hints(
    mode: str,
    chart: Chart,
    *,
    bpm: int,
    version_name: str,
    filters: str = "",
) -> tuple[GuessHint, ...]:
    title_chars = [character for character in chart.title if not character.isspace()]
    edge = (
        f"歌名有 {len(title_chars)} 个非空格字符，首尾是「{title_chars[0]}…{title_chars[-1]}」"
        if len(title_chars) > 1
        else f"歌名非常短，只有 {len(title_chars)} 个字符"
    )
    known = _guess_known_aspects(filters)
    known.update(
        {
            "normal": {"artist", "genre"},
            "chart": {"designer", "type", "level", "notes"},
            "note": {"notes"},
            "letter": {"initials"},
        }.get(mode, set())
    )
    clues = {
        "artist": f"这首歌的艺术家是 {chart.artist}。",
        "genre": f"它在游戏中的分类是「{chart.genre}」。",
        "version": f"它初次收录于 {version_name}。",
        "tempo_band": _tempo_band_hint(bpm),
        "bpm": f"更明确一点：歌曲 BPM 是 {bpm}。" if bpm else "歌曲 BPM 暂无数据。",
        "type": f"目标谱面属于{chart.type_label}谱面。",
        "level": f"目标谱面的游戏内等级是 {chart.level}。",
        "constant": f"目标谱面的定数是 {chart.constant:.1f}。",
        "designer": f"这张谱面的谱师是 {chart.designer or '未知'}。",
        "note_style": _note_style_hint(chart),
        "title_style": _title_style_hint(chart.title),
        "initials": f"标题轮廓：{_guess_title_initials(chart.title)}。",
        "edge": edge,
        "mask": f"最后给出部分歌名：{_masked_guess_title(chart.title)}",
    }
    tiers = (
        ["genre", "version", "tempo_band", "type", "title_style"],
        ["artist", "designer", "note_style", "bpm", "level", "constant"],
        ["initials", "edge", "mask"],
    )
    seed = int.from_bytes(
        hashlib.sha256(f"{chart.song_id}:{mode}:{filters}".encode()).digest()[:8],
        "big",
    )
    rng = random.Random(seed)
    selected: list[list[GuessHint]] = []
    for index, names in enumerate(tiers):
        available = [GuessHint(clues[name]) for name in names if name not in known]
        rng.shuffle(available)
        selected.append(available[:2] if index < 2 else available)
    weak, medium, strong = selected
    if mode == "cover":
        text_hints = weak + medium + strong
        progressive: list[GuessHint] = []
        for level in range(1, 5):
            progressive.append(
                GuessHint(
                    f"曲绘可见范围第 {level} 次扩大。",
                    cover_level=level,
                )
            )
            if text_hints:
                progressive.append(text_hints.pop(0))
        progressive.extend(text_hints)
        return tuple(progressive)
    return tuple(weak + medium + strong)


def _guess_known_aspects(filters: str) -> set[str]:
    known = {"difficulty"}
    for word in filters.casefold().split():
        if word in {"dx", "sd", "standard", "标准"}:
            known.add("type")
        if _GUESS_DIFFICULTY_PATTERN.fullmatch(word):
            known.add("difficulty")
        if word in {"新曲", "当前版本", "new"} or re.fullmatch(
            r"[初真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉熊華华爽煌宙星祭祝双宴镜鏡彩](?:代)?",
            word,
        ) or re.fullmatch(
            r"[初真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉熊華华爽煌宙星祭祝双宴镜鏡彩]"
            r"(?:-|~|～|至|到)"
            r"[初真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉熊華华爽煌宙星祭祝双宴镜鏡彩](?:代)?",
            word,
        ):
            known.add("version")
        if re.fullmatch(r"\d{1,2}\+?", word) or re.fullmatch(
            r"\d{1,2}\+?(?:-|~|～|至|到)\d{1,2}\+?",
            word,
        ):
            known.update(("level", "constant"))
        if re.fullmatch(r"\d+(?:\.\d+)", word) or re.fullmatch(
            r"\d+(?:\.\d+)?(?:-|~|～|至|到)\d+(?:\.\d+)?",
            word,
        ):
            known.update(("level", "constant"))
    return known


def _tempo_band_hint(bpm: int) -> str:
    if not bpm:
        return "这首歌的速度数据暂时未知。"
    if bpm < 120:
        feel = "偏舒缓"
    elif bpm < 160:
        feel = "中速"
    elif bpm < 200:
        feel = "偏快"
    else:
        feel = "高速"
    return f"从体感速度看，它属于{feel}曲目。"


def _note_style_hint(chart: Chart) -> str:
    total = max(chart.note_count, 1)
    traits: list[str] = []
    if chart.touch:
        traits.append("包含 TOUCH")
    if chart.break_count / total >= 0.025:
        traits.append("BREAK 比较显眼")
    if chart.slide / total >= 0.12:
        traits.append("SLIDE 占比较高")
    if chart.note_count >= 900:
        traits.append("整体物量很大")
    elif chart.note_count and chart.note_count <= 500:
        traits.append("整体物量不高")
    if not traits:
        traits.append("键型构成比较均衡")
    return "谱面手感线索：" + "、".join(traits[:2]) + "。"


def _title_style_hint(title: str) -> str:
    latin_words = re.findall(r"[A-Za-z0-9]+", title)
    cjk_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", title))
    if latin_words and not cjk_count:
        return f"歌名是英文/拉丁字母标题，共 {len(latin_words)} 个词。"
    if cjk_count and not latin_words:
        return "歌名主要由中文或日文字符组成。"
    return "歌名是拉丁字母与中日文混合标题。"


def _guess_title_initials(title: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+|[\u3040-\u30ff\u3400-\u9fff]", title)
    return " ".join(word[0].upper() for word in words) or title[0]


def _masked_guess_title(title: str) -> str:
    visible_positions = {
        index
        for index, character in enumerate(title)
        if not character.isspace() and (index % 3 == 0 or index == len(title) - 1)
    }
    return "".join(
        character
        if character.isspace() or not character.isalnum() or index in visible_positions
        else "□"
        for index, character in enumerate(title)
    )


def _format_guess_hints(session: GuessSession) -> str:
    lines = [
        f"增量提示 {session.hint_index}/{len(session.hints)}",
        f"题面：{session.initial_clue}",
    ]
    lines.extend(
        f"{index}. {hint.text}"
        for index, hint in enumerate(session.hints[: session.hint_index], start=1)
    )
    lines.append("提示会继续累计；发送「提示」获取下一条。")
    return "\n".join(lines)


async def _guess_hint_card(session: GuessSession) -> BotReply:
    rows = [("题面", session.initial_clue)]
    rows.extend(
        (f"线索 {i}", hint.text)
        for i, hint in enumerate(session.hints[:session.hint_index], start=1)
    )
    return await result_card(
        f"增量提示 {session.hint_index}/{len(session.hints)}", rows, section="娱乐",
        note="可发送「答案」结束本题。" if session.hint_index >= len(session.hints)
        else "提示会继续累计，发送「提示」获取下一条。",
    )


_KIND_ALIASES: dict[str, CosmeticKind] = {
    "头像": "icon",
    "icon": "icon",
    "avatar": "icon",
    "姓名框": "plate",
    "名牌": "plate",
    "plate": "plate",
}
_SEARCH_WORDS = {"搜索", "查找", "search"}
_RESET_WORDS = {"默认", "自动", "重置", "reset", "default", "auto", "qq"}


async def _handle_cosmetic_command(
    cosmetics: CosmeticService,
    user_id: str,
    argument: str,
) -> str:
    parts = argument.strip().split(maxsplit=2)
    if not parts:
        icon, plate = await cosmetics.describe(user_id)
        return _cosmetic_help(icon, plate)

    action = parts[0].casefold()
    if action in {"重置", "reset"} and len(parts) == 1:
        await cosmetics.reset(user_id)
        return "已恢复默认外观：使用 QQ 头像，姓名框自动匹配水鱼牌子。"

    if action in _SEARCH_WORDS:
        if len(parts) < 3 or parts[1].casefold() not in _KIND_ALIASES:
            return "用法：/cosmetic 搜索 头像|姓名框 关键词"
        kind = _KIND_ALIASES[parts[1].casefold()]
        return _format_search(kind, await cosmetics.search(kind, parts[2]))

    kind = _KIND_ALIASES.get(action)
    if kind is None or len(parts) < 2:
        return "用法：/cosmetic 头像|姓名框 ID；先用 /cosmetic 搜索 头像|姓名框 关键词"

    selector = " ".join(parts[1:]).strip()
    if selector.casefold() in _RESET_WORDS:
        await cosmetics.reset(user_id, kind)
        if kind == "icon":
            return "头像已恢复为 QQ 头像。"
        return "姓名框已恢复自动模式：优先匹配水鱼牌子，缺失时使用游戏默认姓名框。"

    if selector.isdecimal():
        chosen = await cosmetics.choose(user_id, kind, int(selector))
        return f"已选择{_kind_label(kind)}：{chosen.name}（ID {chosen.id}），下次 /b50 生效。"

    matches = await cosmetics.search(kind, selector)
    exact = [item for item in matches if item.name.casefold() == selector.casefold()]
    if len(exact) == 1:
        chosen = await cosmetics.choose(user_id, kind, exact[0].id)
        return f"已选择{_kind_label(kind)}：{chosen.name}（ID {chosen.id}），下次 /b50 生效。"
    return _format_search(kind, matches)


def _cosmetic_help(icon: CollectionItem | None, plate: CollectionItem | None) -> str:
    icon_text = f"{icon.name}（ID {icon.id}）" if icon else "QQ 头像（默认）"
    plate_text = (
        f"{plate.name}（ID {plate.id}）" if plate else "自动匹配水鱼牌子；没有时使用游戏默认姓名框"
    )
    return (
        f"当前 B50 外观：\n头像：{icon_text}\n姓名框：{plate_text}\n\n"
        f"网页图鉴（搜索并点击复制 ID）：\n{COSMETIC_SITE_URL}\n\n"
        "搜索：/cosmetic 搜索 头像 初音\n"
        "搜索：/cosmetic 搜索 姓名框 橙将\n"
        "选择：/cosmetic 头像 101\n"
        "选择：/cosmetic 姓名框 6113\n"
        "恢复：/cosmetic 头像 QQ 或 /cosmetic 姓名框 自动\n"
        "选择仅影响外观，不影响水鱼绑定和成绩。"
    )


def _format_search(kind: CosmeticKind, items: list[CollectionItem]) -> str:
    if not items:
        return f"没有找到匹配的{_kind_label(kind)}。请换一个关键词。"
    rows = [f"找到以下{_kind_label(kind)}（使用对应 ID 选择）："]
    rows.extend(
        f"{item.id} — {item.name}" + (f"〔{item.genre}〕" if item.genre else "") for item in items
    )
    rows.append(f"发送 /cosmetic {_kind_label(kind)} ID")
    return "\n".join(rows)


def _song_resources(
    confirmations: ChartConfirmationResult,
) -> tuple[str, tuple[QuickAction, ...]]:
    if not confirmations.entries:
        return "", ()
    preview_rows = [f"{confirmations.song_title}｜AWMC 在线谱面预览："]
    actions: list[QuickAction] = []
    for entry in confirmations.entries:
        chart = entry.chart
        short_label = ("紫" if chart.difficulty == 3 else "白") + chart.type_label
        if entry.video:
            actions.append(
                QuickAction(
                    f"🎬 {short_label}确认",
                    url=entry.video.direct_url,
                )
            )
        preview_url = "https://v.awmc.cc/preview?" + urlencode(
            {
                "song": chart.song_id,
                "kind": chart.chart_type,
                "diff": chart.difficulty + 2,
            }
        )
        preview_rows.extend((chart_label(chart), preview_url))
        actions.append(QuickAction(f"▶️ {short_label}预览", url=preview_url))
    text_parts = [confirmations.as_text(), "\n".join(preview_rows)]
    return "\n\n".join(part for part in text_parts if part), tuple(actions)


def _kind_label(kind: CosmeticKind) -> str:
    return "头像" if kind == "icon" else "姓名框"


async def _b50_analysis_card(result) -> BotReply:
    records = [*result.old[:35], *result.new[:15]]
    old_bottom = min((int(item.get("ra", 0) or 0) for item in result.old[:35]), default=0)
    new_bottom = min((int(item.get("ra", 0) or 0) for item in result.new[:15]), default=0)
    avg_achievement = sum(float(item.get("achievements", 0) or 0) for item in records) / max(
        len(records), 1
    )
    ap = sum(str(item.get("fc") or "").casefold() in {"ap", "app"} for item in records)
    fc = sum(str(item.get("fc") or "").casefold() in {"fc", "fcp", "ap", "app"} for item in records)
    sssp = sum(str(item.get("rate") or "").casefold() == "sssp" for item in records)
    return await result_card(
        f"{result.nickname} 的 B50 分析",
        [
            ("旧版本 B35", f"{sum(int(x.get('ra', 0) or 0) for x in result.old[:35])} 分 · 底分 {old_bottom}"),
            ("当前版本 B15", f"{sum(int(x.get('ra', 0) or 0) for x in result.new[:15])} 分 · 底分 {new_bottom}"),
            ("平均达成率", f"{avg_achievement:.4f}%"),
            ("SSS+ / FC / AP", f"{sssp} / {fc} / {ap} 张"),
        ],
        section="成绩", metric=("DX RATING", str(result.rating)),
        note=f"统计范围为上榜的 {len(records)} 张谱面，FC、AP 均含更高标记。",
    )


async def _threshold_card(result, constant: float) -> str | BotReply:
    if constant <= 0:
        return "谱面定数必须大于 0。"
    old_bottom = min((int(item.get("ra", 0) or 0) for item in result.old[:35]), default=0)
    new_bottom = min((int(item.get("ra", 0) or 0) for item in result.new[:15]), default=0)
    rows = []
    for label, bottom in (("B35 旧版本", old_bottom), ("B15 当前版本", new_bottom)):
        needed = minimum_achievement(constant, bottom + 1)
        if needed is None:
            rows.append((label, f"即使 100.5000% 也无法达到目标单谱 RA {bottom + 1}"))
        else:
            rows.append((label, f"至少 {needed:.4f}%（目标单谱 RA {bottom + 1}）"))
    return await result_card(
        f"定数 {constant:.1f} 的进榜线", rows, section="成绩",
        note="按超过当前底分 1 点计算。B35、B15 归属取决于曲目版本。相同 RA 的排序还取决于定数、达成率和歌曲 ID。",
    )


async def _chat_score_context(
    scores: DivingFishService,
    catalog: SongCatalogService,
    cache: dict[str, tuple[float, PlayerRecords]],
    user_id: str,
    message: str,
    *,
    cache_key: str | None = None,
) -> str:
    key = user_id if cache_key is None else cache_key
    cached = cache.get(key)
    if (cached and time.monotonic() - cached[0] < 300
            and (not isinstance(scores, DivingFishService) or scores.has_current_token(user_id))):
        records = cached[1]
    else:
        try:
            records = await scores.player_records(user_id)
        except NotBound:
            return "该用户尚未绑定水鱼，因此当前没有可读取的成绩数据。"
        except ScoreServiceError as exc:
            logger.info("聊天成绩上下文暂不可用：%s", exc)
            return "水鱼成绩本次暂时读取失败；不要假装看到了具体成绩。"
        cache[key] = (time.monotonic(), records)

    detailed = _asks_about_scores(message)
    fit_note = ""
    if detailed:
        try:
            songs = await catalog.songs()
            records, matched = _attach_fit_constants(records, songs)
            fit_note = (
                "\n【数据口径】官方定数来自成绩记录；水鱼拟合定数来自 chart_stats.fit_diff。"
                f"本次成功匹配 {matched} 张已有成绩。回答拟合定数问题时必须使用“水鱼拟合定数”字段，"
                "不得用官方定数代替。"
            )
        except SongCatalogError as exc:
            logger.info("聊天拟合定数上下文暂不可用：%s", exc)
            fit_note = "\n水鱼拟合定数目录本次读取失败；请明确说明无法核实，不要拿官方定数代替。"

    snapshot = _score_snapshot(records, detailed=detailed)
    return snapshot + _related_scores(records, message) + fit_note


def _attach_fit_constants(
    records: PlayerRecords,
    songs: tuple[Song, ...],
) -> tuple[PlayerRecords, int]:
    by_id: dict[tuple[int, str, int], float] = {}
    by_title: dict[tuple[str, str, int], float] = {}
    for song in songs:
        for chart in song.charts:
            if chart.fit_constant is None:
                continue
            by_id[(song.id, chart.chart_type, chart.difficulty)] = chart.fit_constant
            by_title[(_normalize_text(song.title), chart.chart_type, chart.difficulty)] = (
                chart.fit_constant
            )

    matched = 0

    def enrich(item: dict) -> dict:
        nonlocal matched
        copied = dict(item)
        try:
            difficulty = int(item.get("level_index", -1))
        except (TypeError, ValueError):
            difficulty = -1
        chart_type = _record_type(item)
        value = by_id.get((_normalized_song_id(item), chart_type, difficulty))
        if value is None:
            value = by_title.get(
                (_normalize_text(str(item.get("title") or "")), chart_type, difficulty)
            )
        if value is not None:
            copied["_fit_constant"] = value
            matched += 1
        return copied

    return (
        PlayerRecords(
            nickname=records.nickname,
            rating=records.rating,
            additional_rating=records.additional_rating,
            plate=records.plate,
            old=[enrich(item) for item in records.old],
            new=[enrich(item) for item in records.new],
        ),
        matched,
    )


def _score_snapshot(records: PlayerRecords, *, detailed: bool) -> str:
    best = records.best50()
    all_scores = [*records.old, *records.new]
    old_total = sum(int(item.get("ra", 0) or 0) for item in best.old)
    new_total = sum(int(item.get("ra", 0) or 0) for item in best.new)
    ap_count = sum(str(item.get("fc") or "").casefold() in {"ap", "app"} for item in all_scores)
    fc_count = sum(
        str(item.get("fc") or "").casefold() in {"fc", "fcp", "ap", "app"} for item in all_scores
    )
    rows = [
        "【水鱼成绩摘要，缓存最多 5 分钟】",
        f"玩家：{records.nickname}；DX Rating：{records.rating}；牌子：{records.plate}",
        f"已记录谱面：{len(all_scores)}；FC及以上：{fc_count}；AP及以上：{ap_count}",
        f"B35合计：{old_total}；B15合计：{new_total}",
    ]
    if detailed:
        rows.append("当前 B50：")
        rows.extend(_score_line(item, index + 1, "B35") for index, item in enumerate(best.old))
        rows.extend(_score_line(item, index + 1, "B15") for index, item in enumerate(best.new))
    return "\n".join(rows)


def _related_scores(records: PlayerRecords, message: str) -> str:
    lowered = message.casefold()
    best = records.best50()
    if "b50" in lowered:
        source_records = [*best.old, *best.new]
    elif "b35" in lowered:
        source_records = best.old
    elif "b15" in lowered:
        source_records = best.new
    else:
        source_records = [*records.old, *records.new]
    normalized = _normalize_text(message)
    words = [_normalize_text(word) for word in re.findall(r"[\w+：:!！?？.-]+", message)]
    words = [word for word in words if len(word) >= 2]
    # Do not mistake the "50" in B50/R50/AP50 for a chart level.
    levels = set(re.findall(r"(?<![\dA-Za-z])(\d{1,2}\+?)(?![\d.])", message))
    desired_rate = _mentioned_rate(message)
    fc_filter = "ap" if "ap" in message.casefold() else "fc" if "fc" in message.casefold() else ""
    negative = any(word in message.casefold() for word in ("没", "未", "没有", "不到"))
    matched = []
    for item in source_records:
        title = _normalize_text(str(item.get("title") or ""))
        title_match = title and (title in normalized or any(word in title for word in words))
        structured = bool(levels or desired_rate or fc_filter)
        condition = True
        if levels:
            condition = str(item.get("level") or "") in levels
        if desired_rate:
            rate_match = str(item.get("rate") or "").casefold() == desired_rate
            condition = condition and (not rate_match if negative else rate_match)
        if fc_filter:
            fc = str(item.get("fc") or "").casefold()
            values = {"ap", "app"} if fc_filter == "ap" else {"fc", "fcp", "ap", "app"}
            condition = condition and (fc not in values if negative else fc in values)
        if title_match or (structured and condition):
            matched.append(item)
    if not matched:
        return ""
    ordering = ""
    if "拟合" in message and any(word in message for word in ("最高", "最大", "最难")):
        matched.sort(
            key=lambda item: (
                item.get("_fit_constant") is None,
                -float(item.get("_fit_constant") or 0),
            )
        )
        ordering = "（已按水鱼拟合定数从高到低）"
    elif "拟合" in message and any(word in message for word in ("最低", "最小", "最简单")):
        matched.sort(
            key=lambda item: (
                item.get("_fit_constant") is None,
                float(item.get("_fit_constant") or 0),
            )
        )
        ordering = "（已按水鱼拟合定数从低到高）"
    rows = [f"\n与本次问题可能相关的水鱼成绩{ordering}："]
    rows.extend(_score_line(item, index + 1, "相关") for index, item in enumerate(matched[:20]))
    return "\n".join(rows)


def _score_line(item: dict, index: int, group: str) -> str:
    fit_value = item.get("_fit_constant")
    try:
        fit_text = f"{float(fit_value):.2f}" if fit_value is not None else "—"
    except (TypeError, ValueError):
        fit_text = "—"
    return (
        f"{group}#{index} {item.get('title', '未知')} "
        f"[{item.get('type', '?')} {item.get('level_label', '?')} {item.get('level', '?')}] "
        f"官方定数{item.get('ds', '?')} 水鱼拟合定数{fit_text} "
        f"{float(item.get('achievements', 0) or 0):.4f}% "
        f"RA{int(item.get('ra', 0) or 0)} {item.get('rate', '')} "
        f"{item.get('fc') or '-'} {item.get('fs') or '-'}"
    )


def _record_sort_key(item: dict) -> tuple[float, float, float, int]:
    return (
        -float(item.get("ra", 0) or 0),
        -float(item.get("ds", 0) or 0),
        -float(item.get("achievements", 0) or 0),
        _normalized_song_id(item),
    )


def _ap_records(records: PlayerRecords) -> list[dict]:
    selected = [
        item
        for item in all_records(records)
        if str(item.get("fc") or "").casefold() in {"ap", "app"}
    ]
    selected.sort(key=_record_sort_key)
    return selected


def _records_for_song(records: PlayerRecords, song) -> list[dict]:
    selected = [
        item
        for item in all_records(records)
        if _normalized_song_id(item) == song.id
        or str(item.get("title") or "").casefold() == song.title.casefold()
    ]
    selected.sort(key=lambda item: (int(item.get("level_index", 0)), _record_type(item)))
    return selected


def _filter_score_records(records: PlayerRecords, condition: str) -> list[dict] | None:
    words = condition.casefold().split()
    if not words:
        return None
    selected = all_records(records)
    understood = False
    for word in words:
        if re.fullmatch(r"\d{1,2}\+?", word):
            selected = [item for item in selected if str(item.get("level") or "") == word]
            understood = True
        elif word in {"dx", "sd", "standard", "标准"}:
            target = "dx" if word == "dx" else "standard"
            selected = [item for item in selected if _record_type(item) == target]
            understood = True
        elif word in DIFFICULTY_MARKS:
            target = DIFFICULTY_MARKS[word]
            selected = [item for item in selected if int(item.get("level_index", -1)) == target]
            understood = True
        elif word in {"fc", "fc+", "fcp", "ap", "ap+", "app"}:
            target = word.replace("+", "p")
            selected = [
                item
                for item in selected
                if FC_ORDER.get(str(item.get("fc") or "").casefold(), 0) >= FC_ORDER[target]
            ]
            understood = True
        elif word in {"s", "s+", "ss", "ss+", "sss", "sss+"}:
            threshold = {"s": 97, "s+": 98, "ss": 99, "ss+": 99.5, "sss": 100, "sss+": 100.5}[word]
            selected = [
                item for item in selected if float(item.get("achievements", 0) or 0) >= threshold
            ]
            understood = True
    if not understood:
        return None
    selected.sort(key=_record_sort_key)
    return selected


def _song_list_record(song) -> dict:
    chart = max(song.charts, key=lambda item: (item.difficulty, item.constant), default=None)
    if chart is None:
        return {
            "song_id": song.id,
            "title": song.title,
            "type": "DX",
            "level_index": 3,
            "level": "?",
            "ds": None,
            "_list_value": song.artist or "未知艺术家",
            "_list_detail": f"歌曲 ID {song.id} · {song.genre or '未知分类'}",
        }
    result = _chart_list_record(chart)
    result["_list_value"] = song.artist or "未知艺术家"
    result["_list_detail"] = (
        f"歌曲 ID {song.id} · 最高难度 {chart.difficulty_name} {chart.level} · {song.genre}"
    )
    return result


def _chart_list_record(chart) -> dict:
    return {
        "song_id": chart.song_id,
        "title": chart.title,
        "type": chart.type_label,
        "level_index": chart.difficulty,
        "level": chart.level,
        "ds": chart.constant,
        "_list_value": f"{chart.difficulty_name} · Lv.{chart.level}",
        "_list_detail": (
            f"定数 {chart.constant:.1f} · ID {chart.song_id} · 谱师 {chart.designer or '未知'}"
        ),
    }


def _level_unfinished_records(
    records: PlayerRecords,
    charts: list,
    level: str,
    goal: str,
) -> tuple[list[dict], int, int]:
    selected_records = [
        item for item in all_records(records) if str(item.get("level") or "") == level
    ]
    record_map = {
        (_normalized_song_id(item), _record_type(item), int(item.get("level_index", -1))): item
        for item in selected_records
    }
    eligible = [chart for chart in charts if chart.level == level]
    unfinished: list[dict] = []
    completed = 0
    for chart in eligible:
        item = record_map.get((chart.song_id, chart.chart_type, chart.difficulty))
        if item is not None and _meets_level_goal(item, goal):
            completed += 1
            continue
        rendered = dict(item) if item is not None else _chart_list_record(chart)
        rendered.setdefault("song_id", chart.song_id)
        rendered.setdefault("title", chart.title)
        rendered.setdefault("type", chart.type_label)
        rendered.setdefault("level_index", chart.difficulty)
        rendered.setdefault("level", chart.level)
        rendered.setdefault("ds", chart.constant)
        rendered.setdefault("achievements", 0.0)
        rendered["_list_detail"] = (
            f"{chart.difficulty_name} · 定数 {chart.constant:.1f} · ID {chart.song_id} · "
            f"谱师 {chart.designer or '未知'}"
        )
        unfinished.append(rendered)
    unfinished.sort(
        key=lambda item: (
            -float(item.get("ds", 0) or 0),
            -int(item.get("level_index", 0) or 0),
            -float(item.get("achievements", 0) or 0),
            _normalized_song_id(item),
        )
    )
    return unfinished, completed, len(eligible)


def _meets_level_goal(item: dict, goal: str) -> bool:
    normalized = goal.casefold().replace("完成表", "游玩").replace("+", "p")
    achievement = float(item.get("achievements", 0) or 0)
    fc = str(item.get("fc") or "").casefold()
    fs = str(item.get("fs") or "").casefold()
    if normalized in {"", "游玩"}:
        return achievement > 0
    if normalized in {"将", "sss"}:
        return achievement >= 100
    if normalized == "sssp":
        return achievement >= 100.5
    if normalized in {"fc", "fcp", "ap", "app", "极"}:
        target = "fc" if normalized == "极" else normalized
        return FC_ORDER.get(fc, 0) >= FC_ORDER[target]
    if normalized == "神":
        return FC_ORDER.get(fc, 0) >= FC_ORDER["ap"]
    if normalized in {"舞", "舞舞", "fdx", "fdxp"}:
        return FS_ORDER.get(fs, 0) >= FS_ORDER["fsd"]
    return False


def _normalized_song_id(item: dict) -> int:
    try:
        value = int(item.get("song_id", item.get("id", -1)))
    except (TypeError, ValueError):
        return -1
    return value - 10000 if 10000 < value < 20000 else value


def _record_type(item: dict) -> str:
    value = str(item.get("type") or "").casefold()
    return "standard" if value in {"sd", "standard", "标准"} else value


def _score_table_sort_key(item: dict) -> tuple[float, float, int]:
    try:
        song_id = int(item.get("song_id", 0) or 0)
    except (TypeError, ValueError):
        song_id = 0
    return (
        -float(item.get("achievements", 0) or 0),
        -float(item.get("ds", 0) or 0),
        song_id,
    )


def _normalize_text(value: str) -> str:
    return "".join(value.casefold().split())


def _asks_about_scores(message: str) -> bool:
    lowered = message.casefold()
    keywords = (
        "b50",
        "b35",
        "b15",
        "rating",
        "ra",
        "底分",
        "成绩",
        "分数",
        "达成率",
        "定数",
        "谱面",
        "练什么",
        "推荐曲",
        "分析",
        "提升",
        "短板",
        "sss",
        "ss+",
        "fc",
        "ap",
    )
    return any(keyword in lowered for keyword in keywords) or bool(
        re.search(r"(?<!\d)\d{1,2}\+?(?![\d.])", message)
    )


def _mentioned_rate(message: str) -> str:
    lowered = message.casefold()
    for label, value in (
        ("鸟加", "sssp"),
        ("鸟+", "sssp"),
        ("鸟", "sss"),
        ("sss+", "sssp"),
        ("sss", "sss"),
        ("ss+", "ssp"),
        ("ss", "ss"),
        ("s+", "sp"),
    ):
        if label in lowered:
            return value
    return ""


async def _get_records(
    scores: DivingFishService,
    request: CommandRequest,
) -> tuple[PlayerRecords | None, str]:
    try:
        return await scores.player_records(score_identity(scores, request.context)), ""
    except NotBound:
        return None, "你还没有授权查分。请先发送 /bind。"
    except ScoreServiceError as exc:
        logger.warning("score query failed: %s", exc)
        return None, str(exc)
