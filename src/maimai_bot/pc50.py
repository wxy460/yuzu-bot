"""Rank charts by a player's cumulative play counts, including personal JSON exports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import httpx

from .domain import BotReply, QuickAction
from .services.b50_image import B50RenderError
from .services.cosmetics import CosmeticError
from .services.divingfish import NotBound, ScoreServiceError, chart_play_count
from .services.gameplay import all_records
from .services.lxns_scores import ScoreSources, score_identity
from .services.qq_attachments import MAX_ATTACHMENT_BYTES, download_qq_json
from .services.song_catalog import DIFFICULTY_NAMES, SongCatalogError

logger = logging.getLogger(__name__)
ChartKey = tuple[int, str, int]
MAX_ROWS = 20_000
IMPORT_HINT = (
    "发送 /pc50 导入，再在同一会话发一份含逐谱游玩次数的 JSON 文件。"
    "也可以把 JSON 直接跟在 /pc50 导入 后。只需谱面编号、类型、难度和次数，无需账号令牌或二维码。"
)


def uint(value, limit=2_147_483_647):
    if type(value) is int:
        return value if 0 <= value <= limit else None
    if isinstance(value, str) and len(value) <= 10 and value.isascii() and value.isdecimal():
        number = int(value)
        return number if number <= limit else None
    return None


def chart_key(row) -> ChartKey | None:
    native = "musicId" in row
    song_id = uint(row.get("musicId") if native else row.get("song_id", row.get("id")))
    difficulty = uint(
        row.get("level") if native else row.get("level_index", row.get("difficulty")), 4
    )
    if not song_id or difficulty is None:
        return None
    kind = str(row.get("type", "")).strip().lower()
    if not kind and native:
        kind = "utage" if song_id >= 100_000 else "dx" if song_id >= 10_000 else "sd"
    if kind in ("sd", "standard", "标准"):
        return song_id, "SD", difficulty
    if kind == "dx":
        return (song_id - 10_000 if 10_000 < song_id < 20_000 else song_id), "DX", difficulty
    if kind == "utage":
        return song_id, "UTAGE", difficulty
    return None


class CountFormatError(ValueError):
    """The attachment does not contain a valid per-chart snapshot."""


def export_counts(raw: str | bytes) -> dict[ChartKey, int]:
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise CountFormatError("文件超过 12 MB。请导出仅含谱面游玩记录的 JSON。")
    try:
        data = json.loads(raw)
    except (UnicodeError, RecursionError, json.JSONDecodeError, ValueError) as exc:
        raise CountFormatError("文件不是可读取的 JSON。") from exc
    if isinstance(data, dict):
        data = next(
            (
                data[k]
                for k in ("userMusicList", "userMusicDetailList", "records", "scores")
                if k in data
            ),
            None,
        )
    if not isinstance(data, list) or len(data) > MAX_ROWS:
        raise CountFormatError(
            "无法识别谱面列表。支持 userMusicList、userMusicDetailList 或 records 数组。"
        )
    counts = {}
    seen = 0
    for part in data:
        rows = part.get("userMusicDetailList", [part]) if isinstance(part, dict) else None
        if not isinstance(rows, list):
            raise CountFormatError("成绩文件中的谱面列表格式错误。")
        seen += len(rows)
        if seen > MAX_ROWS:
            raise CountFormatError("每次导入最多支持 20000 条谱面记录。")
        for row in rows:
            if not isinstance(row, dict):
                raise CountFormatError("成绩行格式错误。")
            if "playCount" not in row and "play_count" not in row:
                continue
            count = uint(chart_play_count(row))
            key = chart_key(row)
            if key is None or count is None:
                raise CountFormatError(
                    "含次数的一行缺少有效的谱面编号、类型、难度或非负整数次数。原有数据已保留。"
                )
            counts[key] = max(count, counts.get(key, 0))
    if not counts:
        raise CountFormatError(
            "这份文件没有逐谱 playCount 或 play_count。请使用带游玩次数的本人游戏成绩备份。"
        )
    return counts


class CountStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        db = sqlite3.connect(self.path, timeout=5)
        db.execute(
            "CREATE TABLE IF NOT EXISTS counts(subject TEXT,song INTEGER,kind TEXT,difficulty INTEGER,n INTEGER,PRIMARY KEY(subject,song,kind,difficulty))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS imports(subject TEXT PRIMARY KEY, ticket TEXT NOT NULL)"
        )
        return db

    def begin_import(self, subject):
        ticket = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)", (subject, ticket))
        return ticket

    def replace(self, subject, counts, ticket=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if ticket is None:
                db.execute("DELETE FROM imports WHERE subject=?", (subject,))
            elif not db.execute(
                "DELETE FROM imports WHERE subject=? AND ticket=?", (subject, ticket)
            ).rowcount:
                return False
            db.execute("DELETE FROM counts WHERE subject=?", (subject,))
            db.executemany(
                "INSERT INTO counts VALUES(?,?,?,?,?)",
                ((subject, *key, n) for key, n in counts.items()),
            )
            return True

    def load(self, subject):
        if not self.path.exists():
            return {}
        with closing(self.connect()) as db:
            return {
                (song, kind, diff): n
                for song, kind, diff, n in db.execute(
                    "SELECT song,kind,difficulty,n FROM counts WHERE subject=?", (subject,)
                )
            }


def rank_counts(records, imported):
    scores_by_key = {}
    counts = dict(imported)
    for row in all_records(records) if records else ():
        key = chart_key(row)
        if key is None:
            continue
        scores_by_key[key] = row
        n = uint(chart_play_count(row))
        if n is not None:
            # Both values are lifetime counts for this chart. Never sum snapshots.
            counts[key] = max(n, counts.get(key, 0))
    positive = sorted((key for key, n in counts.items() if n), key=lambda key: (-counts[key], key))
    rows = []
    for key in positive[:50]:
        song, kind, diff = key
        rows.append(
            {
                **scores_by_key.get(key, {}),
                "song_id": song,
                "type": kind,
                "level_index": diff,
                "level_label": "宴" if kind == "UTAGE" else DIFFICULTY_NAMES[diff],
                "play_count": counts[key],
            }
        )
    return rows, bool(counts)


def register_pc50(router, scores, catalog, cosmetics, images, community):
    waiting = {}

    def subject(ctx):
        owner = scores.history_key(ctx) if isinstance(scores, ScoreSources) else ctx.user_id
        return hashlib.sha256(
            json.dumps([ctx.chat_scope, ctx.chat_id, ctx.user_id, owner]).encode()
        ).hexdigest()

    def store():
        return CountStore(community.database.with_name("pc50.sqlite3"))

    async def show(ctx, owner, snapshot, notice=""):
        player = None
        error = ""
        try:
            player = await scores.player_records(score_identity(scores, ctx))
        except NotBound:
            error = "当前账号尚未绑定查分器。"
        except ScoreServiceError as exc:
            error = str(exc)
        rows, known = rank_counts(player, snapshot)
        if not rows:
            if known:
                return notice + "可读取的谱面游玩次数均为 0，目前没有已游玩的谱面可绘制。"
            reason = error or "当前查分器没有提供逐谱游玩次数。"
            return BotReply(
                text=reason + "\n" + IMPORT_HINT,
                quick_actions=(QuickAction("导入次数", "/pc50 导入"),),
            )
        if any(not row.get("title") or not row.get("level") for row in rows):
            try:
                charts = {
                    (c.song_id, c.type_label, c.difficulty): c
                    for song in await catalog.songs()
                    for c in song.charts
                }
            except SongCatalogError:
                charts = {}
            for row in rows:
                key = (row["song_id"], row["type"], row["level_index"])
                chart = charts.get(key)
                if chart:
                    row.update(title=chart.title, level=chart.level, ds=chart.constant)
                elif not row.get("title"):
                    row["title"] = f"曲目 ID {key[0]}"
        try:
            appearance = await cosmetics.profile(ctx.user_id)
        except CosmeticError:
            appearance = None
        summary = (
            notice
            + f"游玩次数 PC50 · {len(rows)} 张谱面"
            + ("（含本人导入的次数）" if snapshot else "")
        )
        text = (
            summary
            + "\n"
            + "\n".join(
                f"{i}. {row['title']} [{row['type']} {row['level_label']}] · {row['play_count']} 次"
                for i, row in enumerate(rows, 1)
            )
        )
        try:
            png = await images.render_pc50(player, rows, appearance, avatar_url=ctx.avatar_url)
        except B50RenderError as exc:
            logger.warning("PC50 图片生成失败：%s", type(exc).__name__)
            png = None
        if subject(ctx) != owner:
            return "查分绑定在生成图片期间发生了变更，请重新发送 /pc50。"
        return BotReply(text=text, image_png=png, followup_text=notice.strip() if png else "")

    async def handle(ctx, arg):
        owner = subject(ctx)
        conversation = (ctx.chat_scope, ctx.chat_id, ctx.user_id)
        parts = arg.split(maxsplit=1)
        action = parts[0] if parts else ""
        payload = parts[1] if len(parts) > 1 else ""
        if action == "取消":
            waiting.pop(conversation, None)
            return "已取消等待 PC50 文件。"
        if action == "清除" and community is not None:
            waiting.pop(conversation, None)
            await asyncio.to_thread(store().replace, owner, {})
            return "已清除当前身份导入的谱面游玩次数。"
        if action not in ("", "导入"):
            return "发送 /pc50 查看游玩次数榜，/pc50 导入 更新自己的次数，/pc50 清除 删除导入数据。"
        if action == "导入":
            if community is None:
                return "本实例尚未配置个人次数存储。"
            if not payload and not ctx.json_urls:
                now = time.monotonic()
                for key, (deadline, _) in list(waiting.items()):
                    if deadline <= now:
                        del waiting[key]
                waiting[conversation] = (now + 180, owner)
                return "请在 3 分钟内发送本人带次数的 JSON 文件。成功导入会替换这份个人次数快照并立即出图。发送 /pc50 取消 可退出。"
            waiting.pop(conversation, None)
            if len(ctx.json_urls) > 1 or (payload and ctx.json_urls):
                return "请每次只发送一份成绩 JSON。"
            storage = store()
            ticket = await asyncio.to_thread(storage.begin_import, owner)
            try:
                raw = payload or await download_qq_json(community.http, ctx.json_urls[0])
                snapshot = await asyncio.to_thread(export_counts, raw)
            except ValueError as exc:
                return str(exc)
            except httpx.HTTPError:
                return "读取 QQ 文件失败，请重新发送 /pc50 导入 和原文件。"
            if subject(ctx) != owner:
                return "绑定已变更，请重新导入。"
            if not await asyncio.to_thread(storage.replace, owner, snapshot, ticket):
                return "已有更新的导入或清除操作，这份较早的文件已跳过。"
            return await show(ctx, owner, snapshot, f"已导入 {len(snapshot)} 张谱面的次数。\n")
        snapshot = await asyncio.to_thread(store().load, owner) if community is not None else {}
        return await show(ctx, owner, snapshot)

    @router.command(
        "pc50",
        "按逐谱游玩次数绘制个人 Top 50，支持导入游戏成绩 JSON",
        aliases=("游玩排行50",),
        category="成绩",
        usage="/pc50 · /pc50 导入",
    )
    async def pc50(req):
        try:
            return await handle(req.context, req.argument)
        except ScoreServiceError as exc:
            return str(exc)

    def awaiting(ctx):
        return bool(
            ctx.json_urls
            and not router.recognizes_command(ctx.content)
            and waiting.get((ctx.chat_scope, ctx.chat_id, ctx.user_id), (0, ""))[0]
            > time.monotonic()
        )

    async def attachment(ctx):
        try:
            if waiting.pop((ctx.chat_scope, ctx.chat_id, ctx.user_id))[1] != subject(ctx):
                return "查分绑定已改变，请重新发送 /pc50 导入。"
            return await handle(ctx, "导入")
        except ScoreServiceError as exc:
            return str(exc)

    router.add_attachment_handler(attachment, awaiting)
