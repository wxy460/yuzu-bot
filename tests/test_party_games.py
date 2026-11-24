from dataclasses import replace
from datetime import date, datetime, timedelta

from maimai_bot import party_games
from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.router import CommandRouter
from maimai_bot.services.community import CN, CommunityData


def setup_games(path):
    async def fallback(ctx):
        return "ordinary"

    router = CommandRouter(fallback)

    @router.command("guess", "existing song guessing")
    async def music(req):
        return "舞萌猜歌 " + req.argument

    party_games.register_party_games(router, CommunityData(None, path))
    return router, MessageContext("a", "one", "group", "", "m")


def test_wordle_duplicate_feedback_and_accepted_solutions():
    assert party_games.marks("apple", "allee") == ["exact", "present", "absent", "absent", "exact"]
    assert len(party_games.dictionary()) > 10000
    assert set(party_games.SOLUTIONS) <= party_games.dictionary()


async def test_word_round_scope_timeout_and_invalid_input(tmp_path, monkeypatch):
    router, base = setup_games(tmp_path / "community.sqlite")
    clock = [10]
    # Avoid replacing the event loop's own clock.
    monkeypatch.setattr(party_games, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))
    monkeypatch.setattr(party_games.random, "choice", lambda seq: seq[0])

    def ctx(text, **overrides):
        return replace(base, content=text, **overrides)

    assert not router.accepts_context(ctx("#crane"))
    start = await router.dispatch(ctx("/wordle"))
    assert start.image_png.startswith(b"\x89PNG")
    assert router.accepts_context(ctx("#crane", user_id="b"))
    assert not router.accepts_context(ctx("#crane", chat_id="two"))
    assert "不消耗" in await router.dispatch(ctx("#zzzzz"))
    assert "不消耗" in await router.dispatch(ctx("#end"))
    first_guess = await router.dispatch(ctx("#crane", user_id="b"))
    assert "CRANE" in first_guess.text
    assert "猜对" in (await router.dispatch(ctx("#apple"))).text
    assert not router.accepts_context(ctx("#apple"))

    await router.dispatch(ctx("/猜词 困难", chat_scope="c2c"))
    assert router.accepts_context(ctx("#octave", chat_scope="c2c"))
    assert not router.accepts_context(ctx("#octave", chat_scope="c2c", user_id="b"))
    missed = await router.dispatch(ctx("#stream", chat_scope="c2c"))
    assert "O _ _ _ _ _" in missed.text
    clock[0] += 601
    assert not router.accepts_context(ctx("#octave", chat_scope="c2c"))
    assert await router.dispatch(ctx("#octave", chat_scope="c2c")) == "ordinary"


def share(day, attempts=2, fail=False):
    lines = ["🟨⬛⬛⬛🟨 1️⃣1️⃣"] * (attempts - 1)
    lines.append("⬛⬛⬛⬛⬛ 0️⃣0️⃣" if fail else "🟩🟩🟩🟩🟩 5️⃣0️⃣")
    head = f"Hardle \n{day} | {'X' if fail else attempts}/10"
    return "\n".join([head, *lines, "Play #hardle on hardle.org"])


def test_original_hardle_format_and_validation():
    today = date(2026, 11, 25)
    for day in ("2026/11/25", "11/25/2026", "25.11.2026", "2026年11月25日"):
        assert party_games.parse_share(share(day), today) == (today.isoformat(), 2, True)
    assert party_games.parse_share(share("2026/11/24", 10, True), today) == (
        "2026-11-24",
        10,
        False,
    )
    assert party_games.parse_share(share("2026/11/23"), today) is None
    assert party_games.parse_share(share("2026/11/25").replace("2/10", "1/10"), today) is None
    assert party_games.parse_share(share("2026/11/25").replace("🟩", "⬛"), today) is None


async def test_hardle_persistence_and_group_isolation(tmp_path):
    db = tmp_path / "scores.sqlite"
    router, ctx = setup_games(db)
    today = datetime.now(CN).date()

    async def run(text, **updates):
        return await router.dispatch(replace(ctx, content=text, **updates))

    assert "已收录" in (await run(share(today))).text
    assert "已经登记" in (await run(share(today, 1))).text
    assert "2 / 10" in (await run("#hardle")).text
    assert "没有成绩" in (await run("#hardle all", chat_id="other")).text
    assert "请在群里" in await run("#hardle", chat_scope="c2c")
    assert "已收录" in (await run(share(today - timedelta(days=1), 3))).text
    router, ctx = setup_games(db)
    assert "通关 2/2 · 平均 2.5 手" in (await run("#hardle all")).text
    assert "1 / 10" not in (await run("#hardle")).text


async def test_music_compatibility_and_vtuber_links(tmp_path):
    router, ctx = setup_games(tmp_path / "data.sqlite")
    assert await router.dispatch(replace(ctx, content="/guesssong maimai 14+")) == "舞萌猜歌 14+"
    assert "当前可选舞萌" in await router.dispatch(replace(ctx, content="/guesssong chunithm"))
    rec = await router.dispatch(replace(ctx, content="看什么V"))
    assert isinstance(rec, BotReply) and rec.quick_actions[0].url == rec.followup_text
