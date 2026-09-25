from dataclasses import replace
from datetime import date

from maimai_bot.services.daily_recommendation import (
    build_daily_recommendations,
    format_daily_recommendations,
)
from maimai_bot.services.divingfish import PlayerRecords
from maimai_bot.services.song_catalog import Chart, Song


def _song(song_id: int, constant: float) -> Song:
    chart = Chart(
        song_id=song_id,
        title=f"推荐测试曲 {song_id}",
        artist="Artist",
        genre="maimai",
        version=23000,
        chart_type="dx",
        difficulty=3,
        level="13+",
        constant=constant,
        designer="谱师",
        tap=500,
        hold=40,
        slide=80,
        touch=20,
        break_count=30,
    )
    return Song(song_id, chart.title, chart.artist, chart.genre, 180, chart.version, (chart,))


def _record(song_id: int, achievement: float, ra: int) -> dict:
    return {
        "song_id": song_id,
        "title": f"推荐测试曲 {song_id}",
        "type": "DX",
        "level_index": 3,
        "achievements": achievement,
        "ra": ra,
    }


def test_daily_recommendations_are_personalised_unique_and_stable() -> None:
    songs = tuple(_song(index, 12.0 + index / 10) for index in range(1, 31))
    records = [_record(index, 99.0 + index / 1000, 280) for index in range(1, 31)]
    records[14] = _record(15, 100.5, 300)
    player = PlayerRecords("测试玩家", 14_000, 0, "舞萌玩家", records, [])
    args = (player, songs, lambda _: "舞萌DX 2026", "qq-user", date(2026, 9, 24))

    first = build_daily_recommendations(*args)
    second = build_daily_recommendations(*args)

    assert first == second
    assert len(first.items) == 10
    assert len({item.chart.song_id for item in first.items}) == 10
    assert all(item.current_achievement is None or item.current_achievement < 100.5 for item in first.items)
    assert all(item.chart.song_id != 15 for item in first.items)
    assert first.target_constant == 12.7
    assert {item.tier for item in first.items} == {"简单", "中等", "困难"}


def test_daily_recommendation_text_contains_each_pattern() -> None:
    songs = tuple(_song(index, 12.2 + index / 10) for index in range(1, 16))
    records = [_record(index, 99.5, 280) for index in range(1, 16)]
    player = PlayerRecords("测试玩家", 14_000, 0, "舞萌玩家", records, [])
    plan = build_daily_recommendations(
        player, songs, lambda _: "舞萌DX 2026", "qq-user", date(2026, 9, 24)
    ).with_patterns([f"真实配置 {index}" for index in range(1, 11)])

    text = format_daily_recommendations(plan)

    assert "能力拟合中心：12.7" in text
    assert "10. [" in text
    assert "配置/难点：真实配置 10" in text


def test_rating_gain_uses_b35_replacement_instead_of_single_chart_delta() -> None:
    records = [_record(index, 99.0, 300 + index) for index in range(1, 36)]
    records.append(_record(100, 100.0, 299))
    player = PlayerRecords("测试玩家", 14_000, 0, "舞萌玩家", records, [])
    songs = (_song(100, 14.0),)

    plan = build_daily_recommendations(
        player, songs, lambda _: "旧版本", "qq-user", date(2026, 9, 24)
    )

    item = plan.items[0]
    assert item.current_achievement == 100.0
    assert item.target_rating == 315
    assert item.pool == "B35"
    assert not item.in_best
    assert item.rating_gain == 14  # 315 target minus the current B35 floor of 301


def test_hard_chart_uses_sss_target_and_lower_fit_is_preferred() -> None:
    records = [_record(index, 99.0, 280) for index in range(1, 36)]
    player = PlayerRecords("测试玩家", 14_000, 0, "舞萌玩家", records, [])
    harder = _song(100, 14.5)
    easier = _song(101, 14.5)
    harder_chart = harder.charts[0]
    easier_chart = easier.charts[0]
    harder = Song(
        harder.id,
        harder.title,
        harder.artist,
        harder.genre,
        harder.bpm,
        harder.version,
        (replace(harder_chart, fit_constant=14.7),),
    )
    easier = Song(
        easier.id,
        easier.title,
        easier.artist,
        easier.genre,
        easier.bpm,
        easier.version,
        (replace(easier_chart, fit_constant=14.1),),
    )

    plan = build_daily_recommendations(
        player, (harder, easier), lambda _: "旧版本", "qq-user", date(2026, 9, 24)
    )

    assert plan.items[0].chart.song_id == 101
    assert all(item.target_achievement == 100.0 for item in plan.items)
