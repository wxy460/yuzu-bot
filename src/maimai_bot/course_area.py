"""National course and area lookups using published chart IDs and reward milestones."""

from __future__ import annotations

import asyncio
import random
import re
import unicodedata

from .services.community import PublicDataError
from .services.divingfish import NotBound, ScoreServiceError
from .services.gameplay import _record_key
from .services.lxns_scores import score_identity
from .services.result_card import result_card
from .services.song_catalog import DIFFICULTY_NAMES

# National PRiSM PLUS course charts in cabinet order. Source: ChiffonOwO/ChiffonMai,
# lib/service/RankTable/RankTableService.dart. IDs use the 10000 offset for DX.
# Each rule is (initial life, GREAT loss, GOOD loss, MISS loss, stage recovery).
CURRENT_COURSES = {
    "初段": ([(10256, 0), (11052, 0), (10319, 0), (617, 0)], (350, 0, 2, 5, 20)),
    "二段": ([(11764, 1), (11136, 1), (11265, 1), (11697, 1)], (350, 0, 2, 5, 20)),
    "三段": ([(11701, 1), (11411, 1), (11491, 1), (11638, 1)], (600, 1, 2, 5, 50)),
    "四段": ([(11698, 2), (11276, 2), (10535, 2), (417, 2)], (700, 2, 2, 5, 50)),
    "五段": ([(579, 2), (11003, 2), (11333, 2), (11770, 2)], (700, 2, 2, 5, 50)),
    "六段": ([(439, 2), (11405, 2), (432, 2), (11272, 3)], (700, 2, 2, 5, 50)),
    "七段": ([(11271, 3), (634, 3), (11580, 3), (11767, 3)], (700, 2, 2, 5, 50)),
    "八段": ([(11017, 3), (11048, 3), (11615, 3), (11512, 3)], (700, 2, 2, 5, 20)),
    "九段": ([(11699, 3), (11141, 3), (798, 3), (11496, 3)], (800, 2, 2, 5, 30)),
    "十段": ([(840, 3), (11207, 3), (11412, 3), (227, 3)], (900, 2, 2, 5, 30)),
    "真初段": ([(11010, 3), (11133, 3), (759, 3), (136, 2)], (50, 2, 3, 5, 10)),
    "真二段": ([(791, 3), (11199, 3), (191, 2), (11532, 3)], (50, 2, 3, 5, 10)),
    "真三段": ([(11599, 3), (794, 3), (239, 2), (709, 3)], (50, 2, 3, 5, 10)),
    "真四段": ([(11715, 3), (581, 2), (204, 3), (453, 3)], (50, 2, 3, 5, 10)),
    "真五段": ([(11583, 3), (11653, 3), (448, 3), (673, 3)], (50, 2, 3, 5, 10)),
    "真六段": ([(11316, 3), (11710, 3), (11177, 4), (700, 3)], (50, 2, 3, 5, 10)),
    "真七段": ([(11460, 3), (11208, 3), (451, 3), (11413, 3)], (50, 2, 3, 5, 10)),
    "真八段": ([(766, 3), (11213, 3), (24, 4), (556, 3)], (50, 2, 3, 5, 10)),
    "真九段": ([(11675, 3), (11618, 3), (589, 4), (11687, 3)], (50, 2, 3, 5, 10)),
    "真十段": ([(11295, 3), (11026, 3), (11470, 3), (11619, 3)], (50, 2, 3, 5, 10)),
    "真皆传": ([(11223, 3), (11745, 3), (11311, 3), (11753, 3)], (50, 2, 3, 5, 5)),
    "里皆传": ([(834, 4), (11612, 3), (11662, 3), (11663, 4)], (10, 1, 3, 10, 0)),
}

VERSION_NAMES = {0: "Splash", 25: "UNiVERSE", 35: "FESTiVAL", 45: "BUDDiES", 55: "PRiSM"}
VERSION_ALIASES = {
    "splash": 0,
    "spl": 0,
    "1.17": 0,
    "universe": 25,
    "uni": 25,
    "1.25": 25,
    "festival": 35,
    "fes": 35,
    "1.35": 35,
    "buddies": 45,
    "bud": 45,
    "1.45": 45,
    "prism": 55,
    "pri": 55,
    "1.55": 55,
    "当前": 55,
}
REWARD_TYPES = {
    "Character": "旅行伙伴",
    "MusicNew": "解锁乐曲",
    "NamePlate": "姓名框",
    "Title": "称号",
    "Frame": "背景板",
    "Icon": "头像",
    "MapTaskMusic": "课题曲",
    "Challenge": "挑战",
    "Present": "礼物",
}
# The cabinet data names some of its areas in Chinese and uses Japanese in unlock conditions.
AREA_NAMES = {
    "启程": "はじまりの",
    "大都会": "メトロポリス",
    "龙之": "ドラゴン",
    "咻哇咻哇": "しゅわしゅわ",
    "咻哇乐园": "しゅわーランド",
    "天空街": "スカイストリート",
    "宇宙空间站区域": "宇宙すてーしょん",
    "七彩": "なないろ",
    "派对": "パーリィ",
    "欢乐节日": "ハピフェス",
    "10周年纪念": "10周年記念",
}


def _area_key(value):
    value = unicodedata.normalize("NFKC", value).replace("区域ー", "区域")
    for cn, jp in AREA_NAMES.items():
        value = value.replace(cn, jp)
    key = "".join(value.replace("区域", "ちほー").casefold().split())
    return "7srefちほー" if key == "7sref" else key


def _distance(metres):
    if 0 < metres < 1000:
        return f"{metres:,} m"
    return f"{metres / 1000:,.3f}".rstrip("0").rstrip(".") + " km"


def _rank_name(name):
    return name.casefold().translate(str.maketrans("伝裏級", "传里级"))


def _rule(life):
    initial, great, good, miss, recovery = life
    return (
        f"初始 {initial} LIFE · GREAT −{great} / GOOD −{good} / MISS −{miss} · 每曲通过 +{recovery}"
    )


def _life(data):
    return tuple(
        data[key] for key in ("life", "greatDamage", "goodDamage", "missDamage", "recover")
    )


def _chart_id(chart):
    return chart.song_id + (10000 if chart.chart_type == "dx" else 0)


async def _stage_rows(scores, user, lineup, songs):
    charts = {(_chart_id(c), c.difficulty): c for song in songs for c in song.charts}
    personal = None
    try:
        records = await scores.player_records(user)
    except NotBound:
        note = "发送 /bind 后可同时查看四张谱面的个人最佳。"
    except ScoreServiceError:
        note = "暂时读取不到个人成绩。"
    else:
        personal = {_record_key(x): x for x in records.old + records.new}
        note = "显示个人单谱最佳。段位实战能否通过取决于连续四曲的 LIFE。"
    rows = []
    for n, (song_id, difficulty, fallback) in enumerate(lineup, 1):
        chart = charts.get((song_id, difficulty))
        kind = "dx" if song_id >= 10000 else "standard"
        title = chart.title if chart else fallback or f"ID {song_id}"
        tags = f"{'DX' if kind == 'dx' else 'SD'} {DIFFICULTY_NAMES[difficulty]}"
        text = f"{title}\n{tags}" + (f" Lv.{chart.level}" if chart else "") + f" · ID {song_id}"
        if personal is not None:
            record = personal.get((song_id % 10000, kind, difficulty))
            text += f" · 个人 {record['achievements']:.4f}%" if record else " · 未记录"
        rows.append((f"TRACK {n}", text))
    return rows, note


def _page_input(query):
    match = re.search(r"(?:^|\s)p([1-9]\d{0,4})$", query, re.IGNORECASE)
    return (query[: match.start()].strip(), int(match[1])) if match else (query.strip(), 1)


async def _area_page(title, rows, page, note):
    count = len(rows)
    pages = max(1, (count + 11) // 12)
    if page > pages:
        return f"此查询共 {pages} 页。"
    return await result_card(
        title,
        rows[(page - 1) * 12 : page * 12],
        section="区域",
        note=f"{note} · {page}/{pages} 页" + (" · 末尾加 p2 翻页" if pages > 1 else ""),
    )


async def _catalog_rewards(community, name):
    groups = {
        "area_icons": "头像",
        "area_plates": "姓名框",
        "area_frames": "背景板",
        "area_titles": "称号",
    }
    target = _area_key(name)
    # Only direct unlocks or completion of this exact map qualify. Distinguish numbered sequels.
    endings = {"で獲得", "complete", "を完走", "制覇"}
    data = await asyncio.gather(*(community.fetch(key) for key in groups))
    return [
        (label, item["name"])
        for label, entries in zip(groups.values(), data)
        for item in entries
        if any(_area_key(item.get("unlock") or "") == target + suffix for suffix in endings)
    ]


def register_course_area(command, community, scores, catalog):
    @command(
        "段位表", "查看国服固定段位四曲、LIFE 规则与个人最佳", usage="/段位表 真七段 或 uni十段"
    )
    async def course(req):
        value = _rank_name("".join(req.argument.split()))
        match = re.fullmatch(r"(.*?)(真?[初二三四五六七八九十]段|[里真]皆传)", value)
        if not match or (match[1] and match[1] not in VERSION_ALIASES):
            return "用法：/段位表 真七段 或 /段位表 uni十段。支持 spl、uni、fes、bud、prism。"
        version = VERSION_ALIASES.get(match[1], 55)
        rank = match[2]
        if version == 55:
            charts, life = CURRENT_COURSES[rank]
            lineup = [(sid, difficulty, "") for sid, difficulty in charts]
        else:
            courses = await community.fetch("courses")
            row = next(
                (
                    c
                    for c in courses
                    if not c["isRandom"]
                    and c["name"]["id"] // 10000 == version
                    and _rank_name(c["baseDaniId"]["str"]) == rank
                ),
                None,
            )
            if not row:
                return f"{VERSION_NAMES[version]} 的国服段位资料未收录「{rank}」。"
            lineup = [
                (x["musicId"]["id"], x["difficulty"]["id"], x["musicId"]["str"])
                for x in row["courseMusicData"]
            ]
            life = _life(row)
        rows, note = await _stage_rows(scores, score_identity(scores, req.context), lineup, await catalog.songs())
        return await result_card(
            f"{VERSION_NAMES[version]} · {rank}",
            rows,
            section="国服段位认定",
            metric=("通过四曲并保留 LIFE", _rule(life)),
            note=note,
        )

    @command("随机段位", "按随机段位规则抽取四曲", usage="/随机段位 expert上级")
    async def random_course(req):
        rank = _rank_name("".join(req.argument.split()))
        if not re.fullmatch("(expert|master)(初|中|超?上)级", rank):
            return (
                "用法：/随机段位 expert上级。可选 EXPERT 或 MASTER，再选初级、中级、上级或超上级。"
            )
        data = await community.fetch("courses")
        rule = max(
            (
                c
                for c in data
                if c["isRandom"] and _rank_name(c["name"]["str"].split("_")[-1]) == rank
            ),
            key=lambda c: c["name"]["id"],
            default=None,
        )
        if not rule:
            return "公开数据暂未收录该随机段位。"
        version = VERSION_NAMES.get(rule["name"]["id"] // 10000, "国服")
        difficulty = (2,) if rank.startswith("expert") else (3, 4)
        low, high = rule["lowerLevel"], rule["upperLevel"]
        songs = await catalog.songs()
        pool = [
            c
            for s in songs
            for c in s.charts
            if c.difficulty in difficulty and low <= round(c.constant * 10) <= high
        ]
        if len(pool) < 4:
            return "这个难度范围在当前国服曲库中不足四张谱面。"
        chosen = random.sample(pool, 4)
        lineup = [(_chart_id(c), c.difficulty, c.title) for c in chosen]
        rows, note = await _stage_rows(scores, score_identity(scores, req.context), lineup, songs)
        return await result_card(
            f"随机段位 · {rank.upper()}",
            rows,
            section="模拟选曲",
            metric=("LIFE 规则", _rule(_life(rule))),
            note=f"使用国服 {version} 公开规则，从当前曲库抽取定数 {low / 10:.1f}～{high / 10:.1f}。{note}",
        )

    @command(
        "asearch", "按名称或国服区域 ID 查找地图", aliases=("区域查询",), usage="/asearch 天界 [p2]"
    )
    async def area_search(req):
        query, page = _page_input(req.argument)
        areas = await community.fetch("map_ids")
        rows = [
            (f"m{v['id']}", v["name"])
            for v in areas
            if query.casefold() in v["name"].casefold()
            or _area_key(query) in _area_key(v["name"])
            or query.casefold() in (str(v["id"]), f"m{v['id']}")
        ]
        if not rows:
            return "没有找到对应区域。可以尝试 /asearch 天界。"
        return await _area_page(
            "舞萌区域", rows, page, "国服地图编号 · 发送 /ainfo m编号 查看里程奖励"
        )

    @command(
        "ainfo",
        "查询国服区域及各里程节点的奖励",
        aliases=("区域详情",),
        usage="/ainfo m150006 [p2]",
    )
    async def area_detail(req):
        query, page = _page_input(req.argument)
        if not query:
            return "用法：/ainfo m150006。用 /asearch 区域名 查找编号。"
        areas = await community.fetch("map_ids")
        numeric = re.fullmatch(r"m?(\d+)", query, re.IGNORECASE)
        area = next(
            (
                v
                for v in areas
                if (numeric and int(numeric[1]) == v["id"])
                or _area_key(query) == _area_key(v["name"])
            ),
            None,
        )
        story = None
        if not area and not numeric:
            story = next(
                (
                    v
                    for v in await community.fetch("areas")
                    if v["id"].casefold() == query.casefold()
                ),
                None,
            )
            if story:
                area = next(
                    (v for v in areas if _area_key(story["name"]) == _area_key(v["name"])), None
                )
        if not area:
            return "请用 /asearch 关键词 查找区域，再发送 /ainfo m编号。"
        map_id, name = area["id"], area["name"]
        reward_maps = await community.fetch("map_rewards")
        details = next((m for m in reward_maps if m["mapId"] == map_id), None)
        if details and details["treasures"]:
            rows = [
                (
                    _distance(item["distance"]),
                    f"{REWARD_TYPES.get(item['treasureType'], item['treasureType'])} · {item['treasureId']['str']}",
                )
                for item in sorted(details["treasures"], key=lambda i: i["distance"])
            ]
            note = "国服公开节点 · 按累计里程排列 · FloraSoftware"
        else:
            rows = await _catalog_rewards(community, name)
            note = "国服图鉴 · 仅列已记载的直接奖励，尚无完整里程表"
        if not story:
            try:
                story = next(
                    (
                        v
                        for v in await community.fetch("areas")
                        if _area_key(v["name"]) == _area_key(name)
                    ),
                    None,
                )
            except PublicDataError:
                pass
        if story:
            for title, field, key in (
                ("区域伙伴", "characters", "name"),
                ("区域曲目", "songs", "title"),
            ):
                if story.get(field):
                    rows.append((title, "、".join(v[key] for v in story[field])))
        if not rows:
            rows.append(("奖励资料", "此区域的公开目录尚未给出奖励或里程。"))
        return await _area_page(f"{name} · m{map_id}", rows, page, note)
