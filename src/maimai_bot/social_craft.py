"""Time-based campus interactions backed by the campus inventory transaction."""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from datetime import datetime

from .domain import QuickAction
from .services.community import CN

PET_COOLDOWN = 3 * 3600
# (left, right, minutes per gift); automatic crafting uses only check-in gifts.
RECIPES = {"6": ("1", "2", 10), "7": ("3", "4", 15), "8": ("5", "6", 30)}
AUTO_RECIPES = ("6", "7")
CAMPUS_TABLES = """
    CREATE TABLE IF NOT EXISTS campus_pets (
        player TEXT, message TEXT, at INTEGER NOT NULL, UNIQUE(player, message));
    CREATE INDEX IF NOT EXISTS campus_pet_time ON campus_pets(player, at);
    CREATE TABLE IF NOT EXISTS campus_crafts (
        player TEXT, message TEXT, product TEXT, material_a TEXT NOT NULL, material_b TEXT NOT NULL,
        quantity INTEGER NOT NULL, start INTEGER NOT NULL, seconds INTEGER NOT NULL,
        PRIMARY KEY(player, message, product));
"""


def pet(campus, player, message, now):
    with campus._db(write=True) as db:
        duplicate = db.execute("SELECT at FROM campus_pets WHERE player=? AND message=?",
                               (player, message)).fetchone()
        if duplicate:
            return "已收到这次摸头", duplicate[0] + PET_COOLDOWN
        last = db.execute("SELECT MAX(at) FROM campus_pets WHERE player=?", (player,)).fetchone()[0]
        if last is not None and now < last + PET_COOLDOWN:
            return "还在休息", last + PET_COOLDOWN
        db.execute("INSERT INTO campus_pets VALUES(?,?,?)", (player, message or None, now))
        return "好感 +1", now + PET_COOLDOWN


def queue(campus, player, message, product, now):
    with campus._db(write=True) as db:
        message = message or uuid.uuid4().hex
        previous = db.execute(
            "SELECT product,quantity,start+quantity*seconds FROM campus_crafts WHERE player=? AND message=? ORDER BY start",
            (player, message),
        ).fetchall()
        if previous:
            return previous
        bag = campus._bag(db, player, now)
        last = db.execute("SELECT MAX(start+quantity*seconds) FROM campus_crafts WHERE player=?",
                          (player,)).fetchone()[0]
        start = max(now, last or 0)
        jobs = []
        for gift in (AUTO_RECIPES if product is None else (product,)):
            first, second, minutes = RECIPES[gift]
            available = min(bag.get(first, 0), bag.get(second, 0))
            quantity = available if product is None else min(1, available)
            if not quantity:
                continue
            seconds = minutes * 60
            db.execute("INSERT INTO campus_crafts VALUES(?,?,?,?,?,?,?,?)",
                       (player, message, gift, first, second, quantity, start, seconds))
            start += quantity * seconds
            jobs.append((gift, quantity, start))
            bag[first] -= quantity
            bag[second] -= quantity
        return jobs


def state(campus, player, now):
    with campus._db() as db:
        jobs = db.execute("""
            SELECT product, quantity, MIN(quantity, MAX(0, (? - start) / seconds)) AS done,
                   start+quantity*seconds AS ready
            FROM campus_crafts WHERE player=? AND start+quantity*seconds>? ORDER BY start
        """, (now, player, now)).fetchall()
        last = db.execute("SELECT MAX(at) FROM campus_pets WHERE player=?", (player,)).fetchone()[0]
    return jobs, max(0, (last or 0) + PET_COOLDOWN - now)


def register_crafting(command, card, gifts, gift_id, stage_of):
    def clock(at):
        return datetime.fromtimestamp(at, CN).strftime("%m-%d %H:%M")

    @command("摸头", "摸摸柚子的头，每三小时可增加 1 点好感", aliases=("摸摸头",))
    async def headpat(req, player, campus):
        now = int(time.time())
        status, ready = await asyncio.to_thread(pet, campus, player, req.context.message_id, now)
        quote = ("诶嘿，头发都翘起来啦！留一颗糖给你～" if status == "好感 +1" else
                 "刚刚摸过啦！我先把这束翘起来的头发理理。")
        return await card("摸摸柚子的头", [("柚子", quote), ("下次摸头", clock(ready))],
                          metric=("本次互动", status), actions=(QuickAction("看看状态", "/状态"),))

    @command("状态", "查看好感度、摸头冷却、背包与合成进度")
    async def status(req, player, campus):
        now = int(time.time())
        jobs, wait = await asyncio.to_thread(state, campus, player, now)
        _, _, favor = await asyncio.to_thread(campus.profile, player)
        bag = await asyncio.to_thread(campus.bag, player)
        stage, _ = stage_of(favor)
        return await card("柚子的伙伴手账", [
            ("伙伴称号", stage), ("摸头", f"还需 {math.ceil(wait / 60)} 分钟" if wait else "现在可以摸头"),
            ("背包", f"{sum(bag.values())} 份礼物"),
            ("合成", f"等待出炉 {sum(qty - done for _, qty, done, _ in jobs)} 份"),
        ], metric=("好感度", str(favor)), actions=(QuickAction("摸头", "/摸头"), QuickAction("看背包", "/背包")))

    async def craft(req, player, campus):
        auto = req.command == "一键合成"
        if not req.argument and not auto:
            rows = [(f"{a} {gifts[a].name} + {b} {gifts[b].name}",
                     f"{gift} {gifts[gift].name} · {minutes} 分钟")
                    for gift, (a, b, minutes) in RECIPES.items()]
            return await card("柚子的合成配方", rows,
                              note="发送 /合成 1+2 或写礼物名。每种材料各用 1 份，依次制作，完成自动入包。",
                              actions=(QuickAction("一键合成", "/一键合成"), QuickAction("看背包", "/背包")))
        product = None
        if auto and req.argument:
            return "发送 /一键合成 自动配对基础材料。"
        if not auto:
            parts = req.argument.replace("＋", "+").split("+")
            pair = sorted(gift_id(s.strip()) or "" for s in parts)
            product = next((gift for gift, (a, b, _) in RECIPES.items() if pair == [a, b]), None)
            if not product:
                return "这两份材料还没有配方。发送 /合成 可查看材料编号、名称和制作时间。"
        jobs = await asyncio.to_thread(queue, campus, player, req.context.message_id, product, int(time.time()))
        if not jobs:
            return "背包里的材料还凑不成这次配方。用 /背包 查看数量，或用 /签到 领一份礼物。"
        return await card("礼物合成订单",
                          [(f"{gifts[gid].name} × {qty}", f"最后一份 {clock(ready)} 出炉") for gid, qty, ready in jobs],
                          note="每做好一份就自动放入背包。", actions=(QuickAction("合成状态", "/合成状态"),))

    command("合成", "查看配方，或用两份材料合成礼物", usage="/合成 [材料1+材料2]")(craft)
    command("一键合成", "自动用背包内可配对的基础材料批量合成")(craft)

    @command("合成状态", "查看正在制作和排队的礼物", usage="/合成状态 [页码]")
    async def craft_status(req, player, campus):
        if req.argument and not (req.argument.isascii() and req.argument.isdigit() and 0 < len(req.argument) <= 6):
            return "发送 /合成状态 或 /合成状态 2 查看相应页。"
        jobs, _ = await asyncio.to_thread(state, campus, player, int(time.time()))
        page, pages = int(req.argument or 1), max(1, math.ceil(len(jobs) / 10))
        if not 1 <= page <= pages:
            return f"合成队列共有 {pages} 页。"
        rows = [(gifts[k].name, f"已完成 {done}/{qty} · {clock(ready)} 全部出炉")
                for k, qty, done, ready in jobs[(page - 1) * 10:page * 10]]
        return await card("合成队列", rows or [("工作台", "队列已空，做好的礼物可在背包查看。")],
                          note=f"第 {page}/{pages} 页 · 发送 /合成状态 页码 翻页" if pages > 1 else "",
                          actions=(QuickAction("看背包", "/背包"), QuickAction("合成配方", "/合成")))
