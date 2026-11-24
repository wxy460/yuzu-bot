"""Group-scoped conversation, impressions and passive reply budgets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .domain import QuickAction
from .services.chat import ChatUnavailable
from .services.community import CN
from .services.result_card import academy_card

logger = logging.getLogger(__name__)
MONTH = 30 * 86400
# Never sample account details, contact information, credentials or attributed mentions.
PRIVATE = re.compile(
    r"https?://|\b[^\s@]+@[^\s@]+\.|<@|\d{7,}|验证码|令牌|密钥|密码|身份证|银行卡|手机号|住址|家庭地址|病历|确诊|我的电话|access[_ -]?token",
    re.IGNORECASE,
)
SHARED = re.compile(
    r"^(?:(?:明天|今晚|周末|下周|今天|放学后)[，, ]*)?(?:我们|咱们|大家|一起|组队|团练|合练).{0,45}(?:打|约|拼机|练|玩|组队|一起|听|集合|见面)"
)
PERSONAL = re.compile(
    r"我(?:今天|昨天|明天|早上|上午|下午|晚上|最近|平时|喜欢|想|打算|准备|在|刚|学|练|打|去|听|最爱|推)|(?:今天|昨天|明天|早上|上午|下午|晚上).{0,4}我"
)
TOPICS = {
    "舞萌": r"舞萌|鸟加|推分|maimai|b50",
    "中二节奏": r"中二|chunithm",
    "音击": r"音击|オンゲキ|ongeki",
    "练歌": r"练习|练歌|谱面|手法",
    "一起游玩": r"一起|组队|拼机",
    "音乐": r"听歌|音乐|作曲|新歌",
}
LEVELS = {"安静": 1, "偶尔": 3, "活跃": 6}
DEFAULT_LIMIT = 3


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _snippet(text):
    text = " ".join(text.split())
    if len(text) < 2 or len(text) > 300 or text.startswith(("/", "#", "@")) or PRIVATE.search(text):
        return ""
    return text[:180]


@dataclass(frozen=True)
class Options:
    talk: int = 0
    memory: int = 0
    rate: int = DEFAULT_LIMIT
    poke: int = 0
    revision: int = 0


@dataclass(frozen=True)
class Turn:
    transcript: str
    revision: int
    nudge: bool
    started: float


class GroupLife:
    def __init__(self, database: Path, chat):
        self.path = database
        self.chat = chat
        self.lock = threading.RLock()
        self.recent = OrderedDict()
        self.initialized = False

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.initialized:
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=5)
        if not self.initialized:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS group_settings(room TEXT PRIMARY KEY, talk INTEGER DEFAULT 0,
                  memory INTEGER DEFAULT 0, rate INTEGER DEFAULT 3, poke INTEGER DEFAULT 0, revision INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS group_notes(room TEXT, player TEXT, kind TEXT, body TEXT, stamp REAL,
                  PRIMARY KEY(room,player,body));
                CREATE INDEX IF NOT EXISTS group_notes_time ON group_notes(stamp);
                CREATE TABLE IF NOT EXISTS group_optout(room TEXT, player TEXT, PRIMARY KEY(room,player));
                CREATE TABLE IF NOT EXISTS group_topics(room TEXT, player TEXT, day INTEGER, topic TEXT, n INTEGER,
                  PRIMARY KEY(room,player,day,topic));
                CREATE TABLE IF NOT EXISTS group_attempts(room TEXT, message TEXT, stamp REAL, PRIMARY KEY(room,message));
                CREATE INDEX IF NOT EXISTS group_attempt_time ON group_attempts(stamp);
            """)
            self.initialized = True
        return db

    @staticmethod
    def options(db, room):
        row = db.execute(
            "SELECT talk,memory,rate,poke,revision FROM group_settings WHERE room=?", (room,)
        ).fetchone()
        return Options(*row) if row else Options()

    def settings(self, room_id, changes=None, *, erase=False):
        room = _key(room_id)
        with self.lock, closing(self.connect()) as db, db:
            if changes or erase:
                db.execute("INSERT OR IGNORE INTO group_settings(room) VALUES(?)", (room,))
                for field, value in (changes or {}).items():
                    if field not in {"talk", "memory", "rate", "poke"}:
                        raise ValueError("无效设置")
                    db.execute(f"UPDATE group_settings SET {field}=? WHERE room=?", (value, room))
                if changes and changes.get("talk") and "rate" not in changes:
                    db.execute(
                        "UPDATE group_settings SET rate=? WHERE room=? AND rate=0",
                        (DEFAULT_LIMIT, room),
                    )
                db.execute("UPDATE group_settings SET revision=revision+1 WHERE room=?", (room,))
                if erase:
                    db.execute("DELETE FROM group_notes WHERE room=?", (room,))
                    db.execute("DELETE FROM group_topics WHERE room=?", (room,))
                if erase or (changes and 0 in (changes.get("talk"), changes.get("memory"))):
                    self.recent.pop(room, None)
            return self.options(db, room)

    @staticmethod
    def opted_out(db, room, player):
        return (
            db.execute(
                "SELECT 1 FROM group_optout WHERE room=? AND player=?", (room, player)
            ).fetchone()
            is not None
        )

    def forget_person(self, ctx, mode):
        room, player = _key(ctx.chat_id), _key(ctx.user_id)
        with self.lock, closing(self.connect()) as db, db:
            if mode == "恢复":
                db.execute("DELETE FROM group_optout WHERE room=? AND player=?", (room, player))
                return
            if mode == "退出":
                db.execute("INSERT OR IGNORE INTO group_optout VALUES(?,?)", (room, player))
            db.execute("DELETE FROM group_notes WHERE room=? AND player=?", (room, player))
            db.execute("DELETE FROM group_topics WHERE room=? AND player=?", (room, player))
            db.execute("UPDATE group_settings SET revision=revision+1 WHERE room=?", (room,))
            if room in self.recent:
                self.recent[room] = deque(
                    (entry for entry in self.recent[room] if entry[1] not in (player, "柚子")),
                    maxlen=12,
                )

    def notes(self, ctx, *, personal):
        room, player = _key(ctx.chat_id), _key(ctx.user_id)
        with self.lock, closing(self.connect()) as db, db:
            db.execute("DELETE FROM group_notes WHERE stamp<?", (time.time() - MONTH,))
            settings = self.options(db, room)
            excluded = self.opted_out(db, room, player)
            if not settings.memory or (personal and excluded):
                return settings, excluded, []
            query = "SELECT stamp,body FROM group_notes WHERE room=?"
            if personal:
                rows = db.execute(
                    query + " AND player=? ORDER BY stamp DESC LIMIT 8", (room, player)
                )
            else:
                rows = db.execute(
                    query + " AND kind='shared' ORDER BY stamp DESC LIMIT 24", (room,)
                )
            return settings, excluded, list(rows)

    def topics(self, ctx):
        room = _key(ctx.chat_id)
        with self.lock, closing(self.connect()) as db, db:
            if not self.options(db, room).memory:
                return []
            cutoff = int((time.time() - MONTH) // 86400)
            db.execute("DELETE FROM group_topics WHERE day<=?", (cutoff,))
            return [
                r[0]
                for r in db.execute(
                    "SELECT topic FROM group_topics WHERE room=? GROUP BY topic ORDER BY SUM(n) DESC,topic LIMIT 4",
                    (room,),
                )
            ]

    def prepare(self, ctx, *, explicit=False, observe_only=False):
        line = _snippet(ctx.content)
        if (
            not line
            or ctx.chat_scope != "group"
            or not ctx.chat_id
            or not ctx.user_id
            or not ctx.message_id
        ):
            return None
        room, player, now = _key(ctx.chat_id), _key(ctx.user_id), time.time()
        with self.lock, closing(self.connect()) as db, db:
            settings = self.options(db, room)
            if not (settings.talk or settings.memory) or self.opted_out(db, room, player):
                return None
            log = self.recent.setdefault(room, deque(maxlen=12))
            self.recent.move_to_end(room)
            if len(self.recent) > 256:
                self.recent.popitem(last=False)
            msg = _key(ctx.message_id)
            if any(entry[2] == msg for entry in log):
                return None
            log.append((now, player, msg, line))
            if settings.memory:
                day = int(now // 86400)
                for topic, pattern in TOPICS.items():
                    if re.search(pattern, line, re.IGNORECASE):
                        db.execute("DELETE FROM group_topics WHERE day<=?", (day - 30,))
                        db.execute(
                            "INSERT INTO group_topics VALUES(?,?,?,?,1) ON CONFLICT(room,player,day,topic) DO UPDATE SET n=n+1",
                            (room, player, day, topic),
                        )
                kind = (
                    "self"
                    if PERSONAL.search(line)
                    else "shared"
                    if SHARED.search(line) and not re.search(r"我(?!们)", line)
                    else ""
                )
                if kind:
                    db.execute("DELETE FROM group_notes WHERE stamp<?", (now - MONTH,))
                    db.execute(
                        "INSERT OR REPLACE INTO group_notes VALUES(?,?,?,?,?)",
                        (room, player, kind, line, now),
                    )
                    for clause, params, limit in [
                        ("room=? AND player=?", (room, player), 24),
                        ("room=?", (room,), 250),
                    ]:
                        db.execute(
                            "DELETE FROM group_notes WHERE rowid IN (SELECT rowid FROM group_notes WHERE "
                            + clause
                            + " ORDER BY stamp DESC,rowid DESC LIMIT -1 OFFSET ?)",
                            (*params, limit),
                        )
            if observe_only:
                return None
            if not explicit:
                if not settings.talk or not settings.rate or not self.chat.enabled:
                    return None
                chance = 0.5 if "柚子" in line else 0.13
                if random.random() >= chance:
                    return None
                db.execute("DELETE FROM group_attempts WHERE stamp<=?", (now - 3600,))
                count, latest = db.execute(
                    "SELECT count(*), max(stamp) FROM group_attempts WHERE room=?", (room,)
                ).fetchone()
                if count >= settings.rate or (latest is not None and now - latest < 180):
                    return None
                if not db.execute(
                    "INSERT OR IGNORE INTO group_attempts VALUES(?,?,?)", (room, msg, now)
                ).rowcount:
                    return None
            selection = []
            if settings.memory:
                selection = list(
                    db.execute(
                        "SELECT kind,body,stamp FROM group_notes WHERE room=? AND stamp>? AND (kind='shared' OR player=?) AND body!=? ORDER BY stamp DESC LIMIT 6",
                        (room, now - MONTH, player, line),
                    )
                )
            speakers = {}
            dialogue = []
            for stamp, sender, _, body in log:
                if now - stamp < 900:
                    if sender == "柚子":
                        who = sender
                    elif sender == player:
                        who = "当前发言人"
                    else:
                        who = f"同学{speakers.setdefault(sender, len(speakers) + 1)}"
                    dialogue.append([who, body])
            notes = [
                [
                    datetime.fromtimestamp(stamp, CN).strftime("%m-%d %H:%M"),
                    "群里谈到" if kind == "shared" else "当前发言人提过",
                    body,
                ]
                for kind, body, stamp in selection
            ]
            transcript = json.dumps({"本群便签": notes, "最近发言": dialogue}, ensure_ascii=False)
            return Turn(
                transcript,
                settings.revision,
                settings.poke > 0 and random.randrange(100) < settings.poke,
                now,
            )

    async def respond(self, ctx, *, explicit=False):
        turn = await asyncio.to_thread(self.prepare, ctx, explicit=explicit)
        if turn is None:
            return None
        try:
            reply = await self.chat.group_reply(
                turn.transcript, spontaneous=not explicit, nudge=turn.nudge and not explicit
            )
        except ChatUnavailable as exc:
            if explicit:
                return str(exc)
            logger.info("跳过本次群聊接话，模型暂时不可用")
            return None
        if not reply.strip() or "[沉默]" in reply:
            return None
        if turn.nudge and not explicit:
            reply = "（柚子轻轻戳戳）" + reply.strip()[:200]
        return await asyncio.to_thread(self.finish, ctx, turn, reply.strip()[:400])

    def finish(self, ctx, turn, reply):
        room, now = _key(ctx.chat_id), time.time()
        with self.lock, closing(self.connect()) as db:
            if self.options(db, room).revision != turn.revision or now - turn.started > 150:
                return None
            if room in self.recent:
                self.recent[room].append((now, "柚子", "", reply))
            return reply


def register_group_commands(router, life):
    async def card(title, rows, *, group="群友与记忆", note="", actions=()):
        return await academy_card(title, rows, section=group, note=note, actions=actions)

    def may_manage(ctx):
        return ctx.member_role in ("admin", "owner")

    only_group = "这个指令按群分别设置，请在要使用的群里发送。"
    only_admin = "只有本群群主或管理员可以调整，请在群内发送，让 QQ 一并提供群身份。"

    @router.command(
        "群聊设置",
        "查看或启用本群偶尔接话和群聊记忆",
        category="管理",
        usage="/群聊设置 [开启|关闭]",
    )
    async def configure(req):
        ctx = req.context
        if ctx.chat_scope != "group":
            return only_group
        changes = None
        if req.argument:
            if req.argument not in ("开启", "关闭"):
                return "发送 /群聊设置 开启 或 /群聊设置 关闭。可用 /发言频次 和 /群记忆 单独调整。"
            if not may_manage(ctx):
                return only_admin
            value = int(req.argument == "开启")
            changes = {"talk": value, "memory": value}
        settings = await asyncio.to_thread(life.settings, ctx.chat_id, changes)
        return await card(
            "本群的柚子",
            [
                ("偶尔接话", "开启" if settings.talk else "关闭"),
                ("每小时上限", f"{settings.rate} 次，至少间隔 3 分钟"),
                ("群聊记忆", "开启" if settings.memory else "关闭"),
                ("文字戳戳", f"{settings.poke}%"),
            ],
            group="管理",
            note="群主在 QQ 机器人设置打开「接收所有消息」。本群开启后，柚子会参考最近聊天，群内约定与自己提过的事最多记 30 天。任何成员可用 /我的画像 退出。",
            actions=(QuickAction("群记忆", "/群记忆"), QuickAction("我的画像", "/我的画像")),
        )

    @router.command(
        "发言频次", "设置本群偶尔接话的每小时上限", category="管理", usage="/发言频次 0–12"
    )
    async def rate(req):
        ctx = req.context
        if ctx.chat_scope != "group":
            return only_group
        changes = None
        if req.argument:
            if not may_manage(ctx):
                return only_admin
            value = LEVELS.get(req.argument)
            if value is None and re.fullmatch(r"[0-9]{1,2}", req.argument):
                value = int(req.argument)
            if value is None or value > 12:
                return "可设每小时 0 到 12 次，或使用「安静」「偶尔」「活跃」。0 暂停偶尔接话。"
            changes = {"rate": value, "talk": int(bool(value))}
        s = await asyncio.to_thread(life.settings, ctx.chat_id, changes)
        return await card(
            "发言频次",
            [
                ("偶尔接话", "开启" if s.talk else "关闭"),
                ("每小时上限", f"{s.rate} 次"),
                ("两次间隔", "至少 3 分钟"),
            ],
            group="管理",
            note="每条普通群消息都有机会参与抽选。直接 @ 柚子聊天和使用命令随时可用。",
        )

    @router.command(
        "群戳频次", "设置偶尔接话时附带文字戳戳的概率", category="管理", usage="/群戳频次 0–100%"
    )
    async def nudge(req):
        ctx = req.context
        if ctx.chat_scope != "group":
            return only_group
        changes = None
        if req.argument:
            if not may_manage(ctx):
                return only_admin
            val = req.argument.removesuffix("%").removesuffix("％")
            if not re.fullmatch(r"[0-9]{1,3}", val) or int(val) > 100:
                return "发送 /群戳频次 0 到 /群戳频次 100%，0 为关闭。"
            changes = {"poke": int(val)}
        s = await asyncio.to_thread(life.settings, ctx.chat_id, changes)
        return await card(
            "群戳频次",
            [("接话时的概率", f"{s.poke}%"), ("表现形式", "柚子会在偶尔接话时附一句文字戳戳。")],
            group="管理",
            note="与接话共用频次上限；发送 /摸头 可以主动找柚子互动。",
        )

    @router.command(
        "群记忆",
        "看看柚子记得这个群聊过什么",
        category="群友与记忆",
        usage="/群记忆 [开启|关闭|清除]",
    )
    async def collective(req):
        ctx = req.context
        if ctx.chat_scope != "group":
            return only_group
        if req.argument:
            if not may_manage(ctx):
                return only_admin
            if req.argument not in ("开启", "关闭", "清除"):
                return "发送 /群记忆 查看。群管理员可发送 /群记忆 开启、关闭或清除。"
            if req.argument == "清除":
                await asyncio.to_thread(life.settings, ctx.chat_id, erase=True)
                return "已清空本群的群聊便签、成员画像与最近聊天片段。"
            await asyncio.to_thread(
                life.settings, ctx.chat_id, {"memory": int(req.argument == "开启")}
            )
        s, _, notes = await asyncio.to_thread(life.notes, ctx, personal=False)
        topics = await asyncio.to_thread(life.topics, ctx)
        rows = [("常聊的话题", "、".join(topics))] if topics else []
        rows.extend(
            (datetime.fromtimestamp(stamp, CN).strftime("%m 月 %d 日"), body)
            for stamp, body in notes[:5]
        )
        return await card(
            "柚子对本群的印象",
            rows
            or [
                (
                    "群聊便签",
                    "还没记下大家的共同话题。"
                    if s.memory
                    else "群记忆已关闭。由群主或管理员发送 /群记忆 开启。",
                )
            ],
            note="记录来自本群最近 30 天里提过的约定与共同话题。成员可以在 /我的画像 管理自己的发言。",
        )

    @router.command(
        "我的画像",
        "查看本人在本群提过的事情和偏好",
        category="群友与记忆",
        usage="/我的画像 [清除|退出|恢复]",
    )
    async def portrait(req):
        ctx = req.context
        if ctx.chat_scope != "group":
            return only_group
        if req.argument:
            if req.argument not in ("退出", "恢复", "清除"):
                return "可以查看自己的画像，或发送 /我的画像 清除、退出、恢复。"
            await asyncio.to_thread(life.forget_person, ctx, req.argument)
            return {
                "退出": "已移除你在本群的便签和最近片段。以后你的普通发言会被跳过。",
                "恢复": "已恢复参与。本群开启相应功能后，从你的下一条发言开始。",
                "清除": "已清除你在本群留下的便签和最近聊天片段。",
            }[req.argument]
        s, excluded, notes = await asyncio.to_thread(life.notes, ctx, personal=True)
        status = (
            "你已退出，发送 /我的画像 恢复 可重新参与。"
            if excluded
            else "还没记下你说过的事情。"
            if s.memory
            else "本群尚未开启群记忆。"
        )
        rows = [
            (datetime.fromtimestamp(stamp, CN).strftime("%m 月 %d 日"), body)
            for stamp, body in notes
        ]
        return await card(
            "柚子记得的你",
            rows or [("你的便签", status)],
            note="这里仅展示你本人在本群说过的话。发送 /我的画像 清除 删除记录，/我的画像 退出 停止参与。",
        )

    @router.command("更新日志", "查看本版更新记录（Bot 主人）", category="管理")
    async def changelog(req):
        ctx = req.context
        if req.argument == "身份" and ctx.chat_scope == "c2c":
            return f"你的私聊 OpenID 为 {ctx.user_id}。部署者可将它添加到 BOT_OWNER_OPENIDS。"
        owners = os.getenv("BOT_OWNER_OPENIDS", "").replace(",", " ").split()
        if ctx.user_id not in owners:
            return (
                "更新日志仅供 Bot 主人查看。部署者可在私聊发送 /更新日志 身份 获取自己的 OpenID。"
            )
        return await card(
            "柚子的更新笔记",
            [
                ("群聊", "支持偶尔接话、群便签、个人画像和按群调整频次。"),
                ("校园", "可签到、领礼物、送礼、摸头与占卜。"),
                ("舞萌", "增加 QQ 商城表情配字、旧制 B40、底分趋势和日服等级表。"),
            ],
            group="管理",
        )
