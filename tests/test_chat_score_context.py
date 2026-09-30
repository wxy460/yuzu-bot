from maimai_bot.features import _chat_score_context
from maimai_bot.services.divingfish import PlayerRecords
from maimai_bot.services.song_catalog import Chart, Song


class ScoreStub:
    async def player_records(self, _user_id: str) -> PlayerRecords:
        common = {
            "type": "DX",
            "level_index": 3,
            "level": "14+",
            "level_label": "Master",
            "achievements": 100.5,
            "ra": 326,
            "rate": "sssp",
        }
        return PlayerRecords(
            "SIYUE",
            15752,
            0,
            "橙将",
            [
                {**common, "song_id": 10101, "title": "Lower Fit", "ds": 14.5},
                {**common, "song_id": 10202, "title": "Higher Fit", "ds": 14.4},
            ],
            [],
        )


class CatalogStub:
    async def songs(self) -> tuple[Song, ...]:
        return (
            _song(101, "Lower Fit", 14.61),
            _song(202, "Higher Fit", 14.73),
        )


def _song(song_id: int, title: str, fit: float) -> Song:
    chart = Chart(
        song_id=song_id,
        title=title,
        artist="Artist",
        genre="maimai",
        version=25000,
        chart_type="dx",
        difficulty=3,
        level="14+",
        constant=14.5,
        designer="Designer",
        fit_constant=fit,
    )
    return Song(song_id, title, "Artist", "maimai", 180, 25000, (chart,))


async def test_chat_context_includes_and_sorts_divingfish_fit_constants() -> None:
    context = await _chat_score_context(
        ScoreStub(),  # type: ignore[arg-type]
        CatalogStub(),  # type: ignore[arg-type]
        {},
        "user",
        "我的 B50 鸟加里面拟合定数最高的是谁？",
    )

    assert "Higher Fit" in context
    assert "水鱼拟合定数14.73" in context
    assert "水鱼拟合定数14.61" in context
    assert "已按水鱼拟合定数从高到低" in context
    related = context.split("与本次问题可能相关的水鱼成绩", 1)[1]
    assert related.index("Higher Fit") < related.index("Lower Fit")
    assert "不得用官方定数代替" in context
