"""Chat-local word games and a leaderboard for results shared by hardle.org."""

from __future__ import annotations

import asyncio
import hashlib
import io
import random
import re
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw

from .domain import BotReply, QuickAction
from .services.academy import INK, MUTED, PAPER, RULE, draw_header
from .services.b50_image import _font
from .services.community import CN
from .services.result_card import academy_card

# Only familiar words become solutions. The larger CMU dictionary is for submissions.
_COMMON_WORDS = """
apple beach berry blade bloom board brave bread brick brush cabin candy cedar
charm chase chord cloud coast coral crane cream crisp crown dance dream dress drift
drink drive eager eagle earth ember event fairy feast field flame flare flash fleet
float floor flute focus forge frame fresh frost fruit ghost giant glass globe grace
grain grand grape grass green grove habit happy heart honey horse hotel house human
ivory jelly joint judge juice kneel knife label laser later laugh layer lemon light
lilac limit linen liver lodge logic loose lotus lucky lunar lyric magic major maple
march match metal meter minor model month motor mount mouse movie music night noise
north novel nurse ocean offer olive onset opera orbit order organ otter ounce outer
paint panel paper party pasta patch pause peach pearl pedal phase phone photo piano
pilot pitch pixel place plain plane plant plate plaza point polar porch pound power
press price pride prime print prize proof prose proud pulse punch pupil purse queen
quest queue quick quiet quilt radar radio rainy raise rally ranch range rapid ratio
raven reach ready realm rebel relay reply reset rhyme rider ridge right rinse rival
river robin robot rocky rogue roman roast rough round route royal ruler rural rusty
saint salad scale scene scent score scout sense serve seven shade shake shape share
shark sharp sheep sheer sheet shelf shell shift shine shirt shock shore short shout
sight silly since skill skirt skull slate sleep slice slide slope sloth small smart
smell smile smoke snack snail snake snare solar solid solve sound south
space spare spark speak spear speed spell spend spice spike spine split spoil spoke
sport spray squad stack staff stage stain stair stake stale stalk stall stamp
stand stare stark start state stave steak steal steam steel steep steer stern stick
stiff still sting stock stone stood stool storm story stove strap straw strip strum
stuck study stuff stump stunt style sugar suite sunny super surge sushi swamp swarm
sweep sweet swell swept swift swing swirl sword table taken tango taper tasty
teach tempo tenor tense tenth thank theft theme thick thief think third thorn those
three threw throw thumb tiger tight timer tired title toast today token tonal tonic
tooth topic torch torso total touch tough towel tower toxic trace track trade trail
train trait trash tread treat trend trial tribe trick tried troop trout truce truck
truly trunk trust truth tulip tummy tuner turbo tutor tweet twice twine twist tying
ultra uncle under unify union unite unity until upper upset urban usage usher usual
utter vague valid value valve vapor vault venue verse video vigil vigor villa vinyl
viola viper viral visit vital vivid vocal vodka vogue voice voter vowel wacky wafer
wager wagon waist waltz waste watch water weary weave wedge weigh weird whale wheat
wheel where which while whirl whisk white whole whose widen wider widow width wield
winch windy witch witty world worry worse worst worth would wound woven wrist write
wrong wrote yacht yearn yeast yield young youth zebra zippy
"""
SOLUTIONS = tuple(_COMMON_WORDS.split())

CLUES = {
    "简单": (
        ("beat", "心跳或音乐里可以数的拍子"),
        ("drum", "用鼓槌或手敲打的乐器"),
        ("song", "可以跟着唱的一首作品"),
        ("star", "夜空中自己发光的天体"),
        ("blue", "晴朗天空的颜色"),
        ("rain", "从云中落下的水滴"),
        ("moon", "地球唯一的天然卫星"),
        ("snow", "冬季可能从云中飘下的冰晶"),
    ),
    "普通": (
        ("chord", "同时奏响的一组乐音"),
        ("tempo", "乐曲演奏速度的术语"),
        ("piano", "有八十八个琴键的常见乐器"),
        ("scale", "按音高顺序排列的一组音"),
        ("waltz", "典型节拍为三拍子的舞曲"),
        ("choir", "一起演唱多声部作品的团体"),
        ("viola", "音域处于小提琴和大提琴之间的弦乐器"),
        ("lyric", "表示歌词时常用复数形式"),
    ),
    "困难": (
        ("octave", "相隔十二个半音的音程"),
        ("cadence", "乐句或乐段收束时的终止式"),
        ("timbre", "相同音高与响度下仍能区别乐器的声音特性"),
        ("legato", "乐谱中要求音与音连贯衔接的意大利术语"),
        ("arpeggio", "把和弦音依次快速弹出的奏法"),
        ("ostinato", "反复出现的固定音型"),
        ("staccato", "与连奏相对的断奏术语"),
        ("downbeat", "一小节开头的第一拍"),
    ),
}

VTUBERS = (
    (
        "阿萨 Aza",
        "VirtuaReal",
        "想听中文虚拟歌手，可以从他的原创曲和演唱会开始。",
        "https://vrp.moe/zh/talents/aza",
    ),
    (
        "星街すいせい",
        "hololive",
        "喜欢流行乐的话，主页推荐的《NEXT COLOR PLANET》适合当第一首。",
        "https://hololive.hololivepro.com/en/talents/hoshimachi-suisei/",
    ),
    (
        "AZKi",
        "hololive",
        "从原创曲《petrichor》或《canopus》开始听。",
        "https://hololive.hololivepro.com/en/talents/azki/",
    ),
    (
        "レヴィ・エリファ",
        "にじさんじ",
        "这位半人半魔的主播在主页放了介绍视频和频道入口。",
        "https://www.nijisanji.jp/talents/l/levi-elipha",
    ),
)

TILES = {"exact": "#26745B", "present": "#A56712", "absent": "#60657B"}
HELP = "发送 #单词 提交答案，或发送 /wordle 结束。本局限时 10 分钟。"


@cache
def dictionary() -> set[str]:
    text = Path(__file__).with_name("wordle_dictionary.txt").read_text(encoding="utf-8")
    return {word for line in text.splitlines() if not line.startswith("#") for word in line.split()}


def marks(answer, guess):
    available = Counter(right for right, left in zip(answer, guess) if right != left)
    result = []
    for right, left in zip(answer, guess):
        if right == left:
            result.append("exact")
        elif available[left]:
            available[left] -= 1
            result.append("present")
        else:
            result.append("absent")
    return result


def board_image(rows, status):
    canvas = Image.new("RGB", (640, 800), PAPER)
    draw_header(canvas, "Wordle", f"校园游戏　/　{len(rows)} 次提交", 0.74)
    draw = ImageDraw.Draw(canvas)
    for iy in range(6):
        for ix in range(5):
            x, y = 79 + ix * 98, 149 + iy * 89
            filled = iy < len(rows)
            color = TILES[rows[iy][1][ix]] if filled else RULE
            draw.rounded_rectangle((x, y, x + 87, y + 80), radius=12, fill=color)
            if filled:
                draw.text(
                    (x + 43, y + 39),
                    rows[iy][0][ix].upper(),
                    font=_font(39, bold=True),
                    fill="white",
                    anchor="mm",
                )
    draw.text((320, 703), status, font=_font(25, bold=True), fill=INK, anchor="mm")
    draw.text(
        (320, 756),
        "绿色 · 位置正确      金色 · 位置不同      灰色 · 未匹配",
        font=_font(20),
        fill=MUTED,
        anchor="mm",
    )
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


@dataclass
class Round:
    mode: str
    answer: str
    expiry: float
    clue: str = ""
    guesses: list[str] = field(default_factory=list)


def room_key(ctx):
    return (ctx.chat_scope, ctx.user_id if ctx.chat_scope in {"c2c", "direct"} else ctx.chat_id)


def _share_date(stamp, today):
    stamp = re.sub(r"[年月.\-/]", " ", stamp.rstrip("日")).strip()
    parts = stamp.split()
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    a, b, c = map(int, parts)
    options = [(a, b, c)] if len(parts[0]) == 4 else [(c, a, b), (c, b, a)]
    valid = []
    for parts in options:
        try:
            valid.append(date(*parts))
        except ValueError:
            pass
    # toLocaleDateString can emit either MM/DD/YYYY or DD/MM/YYYY.
    recent = [v for v in valid if today - timedelta(days=1) <= v <= today]
    return max(recent) if recent else None


def parse_share(text, today):
    lines = [line.strip() for line in text.replace("\ufe0f", "").splitlines() if line.strip()]
    if (
        len(lines) < 4
        or lines[0].lower() != "hardle"
        or lines[-1].lower() != "play #hardle on hardle.org"
    ):
        return None
    score = re.fullmatch(r"(.{6,20})\s*\|\s*(10|[1-9]|X)/10", lines[1], re.IGNORECASE)
    if not score:
        return None
    day = _share_date(score[1].strip(), today)
    solved = score[2].upper() != "X"
    attempts = int(score[2]) if solved else 10
    rows = lines[2:-1]
    if not day or attempts != len(rows):
        return None
    if not all(re.fullmatch(r"[🟨🟩⬛]{5}\s*(?:[0-5]\u20e3){0,2}", row) for row in rows):
        return None
    if any(row.startswith("🟩" * 5) for row in rows[:-1]) or solved != rows[-1].startswith(
        "🟩" * 5
    ):
        return None
    return day.isoformat(), attempts, solved


def hardle_rows(community, room, member, query, today, result):
    with closing(community.connect()) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS hardle_scores (
                room TEXT NOT NULL, player TEXT NOT NULL, day TEXT NOT NULL,
                attempts INTEGER NOT NULL, solved INTEGER NOT NULL,
                PRIMARY KEY(room,player,day))""")
        added = None
        if result:
            added = bool(
                db.execute(
                    "INSERT OR IGNORE INTO hardle_scores VALUES(?,?,?,?,?)", (room, member, *result)
                ).rowcount
            )
        if query == "all":
            rows = db.execute(
                """
                SELECT player, sum(solved), count(*), sum(attempts)
                FROM hardle_scores WHERE room=? GROUP BY player
                ORDER BY sum(solved) DESC, 1.0*sum(attempts)/count(*) ASC, player LIMIT 15""",
                (room,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT player,solved,1,attempts FROM hardle_scores WHERE room=? AND day=?
                ORDER BY solved DESC, attempts, player LIMIT 15""",
                (room, today),
            ).fetchall()
        return added, rows


def register_party_games(router, community):
    rounds = {}

    def current(ctx):
        key = room_key(ctx)
        game = rounds.get(key)
        if game and game.expiry <= time.monotonic():
            del rounds[key]
            return None
        return game

    async def show(game, status=""):
        if game.mode == "Wordle":
            rows = [(guess, marks(game.answer, guess)) for guess in game.guesses]
            instruction = "发送 /wordle 可以再开一局。" if status else HELP
            status = status or "请猜一个五字母英文单词"
            legend = {"exact": "绿", "present": "黄", "absent": "灰"}
            text = "\n".join(
                f"{word.upper()}　{''.join(legend[m] for m in colors)}" for word, colors in rows
            )
            return BotReply(
                f"Wordle · {status}\n{text}\n{instruction}".strip(),
                await asyncio.to_thread(board_image, rows, status),
                instruction,
            )
        reveal = min(len(game.guesses), len(game.answer) - 1)
        pattern = " ".join(game.answer[:reveal].upper() + "_" * (len(game.answer) - reveal))
        return await academy_card(
            "猜单词",
            [
                ("词义", game.clue),
                ("提示", pattern),
                ("已猜", "、".join(game.guesses) or "尚未作答"),
            ],
            section="娱乐",
            note=status
            or "发送 #单词 作答。每次答错会揭开一个字母，共有六次机会，本局限时 10 分钟。",
        )

    def begin(ctx, mode, pair):
        now = time.monotonic()
        for key in [key for key, game in rounds.items() if game.expiry <= now]:
            del rounds[key]
        game = Round(mode, pair[0], now + 600, pair[1])
        rounds[room_key(ctx)] = game
        return game

    async def play(req, mode):
        ctx = req.context
        arg = req.argument.strip().casefold()
        game = current(ctx)
        if ctx.content.strip().startswith("#"):
            return await submit(ctx, arg)
        if arg in {"结束", "放弃", "end"}:
            if not game:
                return "当前会话没有进行中的猜词游戏。"
            del rounds[room_key(ctx)]
            return await show(game, f"本局已结束，答案是 {game.answer.upper()}")
        if game:
            if arg and arg not in CLUES:
                return await submit(ctx, arg)
            return await show(game)
        if mode == "Wordle":
            if arg:
                return "先发送 /wordle 开局，再用 #单词 作答。"
            game = begin(ctx, mode, (random.choice(SOLUTIONS), ""))
        else:
            if arg not in CLUES:
                return await academy_card(
                    "猜单词 · 选择难度",
                    [(name, f"/猜词 {name}") for name in CLUES],
                    section="娱乐",
                    actions=tuple(QuickAction(name, f"/猜词 {name}") for name in CLUES),
                )
            game = begin(ctx, mode, random.choice(CLUES[arg]))
        return await show(game)

    async def submit(ctx, guess):
        game = current(ctx)
        if not game:
            return "本局已结束。可发送 /wordle 或 /猜词 再开一局。"
        guess = guess.lower()
        if not re.fullmatch(rf"[a-z]{{{len(game.answer)}}}", guess):
            return f"请输入 {len(game.answer)} 个英文字母，本次不消耗机会。"
        if game.mode == "Wordle" and guess not in dictionary():
            return "词典未收录这个五字母词，本次不消耗机会。"
        if guess in game.guesses:
            return "本局已经猜过这个词，本次不消耗机会。"
        game.guesses.append(guess)
        if guess == game.answer or len(game.guesses) == 6:
            del rounds[room_key(ctx)]
            outcome = "猜对了" if guess == game.answer else "机会已用完"
            return await show(game, f"{outcome}！答案是 {game.answer.upper()}")
        return await show(game)

    @router.command("wordle", "六次机会猜出五字母英文单词", category="娱乐", usage="/wordle")
    async def wordle(req):
        return await play(req, "Wordle")

    @router.command(
        "wordguess",
        "根据释义和逐步揭开的字母猜单词",
        aliases=("猜词", "猜单词"),
        category="娱乐",
        usage="/猜词 困难",
    )
    async def wordguess(req):
        return await play(req, "猜单词")

    @router.command(
        "guesssong",
        "开始舞萌猜歌，支持原猜歌指令的范围筛选",
        category="娱乐",
        usage="/guesssong maimai [范围]",
    )
    async def guesssong(req):
        game, _, filters = req.argument.strip().partition(" ")
        if game and game.casefold() not in {"maimai", "maidx", "舞萌", "舞萌dx"}:
            return (
                "当前可选舞萌 DX 曲库。发送 /guesssong maimai，或 /guesssong maimai 14+ 限定等级。"
            )
        return await router.dispatch(replace(req.context, content=f"/guess {filters}".rstrip()))

    @router.command(
        "hardle",
        "登记原站分享的成绩，查看本群今日榜或总榜",
        aliases=("#hardle",),
        category="娱乐",
        usage="#hardle [all]",
    )
    async def hardle(req):
        ctx = req.context
        if ctx.chat_scope not in {"group", "channel"}:
            return "请在群里分享 Hardle 成绩，或发送 #hardle 查看本群榜单。"
        today = datetime.now(CN).date()
        share = ctx.content.strip()
        result = parse_share(share, today) if "\n" in share else None
        if "\n" in share and result is None:
            return "未识别到有效成绩。请复制 hardle.org 今天或昨天的完整分享，保留日期、方格和末行网址。"
        query = req.argument.strip().lower()
        if not result and query not in {"", "all"}:
            return "发送 #hardle 查看今日榜，#hardle all 查看总榜。"
        room = hashlib.sha256(repr(room_key(ctx)).encode()).hexdigest()
        player = hashlib.sha256(f"{room}:{ctx.user_id}".encode()).hexdigest()[:12]
        day = result[0] if result else today.isoformat()
        added, records = await asyncio.to_thread(
            hardle_rows, community, room, player, query, day, result
        )
        rows = []
        for index, (nickname, wins, count, attempts) in enumerate(records, 1):
            who = ("你 · " if nickname == player else "") + nickname[:6].upper()
            detail = (
                f"通关 {wins}/{count} · 平均 {attempts / count:.1f} 手"
                if query == "all"
                else f"{attempts} / 10"
                if wins
                else "未解出"
            )
            rows.append((f"{index}. {who}", detail))
        note = ("已收录本次成绩。" if added else "这一天的成绩已经登记。") if result else ""
        return await academy_card(
            "Hardle · " + ("本群总榜" if query == "all" else f"{day} 群榜"),
            rows or [("", "本群还没有成绩。")],
            section="娱乐",
            note=note + "玩家代号仅在本群使用。复制原站的完整分享即可参与。",
            actions=(QuickAction("打开 Hardle", url="https://hardle.org/"),),
        )

    @router.command(
        "vtuber",
        "推荐一位虚拟主播和入门作品",
        aliases=("看什么v", "看什么Vtuber"),
        category="娱乐",
        usage="看什么V",
    )
    async def vtuber(req):
        name, group, why, url = random.choice(VTUBERS)
        return await academy_card(
            name,
            [("所属", group), ("从这里入门", why)],
            section="娱乐",
            followup=url,
            actions=(QuickAction("打开主页", url=url), QuickAction("再来一位", "/vtuber")),
        )

    router.pattern(r"Hardle\s*\n[\s\S]{1,800}", "hardle")
    router.pattern(
        r"#([a-z]{1,20})", "wordguess", lambda m: m[1], when=lambda ctx: bool(current(ctx))
    )
