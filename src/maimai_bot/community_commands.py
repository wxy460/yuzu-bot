"""Public maimai lookups and local player/group features."""

from __future__ import annotations

import math
import random
import re
import shlex
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .course_area import register_course_area
from .domain import BotReply, QuickAction
from .services.community import CN, PublicDataError
from .services.divingfish import NotBound, ScoreServiceError
from .services.lxns_scores import ScoreSources, score_identity
from .services.result_card import academy_card, result_card
from .services.song_catalog import SongCatalogError
from .social import register_social


def _page(argument):
    query, _, tail = argument.rpartition(" ")
    if query and re.fullmatch(r"p[1-9]\d{0,4}", tail, re.IGNORECASE):
        return query.strip(), int(tail[1:])
    return argument.strip(), 1


async def _paged(title, rows, page, source):
    if not rows:
        return "没有找到匹配项。"
    pages = (len(rows) + 11) // 12
    if page > pages:
        return f"共 {pages} 页。"
    return await result_card(
        title,
        rows[(page - 1) * 12 : page * 12],
        section="查询",
        note=f"{source} · 共 {len(rows)} 项 · 第 {page}/{pages} 页。命令末尾加 p2 可翻页。",
    )


def legacy_ra(record):
    rate = min(100.5, float(record.get("achievements", 0)))
    bands = [
        (100.5, 15),
        (100, 14),
        (99.99, 13.5),
        (99.5, 13),
        (99, 12),
        (98, 11),
        (97, 10),
        (94, 9.4),
        (90, 9),
        (80, 8),
        (75, 7.5),
        (70, 7),
        (60, 6),
        (50, 5),
    ]
    return math.floor(
        float(record.get("ds", 0))
        * (rate / 100)
        * next((factor for limit, factor in bands if rate >= limit), 6)
    )


def register_community(router, community, scores, catalog, images, cross_games=None):
    def command(name, description, *, aliases=(), category="舞萌工具", usage=""):
        def register(handler):
            async def run(request):
                try:
                    return await handler(request)
                except NotBound:
                    return "请先发送 /bind 授权水鱼账号。"
                except (PublicDataError, ScoreServiceError, SongCatalogError) as exc:
                    return str(exc)
                except httpx.HTTPError:
                    return "数据源暂时无法连接，请稍后重试。"
                except sqlite3.Error:
                    return "暂时读写不了本地记录，请稍后重试。"

            return router.command(
                name, description, aliases=aliases, category=category, usage=usage
            )(run)

        return register

    @command(
        "tsearch", "按名称、编号或获取条件查称号", aliases=("称号查询",), usage="/tsearch muse"
    )
    async def trophies(req):
        q, p = _page(req.argument)
        if not q:
            return "用法：/tsearch 称号或条件关键词 [p2]"
        rows = [
            (
                str(v["id"]),
                f"{v['name']} · {v.get('color', '')}\n{v.get('description') or '尚无获取条件'}",
            )
            for v in await community.fetch("titles")
            if q.casefold() in (str(v["name"]) + " " + str(v.get("description") or "")).casefold()
            or q == str(v["id"])
        ]
        return await _paged("舞萌称号", rows, p, "落雪公开图鉴")

    register_course_area(command, community, scores, catalog)

    @command("maijacket", "提取单曲曲绘与可复制标题", aliases=("曲绘",), usage="/maijacket 834")
    async def jacket(req):
        song = await catalog.resolve(re.sub(r"^m(?=\d+$)", "", req.argument, flags=re.IGNORECASE))
        if not song:
            return "请先用 /song 确认歌曲 ID。"
        image = await images.song_jacket(song.id)
        if not image:
            return f"{song.title}（ID {song.id}）的曲绘暂时取不到。"
        return BotReply(text=song.title, image_png=image, followup_text=song.title)

    @command(
        "sjcj", "从自己的记录里随机抽成绩", aliases=("随机成绩",), usage="/sjcj [牛逼|丢人] [1–10]"
    )
    async def random_score(req):
        m = re.fullmatch(r"(牛逼|丢人)?\s*(10|[1-9])?", req.argument)
        if not m:
            return "用法：/sjcj [牛逼|丢人] [1–10]"
        player = await scores.player_records(score_identity(scores, req.context))
        pool = sorted(player.old + player.new, key=lambda v: float(v.get("achievements", 0)))
        if m[1]:
            size = max(1, math.ceil(len(pool) / 4))
            pool = pool[-size:] if m[1] == "牛逼" else pool[:size]
        if not pool:
            return "还没有可抽取的个人成绩。"
        selected = random.sample(pool, min(int(m[2] or 1), len(pool)))
        text = f"{player.nickname} 的随机成绩"
        return BotReply(
            text=text
            + "\n"
            + "\n".join(f"{v['title']} {v.get('achievements', 0)}%" for v in selected),
            image_png=await images.render_record_list(
                text,
                selected,
                subtitle="按达成率抽样 · "
                + ({"牛逼": "最高四分之一", "丢人": "最低四分之一"}.get(m[1], "全量成绩")),
            ),
        )

    @command("b40", "旧制 B25 与新曲 B15 的 Rating 折算", aliases=("maib40",), category="成绩")
    async def best40(req):
        player = await scores.player_records(score_identity(scores, req.context))

        def top(records, n, label):
            items = sorted(
                records, key=lambda v: (legacy_ra(v), v.get("achievements", 0)), reverse=True
            )[:n]
            return [dict(v, _list_extra=f"{label} · RA {legacy_ra(v)}") for v in items]

        rows = top(player.old, 25, "B25") + top(player.new, 15, "B15")
        title = f"{player.nickname} 的 B40"
        summary = f"现曲库按旧制折算 · 曲目分 {sum(map(legacy_ra, rows))} · 不含历史段位加分"
        return BotReply(
            text=title + "\n" + summary,
            image_png=await images.render_record_list(title, rows, subtitle=summary),
        )

    @command("trend", "查看已记录的 Rating 和进榜底分", aliases=("底分趋势",), category="成绩")
    async def trend(req):
        if req.argument in {"中二", "chuni"} and cross_games:
            try:
                history = cross_games.trend_key(req.context)
                best = await cross_games.best(req.context)
            except NotBound:
                return "请先发送 /chubind 绑定水鱼中二成绩。"
            records = best["records"]
            old = records.get("b30", records.get("best", []))[:30]
            new = records.get("n20", [])

            def floor(items, size):
                return min(r["ra"] for r in items) if len(items) == size else 0

            data = community.snapshot(
                history, best["rating"], floor(old, 30), floor(new, 20)
            )
            return await trend_image(data, f"{cross_games.source(req.context)}中二底分趋势", ("B30", "N20"), school=True)
        if req.argument:
            return "用法：/trend 或 /trend 中二"
        key = (
            scores.history_key(req.context)
            if isinstance(scores, ScoreSources)
            else req.context.user_id
        )
        best = await scores.best50(score_identity(scores, req.context))
        data = community.trend(key, best)
        return await trend_image(data, "舞萌底分趋势", ("B35", "B15"))

    @command("舞萌定数表", "查询国服或日服指定等级的全部谱面", usage="/舞萌定数表 [日服] 14+ [p2]")
    async def levels(req):
        q, p = _page(req.argument)
        region = "日服" if q.startswith("日服") else "国服"
        lv = q.removeprefix(region).strip()
        if not re.fullmatch(r"(?:[1-9]|1[0-5])\+?", lv):
            return "用法：/舞萌定数表 14+ 或 /舞萌定数表 日服 14+，末尾加 p2 翻页。"
        charts = []
        if region == "日服":
            for song in await community.fetch("mai_jp"):
                for prefix, kind in (("lev_", "SD"), ("dx_lev_", "DX")):
                    for diff in ("bas", "adv", "exp", "mas", "remas"):
                        key = prefix + diff
                        if song.get(key) == lv:
                            charts.append(
                                (
                                    str(song.get(key + "_i") or "未知"),
                                    f"{song['title']} · {kind} {diff.upper()}",
                                )
                            )
        else:
            charts = [
                (f"{c.constant:.1f}", f"{s.title} · {c.type_label} {c.difficulty_name}")
                for s in await catalog.songs()
                for c in s.charts
                if c.level == lv
            ]
        charts.sort(
            key=lambda x: (-float(x[0]) if re.fullmatch(r"\d+(?:\.\d+)?", x[0]) else 0, x[1])
        )
        return await _paged(
            f"{region} Lv.{lv} 定数表",
            charts,
            p,
            "otoge-db" if region == "日服" else "落雪国服曲库",
        )

    @command("search", "同时搜索舞萌和中二乐曲", usage="/search amazing")
    async def search(req):
        if not req.argument:
            return "用法：/search 歌名或别名"
        mai = await catalog.search(req.argument)
        rows = [(f"m{s.id}", s.title) for s in mai]
        rows.extend(
            (f"u{song['id']}", song["title"]) for song in (await catalog.utage(req.argument))[:10]
        )
        if cross_games:
            chu = await cross_games.chuni_search(req.argument, req.context)
            rows.extend((f"z{s['id']}", s["title"]) for s in chu[:10])
        return await academy_card(
            "跨音游查歌",
            rows or [("曲库", "没有找到匹配曲目")],
            section="曲目索引",
            note="每个曲库最多展示 10 首，发送 /id 加编号查看详情",
        )

    @command("id", "按游戏与编号查看完整曲目资料", usage="/id m834 或 /id z317")
    async def by_id(req):
        m = re.fullmatch(r"([mzu]?)(\d{1,7})", req.argument.casefold())
        if not m:
            return "用法：/id m834（舞萌）、/id u100018（宴会场）或 /id z317（中二）"
        if m[1] == "u":
            found = await catalog.utage(m[2])
            if not found:
                return "没有找到这个宴会场 ID，可发送 /search 曲名 查询。"
            song = found[0]
            charts = song["difficulties"]["utage"]
            return await result_card(
                song["title"],
                [
                    ("曲师", song["artist"]),
                    *[
                        (
                            f"{chart.get('kanji', '宴')} · Lv.{chart['level']}",
                            chart.get("description") or "宴会场",
                        )
                        for chart in charts
                    ],
                ],
                section="舞萌宴会场",
                note=f"落雪国服曲库 · u{song['id']}",
            )
        if m[1] == "z":
            if cross_games is None:
                return "中二曲库暂时不可用。"
            found = await cross_games.chuni_search(m[2], req.context)
            if not found:
                return "没有找到这个中二曲目编号。"
            s = found[0]
            return await academy_card(
                s["title"],
                [
                    (n, f"Lv.{lv} · 定数 {ds}")
                    for n, lv, ds in zip(
                        ("Basic", "Advanced", "Expert", "Master", "Ultima", "World's End"),
                        s["level"],
                        s["ds"],
                    )
                    if lv and ds is not None
                ],
                section="中二节奏",
                note=f"{cross_games.source(req.context)}中二曲库 · z{s['id']}",
            )
        from dataclasses import replace

        return await router.dispatch(replace(req.context, content="/song " + m[2], image_urls=()))

    @command("上传成绩", "打开查分器的成绩同步入口", aliases=("同步成绩",), category="成绩")
    async def upload(req):
        providers = {
            "水鱼": "https://maimai.diving-fish.com/manual/docs/update-records/proxy/",
            "落雪": "https://maimai.lxns.net/docs/sync",
        }
        if req.argument and req.argument not in providers:
            return "用法：/上传成绩 [水鱼或落雪]"
        if req.argument:
            providers = {req.argument: providers[req.argument]}
        return await result_card(
            "同步查分器成绩",
            [(name, "点下方入口，按查分器页面提示完成同步。") for name in providers],
            section="成绩",
            note="在查分器完成授权后，可回来查询成绩。",
            followup="\n".join(f"{name}同步教程  {url}" for name, url in providers.items()),
            actions=tuple(
                QuickAction(f"{name}同步教程", url=url) for name, url in providers.items()
            ),
        )

    @command("mai", "查看舞萌服务器监测", aliases=("舞萌状态",), usage="/mai ping")
    async def status(req):
        if req.argument.casefold() not in ("", "ping"):
            return "用法：/mai ping"
        v = await community.fetch("status")
        rows = [
            (
                x["key"].upper(),
                str(x.get("state_text") or "未知")
                + (f" · {x['latency']} ms" if isinstance(x.get("latency"), (int, float)) else ""),
            )
            for x in v.get("services", [])
        ]
        return await result_card(
            "舞萌服务器",
            rows,
            metric=("社区监测", str(v.get("verdictText") or "暂无结论")),
            note=f"查询于 {datetime.now(CN):%m-%d %H:%M} · {v.get('incidentSummary') or '华立服务器死了吗？'}",
            followup="https://mai.chongxi.us/",
        )

    @command(
        "roll", "替你选一项或掷骰", aliases=("帮我选",), category="娱乐", usage="/roll 打舞萌 吃饭"
    )
    async def roll(req):
        try:
            choices = shlex.split(req.argument)
        except ValueError:
            return "选项之间用空格隔开，带空格的选项可用成对引号。"
        if not choices or (len(choices) == 1 and choices[0].isascii() and choices[0].isdigit()):
            if choices and (len(choices[0]) > 9 or int(choices[0]) < 1):
                return "骰子的上限请输入 1 到 999999999 之间的整数。"
            limit = int(choices[0]) if choices else 100
            return await academy_card(
                "掷骰", [], metric=(f"1–{limit}", str(random.randint(1, limit))), section="娱乐"
            )
        return await academy_card(
            "柚子帮你选", [], metric=("今天选", random.choice(choices)), section="娱乐"
        )

    @command("time", "查询城市当前时间", aliases=("世界时间",), usage="/time 东京")
    async def world_time(req):
        zones = {
            "中国": "Asia/Shanghai",
            "北京": "Asia/Shanghai",
            "东京": "Asia/Tokyo",
            "日本": "Asia/Tokyo",
            "伦敦": "Europe/London",
            "纽约": "America/New_York",
            "巴黎": "Europe/Paris",
            "多伦多": "America/Toronto",
            "温哥华": "America/Vancouver",
            "悉尼": "Australia/Sydney",
            "首尔": "Asia/Seoul",
            "上海": "Asia/Shanghai",
        }
        if req.argument == "加拿大":
            rows = [
                (city, datetime.now(ZoneInfo(zones[city])).strftime("%m-%d %H:%M %Z"))
                for city in ("多伦多", "温哥华")
            ]
            return await academy_card("加拿大时间", rows, section="世界时钟")
        name = zones.get(req.argument, req.argument or "Asia/Shanghai")
        try:
            stamp = datetime.now(ZoneInfo(name))
        except (ValueError, ZoneInfoNotFoundError):
            return "请输入城市名或 IANA 时区，例如 /time 东京、/time America/Chicago。"
        return await academy_card(
            req.argument or "北京时间",
            [("日期", str(stamp.date())), ("时区", name)],
            section="世界时钟", metric=("当地时间", stamp.strftime("%H:%M %Z")),
        )

    router.pattern(r"^#查询(.{1,50})时间$", "time", lambda m: m[1])
    register_social(command, community, catalog)


async def trend_image(data, title, names, *, school=False):
    import io

    from PIL import Image, ImageDraw

    from .services.academy import INK, MUTED, PAPER, draw_header
    from .services.b50_image import _font

    def draw_graph():
        image = Image.new("RGB", (900, 700 if school else 650), PAPER if school else "#f4f1fa")
        pen = ImageDraw.Draw(image)
        if school:
            draw_header(image, title, "中二节奏", 0.85)
        else:
            pen.rectangle((0, 0, 900, 112), fill="#282341")
            pen.text((36, 24), title, font=_font(40, bold=True), fill="white")
        offset = 48 if school else 0
        for index, label in enumerate(("Rating", *names), 1):
            top = 128 + offset + (index - 1) * 148
            pen.rounded_rectangle((22, top, 878, top + 130), 14, fill="white")
            values = [r[index] for r in data]
            low, high = min(values), max(values)
            pen.text((42, top + 12), label, font=_font(22), fill=MUTED if school else "#5a506e")
            pen.text((42, top + 53), f"{values[-1]:g}", font=_font(34, bold=True), fill=INK if school else "#342c54")
            dates = [datetime.fromisoformat(r[0]).date().toordinal() for r in data]
            points = [
                (
                    220 + 620 * (d - dates[0]) / max(1, dates[-1] - dates[0]),
                    top + 67 if high == low else top + 106 - 80 * (v - low) / (high - low),
                )
                for d, v in zip(dates, values)
            ]
            palette = ("#216457", "#987117", "#a66459") if school else ("#7954b0", "#33837d", "#94537f")
            color = palette[index - 1]
            if len(points) > 1:
                pen.line(points, fill=color, width=4)
            for x, y in points:
                pen.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
        pen.text(
            (36, 590 + offset),
            f"{data[0][0]}  —  {data[-1][0]}    ·    {len(data)} 个观测日",
            font=_font(23),
            fill=MUTED if school else "#4e4564",
        )
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()

    import asyncio

    return BotReply(
        text=title + "\n" + "\n".join(" · ".join(map(str, r)) for r in data),
        image_png=await asyncio.to_thread(draw_graph),
    )
