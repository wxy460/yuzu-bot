"""Read-only music and score commands for rhythm games outside maimai."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .domain import BotReply, CommandRequest, MessageContext, QuickAction
from .router import CommandRouter
from .services import lxns_chuni
from .services.divingfish import DivingFishService, NotBound, ScoreServiceError
from .services.lxns_scores import lxns_subject
from .services.result_card import academy_card

CHUNI = "https://www.diving-fish.com/api/chunithmprober"
CHU_ALIASES = "https://maimai.lxns.net/api/v0/chunithm/alias/list"
CHU_JP = "https://otoge-db.net/chunithm/data/music-ex.json"
TAIKO = "https://raw.githubusercontent.com/taikowiki/taiko-song-database/main/database.json"
BANDORI = "https://bestdori.com/api/songs/all.7.json"
SEKAI = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-diff/main"
DIFFS = lxns_chuni.DIFFICULTIES
JP_DIFFS = ("bas", "adv", "exp", "mas", "ult")
RANK = {
    "sss+": 1009000,
    "sss": 1007500,
    "ss+": 1005000,
    "ss": 1000000,
    "s+": 990000,
    "s": 975000,
    "aaa": 950000,
    "aa": 925000,
    "a": 900000,
}
FC = {
    "fc": {"fullcombo", "alljustice", "alljusticecritical"},
    "aj": {"alljustice", "alljusticecritical"},
    "ajc": {"alljusticecritical"},
}
FILTER_HELP = "支持等级（14+）、定数（14.7）、版本（sun plus）、难度（Master）、类别或曲名。成绩列表还支持 AJ、FC、SSS 等下限。列表可加「第2页」。"
CLEAR_TYPES = {"catastrophy", "absolute", "brave", "hard", "clear", "failed"}


def _clear_type(query):
    return {"catastrophe": "catastrophy", "normal": "clear"}.get(_norm(query), _norm(query))


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


class _ChuniGrant(DivingFishService):
    scope = "chunithm.records.read"

    def _subject_ref(self, external_id: str) -> str:
        # Upstream keys device consents by client and subject. A second scope
        # on the maimai subject could replace its existing grant.
        return super()._subject_ref("chunithm:" + external_id)

    async def headers(self, external_id: str) -> dict[str, str]:
        self._require_enabled()
        return {"Authorization": f"Bearer {await self._access_token(external_id)}"}


class CrossGameService:
    def __init__(
        self, http: httpx.AsyncClient, *, client_id: str, client_secret: str,
        lxns=None, binding_state: Path | None = None,
    ) -> None:
        self.http = http
        self.grant = _ChuniGrant(http, client_id=client_id, client_secret=client_secret, binding_state=binding_state)
        self.lxns = lxns
        self._cached: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def catalog(self, url: str):
        async with self._locks.setdefault(url, asyncio.Lock()):
            expiry, value = self._cached.get(url, (0, None))
            if time.monotonic() < expiry:
                return value
            try:
                response = await self.http.get(url, timeout=18)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, (list, dict)) or not data:
                    raise ValueError("empty catalog")
            except (httpx.HTTPError, ValueError):
                if value is None:
                    raise
                return value
            self._cached[url] = (time.monotonic() + 21600, data)
            return data

    def source(self, context):
        return "落雪" if self.lxns and isinstance(context, MessageContext) and self.lxns.uses_chuni(lxns_subject(context)) else "水鱼"

    def trend_key(self, context):
        if self.source(context) == "落雪":
            user = lxns_subject(context)
            identity = self.lxns.identity(user)
            if not identity:
                raise ScoreServiceError("请先发送 /lxns 完成落雪绑定。")
            return f"chuni:lxns:{identity['binding']}:{user}"
        user_id = context.user_id if isinstance(context, MessageContext) else context
        return "chuni:" + self.grant.history_key(user_id)

    async def song_list(self, context=None):
        if self.source(context) == "落雪":
            return lxns_chuni.catalog_songs(await self.catalog(lxns_chuni.SONGS))
        return await self.catalog(f"{CHUNI}/music_data")

    async def chuni_search(self, query: str, context=None) -> list[dict]:
        songs = await self.song_list(context)
        query = _norm(query)
        if not query:
            return []
        if re.fullmatch(r"z?\d+", query):
            return [s for s in songs if str(s["id"]) == query.removeprefix("z")]
        try:
            aliases = await self.catalog(CHU_ALIASES)
        except (httpx.HTTPError, ValueError):
            aliases = {}
        ids = {
            int(a["song_id"])
            for a in aliases.get("aliases", [])
            if any(query == _norm(name) for name in a["aliases"])
        }
        matches = [s for s in songs if query in _norm(s["title"]) or s["id"] in ids]
        return sorted(
            matches,
            key=lambda s: (
                _norm(s["title"]) != query,
                s["id"] not in ids,
                len(s["title"]),
                s["id"],
            ),
        )

    async def best(self, context, username: str = "") -> dict:
        if self.source(context) == "落雪" and not username:
            return await lxns_chuni.read_scores(self.lxns, context, lambda: self.song_list(context), best=True)
        user_id = context.user_id if isinstance(context, MessageContext) else context
        identity = self.trend_key(context) if not username else None
        headers = {} if username else await self.grant.headers(user_id)
        response = await self.http.post(
            f"{CHUNI}/query/player",
            json={"username": username} if username else {},
            headers=headers,
            timeout=20,
        )
        if username and response.status_code in (400, 403):
            raise ScoreServiceError("这个水鱼用户名不存在或未开放成绩。")
        response.raise_for_status()
        if identity and identity != self.trend_key(context):
            raise ScoreServiceError("查询期间中二授权或数据源发生变化，请重新查询。")
        return response.json()

    async def records(self, context) -> list[dict]:
        if self.source(context) == "落雪":
            return await lxns_chuni.read_scores(self.lxns, context, lambda: self.song_list(context))
        user_id = context.user_id if isinstance(context, MessageContext) else context
        identity = self.trend_key(context)
        headers = await self.grant.headers(user_id)
        response = await self.http.get(f"{CHUNI}/player/records", headers=headers, timeout=30)
        response.raise_for_status()
        if identity != self.trend_key(context):
            raise ScoreServiceError("查询期间中二授权或数据源发生变化，请重新查询。")
        return response.json()["records"]["best"]


def _page_arg(argument: str) -> tuple[str, int]:
    match = re.search(r"(?:^|\s)第(\d{1,5})页$", argument)
    if match:
        return argument[: match.start()].strip(), max(1, int(match[1]))
    return argument.strip(), 1


async def _page_card(title, rows, *, section="中二节奏", page=1, metric=None, note="", size=15):
    pages = max(1, math.ceil(len(rows) / size))
    if page > pages:
        return f"只有 {pages} 页，请重新指定页码。"
    start = (page - 1) * size
    return await academy_card(
        title,
        rows[start : start + size] or [("结果", "没有符合条件的谱面或曲目")],
        section=section,
        metric=metric,
        note=" · ".join(
            filter(
                None,
                (
                    note,
                    f"第 {page}/{pages} 页，共 {len(rows)} 项" if pages > 1 else "",
                    "命令末尾加「第2页」翻页" if pages > 1 else "",
                ),
            )
        ),
    )


def _charts(songs) -> list[dict]:
    charts = []
    for song in songs:
        for i, (cid, level, ds) in enumerate(zip(song["cids"], song["level"], song["ds"])):
            if cid is None or not level or i >= len(DIFFS):
                continue
            charts.append(
                {
                    "cid": cid,
                    "title": song["title"],
                    "mid": song["id"],
                    "level": level,
                    "ds": ds,
                    "level_label": DIFFS[i],
                    "info": {**song["basic_info"], "from": song["chart_versions"][i]} if "chart_versions" in song else song["basic_info"],
                }
            )
    return charts


def _chart_filter(chart: dict, query: str) -> bool:
    key = _norm(query)
    info = chart.get("info", {})
    if key in FC:
        return _norm(chart.get("fc") or "") in FC[key]
    if _clear_type(key) in CLEAR_TYPES:
        return _norm(chart.get("clear") or "") == _clear_type(key)
    if key in RANK:
        return chart.get("score", 0) >= RANK[key]
    if re.fullmatch(r"\d+\+?", key):
        return str(chart["level"]) == key
    if re.fullmatch(r"\d+\.\d", key):
        return float(key) == chart["ds"]
    return (
        key == _norm(chart["level_label"])
        or key in _norm(chart["title"])
        or key in _norm(str(info.get("from") or ""))
        or key in _norm(str(info.get("genre") or ""))
    )


def _record_row(record: dict, rank: int = 0) -> tuple[str, str]:
    return (
        f"{'#' + str(rank) + ' · ' if rank else ''}{record['level_label']} {record['level']}",
        f"{record['title']}\n{record['score']:,}  ·  RA {record['ra']:.2f}  {record.get('fc') or ''} {str(record.get('clear') or '').upper()}".rstrip(),
    )


async def _command_error(handler, request):
    try:
        return await handler(request)
    except NotBound:
        return "请先使用 /中二绑定 单独授权读取中二成绩。"
    except ScoreServiceError as exc:
        return str(exc)
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return "上游数据暂时无法读取，请稍后重试。"


def register_cross_game(router: CommandRouter, service: CrossGameService) -> None:
    def command(name, description, *, aliases=(), usage="", category="中二节奏"):
        def decorate(handler: Callable[[CommandRequest], Awaitable[BotReply | str]]):
            async def handle(request):
                return await _command_error(handler, request)

            return router.command(
                name, description, aliases=aliases, category=category, usage=usage
            )(handle)

        return decorate

    @command("chubind", "授权或解除水鱼中二节奏查分", aliases=("中二绑定",), usage="/chubind [解绑]")
    async def bind(req):
        if req.argument == "解绑":
            service.grant.unlink(req.context.user_id)
            return "已解除此 QQ 身份的水鱼中二绑定。下次查询时可用 /chubind 重新授权。"
        if req.argument:
            return "发送 /chubind 授权，或 /chubind 解绑 解除自己的中二绑定。"
        binding = await service.grant.start_binding(req.context.user_id)
        text = f"中二只读授权 · 用户码 {binding.user_code}\n{binding.url}"
        return BotReply(text=text, quick_actions=(QuickAction("打开水鱼授权", url=binding.url),))

    @command("chub30", "查看中二旧曲 Best 30", usage="/chub30 [公开水鱼用户名]")
    async def b30(req):
        return await best_pool(req, new=False)

    @command("chun20", "查看中二当前版本 New 20", aliases=("中二n20",), usage="/chun20 [公开水鱼用户名]")
    async def n20(req):
        return await best_pool(req, new=True)

    async def best_pool(req, new):
        username = req.argument.removeprefix("水鱼 ").strip()
        source = service.source(req.context)
        if source == "落雪" and username and req.argument == username:
            return f"当前选择了落雪。查询水鱼公开用户名时请显式发送 /{req.command} 水鱼 用户名。"
        data = await service.best(req.context, username)
        records = data["records"]
        if new:
            if "n20" not in records:
                return "该成绩源没有提供这份资料的 New 20。"
            pool = records["n20"][:20]
        else:
            pool = records.get("b30", records.get("best", []))[:30]
        return await _page_card(
            f"{data.get('nickname') or data.get('username', '玩家')} · {'New 20' if new else 'Best 30'}",
            [_record_row(r, i) for i, r in enumerate(pool, 1)],
            size=20 if new else 30,
            metric=("中二 Rating", f"{data['rating']:.2f}"),
            note=f"数据来自{'水鱼' if username else source}中二节奏查分器",
        )

    @command("cscore", "按歌名、别名或 zID 查询自己的中二成绩", usage="/cscore z317")
    async def single(req):
        query, page = _page_arg(req.argument)
        if not query:
            return "用法：/cscore 曲名、别名或 zID"
        songs = await service.chuni_search(query, req.context)
        if not songs:
            return "未找到对应的中二曲目。"
        if len(songs) > 1:
            return await _page_card(
                "选择中二曲目",
                [(f"z{s['id']}", s["title"]) for s in songs],
                page=page,
                note="发送 /cscore zID 查看个人成绩",
            )
        song = songs[0]
        records = {r["cid"]: r for r in await service.records(req.context)}
        rows = []
        for c in _charts([song]):
            r = records.get(c["cid"])
            rows.append(
                (
                    f"{c['level_label']} {c['level']}",
                    _record_row(r)[1].split("\n", 1)[1] if r else "暂无成绩",
                )
            )
        return await _page_card(song["title"], rows, note=f"{service.source(req.context)} · z{song['id']}")

    @command("cscorelist", "按等级、版本、评价或 FC/AJ 筛选个人成绩", usage="/cscorelist aj")
    async def scores(req):
        query, page = _page_arg(req.argument)
        if not query:
            return FILTER_HELP
        if _clear_type(query) in CLEAR_TYPES and service.source(req.context) == "水鱼":
            return await academy_card(
                "通关类型筛选",
                [
                    (
                        "上游字段",
                        ("水鱼中二成绩只返回 FC/AJ 与分数，无法据此判定所用技能的通关类型。"),
                    )
                ],
                section="中二节奏",
                note="可使用 /cscorelist aj 或 /cscorelist sss。已绑定落雪时，发送 /中二数据源 落雪 可查 CLEAR 类型。",
            )
        catalog, records = await asyncio.gather(
            service.song_list(req.context), service.records(req.context)
        )
        infos = {s["id"]: s["basic_info"] for s in catalog}
        matches = [
            r for r in records if _chart_filter({"info": infos.get(r["mid"], {}), **r}, query)
        ]
        matches.sort(key=lambda r: (-r["score"], -r["ra"], r["cid"]))
        return await _page_card(f"中二成绩 · {query}", [_record_row(r) for r in matches], page=page, note=service.source(req.context))

    @command("中二进度表", "查看等级或版本的达成进度", usage="/中二进度表 14+ [sss|aj]")
    async def progress(req):
        query, page = _page_arg(req.argument)
        if not query:
            return FILTER_HELP + " 例：/中二进度表 sun plus sss"
        words = query.split()
        goal = _norm(words[-1])
        if len(words) > 1 and (goal in (RANK | FC) or _clear_type(goal) in CLEAR_TYPES):
            if _clear_type(goal) in CLEAR_TYPES and service.source(req.context) == "水鱼":
                return "水鱼成绩没有 CLEAR 类型。可用 /中二数据源 落雪 切换已绑定的落雪账号。"
            query = " ".join(words[:-1])
        else:
            goal = "s"
        songs, records = await asyncio.gather(
            service.song_list(req.context), service.records(req.context)
        )
        lookup = {r["cid"]: r for r in records}
        charts = [c for c in _charts(songs) if _chart_filter(c, query)]
        charts.sort(key=lambda c: -c["ds"])
        rows = []
        for c in charts:
            r = lookup.get(c["cid"])
            if r and _chart_filter(r, goal):
                continue
            detail = f"{r['score']:,} {r.get('fc') or ''}" if r else "未游玩"
            rows.append((f"{c['level_label']} {c['level']}", f"{c['title']} · {detail}"))
        done = len(charts) - len(rows)
        note = "全部达成！" if charts and not rows else "下列为尚未达成的谱面，按定数降序"
        return await _page_card(
            f"{query} · {goal.upper()} 进度",
            rows,
            page=page,
            metric=("已达成谱面", f"{done} / {len(charts)}"),
            note=note,
        )

    @command("中二定数表", "查询国服或日服谱面定数", usage="/中二定数表 [日服]14+")
    async def constants(req):
        query, page = _page_arg(req.argument)
        jp = query.startswith("日服")
        query = query.removeprefix("日服").removeprefix("国服").strip()
        if not re.fullmatch(r"\d{1,2}(?:\+|\.\d)?", query):
            return "用法：/中二定数表 14+ 或 /中二定数表 日服14+"
        if jp:
            charts = []
            for s in await service.catalog(CHU_JP):
                for diff, name in zip(JP_DIFFS, DIFFS):
                    level = s.get(f"lev_{diff}")
                    raw = s.get(f"lev_{diff}_i")
                    if not level:
                        continue
                    ds = float(raw) if raw and re.fullmatch(r"\d+(\.\d+)?", str(raw)) else None
                    charts.append(
                        {"level": level, "ds": ds, "level_label": name, "title": s["title"]}
                    )
        else:
            charts = _charts(await service.song_list(req.context))
        matches = [c for c in charts if _chart_filter(c, query)]
        matches.sort(key=lambda c: (-(c["ds"] or 0), c["title"]))
        rows = [
            (
                f"{c['ds']:.1f}" if c["ds"] is not None else "定数未公布",
                f"{c['title']} · {c['level_label']}",
            )
            for c in matches
        ]
        return await _page_card(
            f"{'日服' if jp else '国服'} {query} 定数表",
            rows,
            page=page,
            note="OTOGE DB" if jp else f"{service.source(req.context)}中二曲库",
        )

    @command("秋什么", "随机推荐中二乐曲", usage="/秋什么 [等级]")
    async def chuni_random(req):
        charts = _charts(await service.song_list(req.context))
        matches = [c for c in charts if _chart_filter(c, req.argument)] if req.argument else charts
        if not matches:
            return "没有符合条件的中二谱面。"
        c = random.choice(matches)
        return await academy_card(
            c["title"],
            [
                ("曲师", c["info"]["artist"]),
                ("版本", c["info"]["from"]),
                ("BPM", str(c["info"]["bpm"])),
            ],
            section="中二节奏",
            metric=(c["level_label"], str(c["ds"])),
            note=f"{service.source(req.context)} · z{c['mid']}",
        )

    @command("tk", "检索太鼓之达人曲名与列表位置", usage="/tk idol", category="其他音游")
    async def taiko(req):
        query, page = _page_arg(req.argument)
        if not query:
            return "用法：/tk 部分曲名、别名或歌曲编号"
        key = _norm(query)

        def match(s):
            names = (s.get(k) for k in ("title", "titleEn", "titleZhCN", "aliasEn", "romaji"))
            return str(s["songNo"]) == query or any(key in _norm(n) for n in names if n)

        songs = [s for s in await service.catalog(TAIKO) if match(s)]
        rows = []
        for s in songs:
            courses = s.get("courses") or {}
            stars = " · ".join(
                f"{label}★{courses[k]['level']}"
                for k, label in (
                    ("easy", "梅"),
                    ("normal", "竹"),
                    ("hard", "松"),
                    ("oni", "鬼"),
                    ("ura", "里"),
                )
                if courses.get(k)
            )
            status = " · 已删除" if s.get("isDeleted") else ""
            rows.append(
                (
                    f"ID {s['songNo']}",
                    (
                        f"{s['title']}{status}\n{stars}\n"
                        f"列表顺位 {s.get('order', '—')} · {' / '.join(s.get('genre') or [])}"
                    ),
                )
            )
        return await _page_card(
            f"太鼓曲目 · {query}",
            rows,
            page=page,
            size=10,
            section="太鼓之达人",
            note="TaikoWiki 曲库顺位，机台收录因地区或版本而异",
        )

    @command("邦什么", "推荐 BanG Dream 乐曲与谱面等级", category="其他音游")
    async def bandori(req):
        catalog = await service.catalog(BANDORI)
        now = time.time() * 1000
        visible = [
            (i, s)
            for i, s in catalog.items()
            if s["musicTitle"][0]
            and float(s["publishedAt"][0] or 0) <= now < float(s["closedAt"][0] or math.inf)
        ]
        sid, song = random.choice(visible)
        rows = [
            (
                name,
                f"Lv.{song['difficulty'][str(i)]['playLevel']}  ·  {song['notes'].get(str(i), '—')} Notes",
            )
            for i, name in enumerate(("Easy", "Normal", "Hard", "Expert", "Special"))
            if str(i) in song["difficulty"]
        ]
        return await academy_card(
            song["musicTitle"][0], rows, section="BanG Dream", note=f"Bestdori · 日服 · ID {sid}"
        )

    @command("烤什么", "推荐 Project Sekai 乐曲与谱面等级", category="其他音游")
    async def sekai(req):
        songs, charts = await asyncio.gather(
            service.catalog(f"{SEKAI}/musics.json"),
            service.catalog(f"{SEKAI}/musicDifficulties.json"),
        )
        song = random.choice([s for s in songs if s["publishedAt"] <= time.time() * 1000])
        rows = [
            (d["musicDifficulty"].title(), f"Lv.{d['playLevel']}  ·  {d['totalNoteCount']} Notes")
            for d in charts
            if d["musicId"] == song["id"]
        ]
        return await academy_card(
            song["title"],
            rows,
            section="Project Sekai",
            note=f"Sekai-World · 日服 · ID {song['id']} · 作曲 {song['composer']}",
        )
