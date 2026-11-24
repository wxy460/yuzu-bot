"""Local campus interactions. Character reference: https://ongeki.sega.jp/character/1020/"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .domain import QuickAction
from .services.community import CN
from .services.result_card import academy_card
from .services.song_catalog import SongCatalogError
from .social_craft import CAMPUS_TABLES, register_crafting


@dataclass(frozen=True)
class Gift:
    name: str
    favor: int
    reply: str


GIFTS = {
    "1": Gift("柚子糖", 3, "一撕开就是柚子的香气！这颗先放进我的糖果包，留到练习结束～"),
    "2": Gift("星星贴纸", 2, "借我看看角度……贴在糖果包的搭扣上，合起来正好是一颗星！"),
    "3": Gift("弹珠汽水", 3, "我来按弹珠！你听，开瓶那一声好像给下一首歌数了个拍。"),
    "4": Gift("跳跳糖", 4, "噼里啪啦！要是把嘴里的声音写成谱面，第一小节该怎么打呀？"),
    "5": Gift("神秘口味棒棒糖", 5, "让我猜猜，第一口是奶油，第二口是……烤玉米？你也来猜呀！"),
    "6": Gift("星糖小袋", 8, "你把星星贴在糖纸上啦？这包我想带去和大家分着吃。"),
    "7": Gift("跳跳汽水", 10, "气泡自己在杯子里打拍子！来，碰一下杯～"),
    "8": Gift("放学糖果礼盒", 18, "打开一层还有一层！我想把今天的练习曲名也夹进糖盒里。"),
}
CHECKIN_GIFTS = ("1", "2", "3", "4", "5")
FORTUNES = (
    ("大吉", "今天糖纸拆出了一颗完整的星星！把最想打的那首放在手感最好的时候吧。"),
    ("中吉", "挑首副歌会哼的曲子吧，好听的那一拍，手也更容易跟上。"),
    ("小吉", "糖果包里还剩一对同口味的糖。今天要不要找个搭子合一首？"),
    ("吉", "抽到了汽水味！开局先松松手腕，给喜欢的歌留一轨。"),
    ("末吉", "今天慢一点也很好呀。先挑首熟悉的歌，把漏掉的那一拍听清楚。"),
)
STAGES = (
    (0, "初次见面的同学"),
    (10, "一起分糖的朋友"),
    (30, "放学后的练习搭档"),
    (70, "糖果试吃团搭档"),
)
CHECKIN_FAVOR = 1


def gift_id(value):
    return next((idx for idx, gift in GIFTS.items() if value in {idx, gift.name}), None)


def player_key(context):
    room = context.chat_id if context.chat_scope != "c2c" else ""
    return json.dumps([context.chat_scope, room, context.user_id], ensure_ascii=False)


def daily_draw(subject, day, kind):
    seed = f"{day.isoformat()}\n{kind}\n{subject}".encode()
    return int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")


def stage_of(total):
    current = STAGES[0][1]
    for threshold, name in STAGES[1:]:
        if total < threshold:
            return current, f"还有 {threshold - total} 点成为「{name}」"
        current = name
    return current, "所有伙伴称号都已解锁，好感仍可继续累积。"


class Campus:
    def __init__(self, database: Path):
        self.database = database

    @contextmanager
    def _db(self, *, write=False):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=8)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campus_checkins (
                    player TEXT, day TEXT, gift TEXT NOT NULL, quantity INTEGER NOT NULL,
                    favor INTEGER NOT NULL, streak INTEGER NOT NULL, PRIMARY KEY(player, day));
                CREATE TABLE IF NOT EXISTS campus_gifts (
                    player TEXT, message TEXT, gift TEXT NOT NULL, quantity INTEGER NOT NULL,
                    favor INTEGER NOT NULL, UNIQUE(player, message));
                CREATE TABLE IF NOT EXISTS campus_fortunes (
                    player TEXT, day TEXT, luck TEXT NOT NULL, advice TEXT NOT NULL,
                    song_id INTEGER, title TEXT, artist TEXT, PRIMARY KEY(player, day));
            """ + CAMPUS_TABLES)
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def checkin(self, player: str, day: date):
        day_key = day.isoformat()
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT * FROM campus_checkins WHERE player=? AND day=?", (player, day_key)
            ).fetchone()
            if row:
                return dict(row), False
            yesterday = (day - timedelta(days=1)).isoformat()
            last = db.execute(
                "SELECT streak FROM campus_checkins WHERE player=? AND day=?", (player, yesterday)
            ).fetchone()
            streak = last["streak"] + 1 if last else 1
            gift = CHECKIN_GIFTS[daily_draw(player, day, "gift") % len(CHECKIN_GIFTS)]
            quantity = 2 if streak % 7 == 0 else 1
            db.execute(
                "INSERT INTO campus_checkins VALUES(?,?,?,?,?,?)",
                (player, day_key, gift, quantity, CHECKIN_FAVOR, streak),
            )
            return {
                "gift": gift,
                "quantity": quantity,
                "favor": CHECKIN_FAVOR,
                "streak": streak,
            }, True

    def fortune(self, player, day, songs=()):
        luck, advice = FORTUNES[daily_draw(player, day, "fortune") % len(FORTUNES)]
        params = (player, day.isoformat())
        with self._db(write=True) as db:
            db.execute(
                """
                INSERT OR IGNORE INTO campus_fortunes(player,day,luck,advice) VALUES(?,?,?,?)
            """,
                (*params, luck, advice),
            )
            if songs:
                ordered = sorted(songs, key=lambda s: s.id)
                song = ordered[daily_draw(player, day, "song") % len(ordered)]
                db.execute(
                    """
                    UPDATE campus_fortunes SET song_id=?, title=?, artist=?
                    WHERE player=? AND day=? AND song_id IS NULL
                """,
                    (song.id, song.title, song.artist, *params),
                )
            return dict(
                db.execute(
                    "SELECT * FROM campus_fortunes WHERE player=? AND day=?", params
                ).fetchone()
            )

    @staticmethod
    def _bag(db, player, now=None):
        rows = db.execute(
            """
            SELECT gift, SUM(n) FROM (
                SELECT gift, quantity AS n FROM campus_checkins WHERE player=?
                UNION ALL SELECT gift, -quantity AS n FROM campus_gifts WHERE player=?
                UNION ALL SELECT material_a, -quantity FROM campus_crafts WHERE player=?
                UNION ALL SELECT material_b, -quantity FROM campus_crafts WHERE player=?
                UNION ALL SELECT product, MIN(quantity, MAX(0, (? - start) / seconds))
                    FROM campus_crafts WHERE player=?
            ) GROUP BY gift HAVING SUM(n)>0
        """,
            (player, player, player, player, int(time.time()) if now is None else now, player),
        )
        return dict(rows)

    def bag(self, player):
        with self._db() as db:
            return self._bag(db, player)

    def profile(self, player):
        with self._db() as db:
            count, checkin = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(favor),0) FROM campus_checkins WHERE player=?",
                (player,),
            ).fetchone()
            gifts, gift_favor = db.execute(
                "SELECT COALESCE(SUM(quantity),0), COALESCE(SUM(favor),0) FROM campus_gifts WHERE player=?",
                (player,),
            ).fetchone()
            pets = db.execute("SELECT COUNT(*) FROM campus_pets WHERE player=?", (player,)).fetchone()[0]
            return count, gifts, checkin + gift_favor + pets

    def give(self, player, message, gift, quantity):
        if gift not in GIFTS or not 1 <= quantity <= 99:
            raise ValueError("请输入图鉴里的礼物编号和 1 到 99 之间的数量。")
        with self._db(write=True) as db:
            previous = db.execute(
                "SELECT gift,quantity,favor FROM campus_gifts WHERE player=? AND message=?",
                (player, message),
            ).fetchone()
            if previous:
                return dict(previous), False
            owned = self._bag(db, player).get(gift, 0)
            if owned < quantity:
                raise ValueError(
                    f"你的背包里有 {owned} 份{GIFTS[gift].name}，这次要送 {quantity} 份。可用 /背包 查看现有礼物。"
                )
            favor = GIFTS[gift].favor * quantity
            db.execute(
                "INSERT INTO campus_gifts VALUES(?,?,?,?,?)",
                (player, message or None, gift, quantity, favor),
            )
            return {"gift": gift, "quantity": quantity, "favor": favor}, True


def register_social(command, community, catalog):
    def campus_command(name, description, **kwargs):
        def decorate(handler):
            @command(name, description, category="校园互动", **kwargs)
            async def wrapped(req):
                ctx = req.context
                if not ctx.user_id or (ctx.chat_scope != "c2c" and not ctx.chat_id):
                    return "柚子没能认出这条消息的发送者，请重新发送指令。"
                return await handler(req, player_key(ctx), Campus(community.database))

            return wrapped

        return decorate

    async def card(title, rows, **kwargs):
        return await academy_card(title, rows, section="校园互动", **kwargs)

    register_crafting(campus_command, card, GIFTS, gift_id, stage_of)

    @campus_command("签到", "来找柚子领每日礼物，连续七天领双份", aliases=("打卡", "checkin"))
    async def checkin(req, player, campus):
        if req.argument:
            return "发送 /签到 领取你今天的礼物。"
        day = datetime.now(CN).date()
        row, new = await asyncio.to_thread(campus.checkin, player, day)
        gift, count = GIFTS[row["gift"]].name, row["quantity"]
        note = "给你留了一份，接好啦！" if new else "今天这格已经盖过章啦，明天再来一起拆一份吧。"
        return await card(
            "柚子的签到本",
            [("今日已领取", f"{gift} × {count}"), ("签到好感", f"+{row['favor']}"), ("柚子", note)],
            metric=(f"{day:%m 月 %d 日} · 连续签到", f"{row['streak']} 天"),
            actions=(QuickAction("看看背包", "/背包"), QuickAction("今日运势", "/jrys")),
        )

    @campus_command("jrys", "抽今日运势，查看幸运曲和柚子的练习建议", aliases=("今日运势", "运势"))
    async def fortune(req, player, campus):
        if req.argument:
            return "发送 /jrys 抽取你今天的运势。"
        day = datetime.now(CN).date()
        entry = await asyncio.to_thread(campus.fortune, player, day)
        if entry["song_id"] is None and catalog:
            try:
                songs = await catalog.songs()
            except SongCatalogError:
                songs = ()
            if songs:
                entry = await asyncio.to_thread(campus.fortune, player, day, songs)
        rows = [("柚子的话", entry["advice"])]
        actions = ()
        note = ""
        if entry["song_id"] is not None:
            rows.extend((("幸运曲", entry["title"]), ("艺术家", entry["artist"])))
            actions = (QuickAction("查看幸运曲", f"/song {entry['song_id']}"),)
            note = f"发送 /song {entry['song_id']} 查看谱面。"
        else:
            rows.append(("幸运曲", "曲库暂时读不到，稍后可以回来补看。"))
        return await card(
            "今日舞萌运势", rows, metric=(str(day), entry["luck"]), note=note, actions=actions
        )

    @campus_command("背包", "查看签到获得的礼物与数量", aliases=("我的背包",))
    async def bag(req, player, campus):
        return await bag_card(player, campus)

    async def bag_card(player, campus):
        items = await asyncio.to_thread(campus.bag, player)
        rows = [
            (f"{key} · {GIFTS[key].name}", f"{items[key]} 份 · 每份好感 +{GIFTS[key].favor}")
            for key in sorted(items)
        ]
        return await card(
            "我的背包",
            rows or [("空背包", "去签到本盖个章，领一份今天的礼物吧。")],
            note="发送 /送礼 编号*数量 将礼物送给柚子。",
            actions=tuple(QuickAction(f"送{GIFTS[key].name}", f"/送礼 {key}") for key in sorted(items))
            + (QuickAction("去签到", "/签到"), QuickAction("礼物图鉴", "/礼物图鉴")),
        )

    @campus_command("礼物图鉴", "查看所有校园礼物、编号和好感值")
    async def gifts(req, player, campus):
        rows = [(f"{key} · {gift.name}", f"每份好感 +{gift.favor}") for key, gift in GIFTS.items()]
        return await card(
            "柚子的礼物图鉴", rows, note="编号 1–5 由签到获得，6–8 可在 /合成 查看制作配方。"
        )

    @campus_command(
        "送礼", "查看礼物或从自己的背包给柚子送礼", aliases=("赠礼",), usage="/送礼 [编号*数量]"
    )
    async def give(req, player, campus):
        if not req.argument:
            items = await asyncio.to_thread(campus.bag, player)
            return await card("送给柚子的礼物", [
                (f"{key} · {gift.name}", f"拥有 {items.get(key, 0)} 份 · 好感 +{gift.favor}")
                for key, gift in GIFTS.items()
            ], note="发送 /送礼 编号*数量，可一次送 1–99 份。",
                actions=tuple(QuickAction(f"送{GIFTS[key].name}", f"/送礼 {key}") for key in sorted(items))
                        + (QuickAction("去签到", "/签到"),))
        match = re.fullmatch(r"([^\s*＊×]+)(?:(?:\s*[*＊×]\s*|\s+)([1-9][0-9]?))?", req.argument)
        if not match:
            return "用法：/送礼 编号*数量 或 /送礼 礼物名 数量。数量为 1–99。"
        key = gift_id(match[1])
        if not key:
            return "柚子的图鉴里还没有这种礼物，发送 /礼物图鉴 看看编号吧。"
        try:
            gift, new = await asyncio.to_thread(
                campus.give,
                player,
                req.context.message_id,
                key,
                int(match[2] or 1),
            )
        except ValueError as exc:
            return str(exc)
        item = GIFTS[gift["gift"]]
        rows = [
            ("收下的礼物", f"{item.name} × {gift['quantity']}"),
            ("柚子", item.reply if new else "这一份已经收好啦！"),
        ]
        return await card(
            "柚子的回礼便签",
            rows,
            metric=("本次赠礼好感", f"+{gift['favor']}"),
            actions=(QuickAction("看看好感度", "/好感度"), QuickAction("我的背包", "/背包")),
        )

    @campus_command("好感度", "查看与柚子的伙伴称号和下个阶段", aliases=("我的好感", "好感"))
    async def profile(req, player, campus):
        days, sent, total = await asyncio.to_thread(campus.profile, player)
        stage, next_stage = stage_of(total)
        return await card(
            "我和柚子的伙伴记录",
            [("伙伴称号", stage), ("一起走过", f"签到 {days} 天 · 送出 {sent} 份礼物")],
            metric=("好感度", str(total)),
            note=next_stage,
        )

    @campus_command("互动帮助", "查看柚子的校园互动指令", aliases=("校园互动",))
    async def help_(req, player, campus):
        return await card(
            "来找柚子玩吧",
            [
                ("/签到", "盖一个章，领取礼物与 1 点好感。北京时间零点刷新。"),
                ("/jrys", "拆开今天的运势，听一听幸运曲。"),
                ("/背包 · /礼物图鉴", "看手上有哪些礼物，再按编号选一份。"),
                ("/送礼 1 1", "送出一份 1 号礼物。也可以直接写礼物名。"),
                ("/好感度", "查看伙伴称号与下个阶段。"),
                ("/摸头 · /状态", "每三小时可摸头一次，增加 1 点好感。"),
                ("/合成 · /一键合成 · /合成状态", "用两种材料制作礼物，自动配对基础材料或查看等待时间。"),
            ],
            note="群聊与私聊各自记录，你的礼物和好感只归你使用。",
        )
