from maimai_bot.services.divingfish import PlayerRecords
from maimai_bot.services.gameplay import (
    build_plate_progress,
    conditional_scores,
    level_progress_text,
    personal_song_text,
    scoreline,
)
from maimai_bot.services.song_catalog import Chart, Song


def chart(*, level: str = "14+") -> Chart:
    return Chart(
        song_id=451,
        title="Test Song",
        artist="Artist",
        genre="maimai",
        version=15000,
        chart_type="standard",
        difficulty=3,
        level=level,
        constant=14.7,
        designer="Designer",
        tap=100,
        hold=10,
        slide=10,
        touch=0,
        break_count=10,
    )


def records() -> PlayerRecords:
    return PlayerRecords(
        nickname="Player",
        rating=15000,
        additional_rating=0,
        plate="橙将",
        old=[
            {
                "song_id": 10451,
                "title": "Test Song",
                "type": "SD",
                "level_index": 3,
                "level": "14+",
                "level_label": "Master",
                "achievements": 100.5,
                "ra": 320,
                "ds": 14.7,
                "rate": "sssp",
                "fc": "ap",
                "fs": "fsd",
            }
        ],
        new=[],
    )


def test_personal_song_matches_divingfish_offset_id() -> None:
    song = Song(451, "Test Song", "Artist", "maimai", 180, 15000, (chart(),))
    assert "100.5000%" in personal_song_text(records(), song)


def test_condition_filters_sd_and_ap() -> None:
    result = conditional_scores(records(), "14+ SD AP")
    assert "1 张" in result
    assert "Test Song" in result


def test_level_progress_uses_public_catalog_denominator() -> None:
    result = level_progress_text(records(), "14+", "ap", [chart(), chart(level="13+")])
    assert "1/1" in result


def test_scoreline_uses_note_weights() -> None:
    result = scoreline(chart(), 100.5)
    assert "TAP GREAT" in result
    assert "BREAK 50落" in result


async def test_plate_progress_counts_unplayed_charts() -> None:
    first = chart()
    second = Chart(
        song_id=452,
        title="Unplayed",
        artist="Artist",
        genre="maimai",
        version=15000,
        chart_type="standard",
        difficulty=3,
        level="13+",
        constant=13.8,
        designer="Designer",
    )
    third = Chart(
        song_id=453,
        title="Harder Unplayed",
        artist="Artist",
        genre="maimai",
        version=15000,
        chart_type="standard",
        difficulty=3,
        level="14",
        constant=14.2,
        designer="Designer",
    )

    class Catalog:
        async def songs(self):
            return (
                Song(451, "Test Song", "Artist", "maimai", 180, 15000, (first,)),
                Song(452, "Unplayed", "Artist", "maimai", 180, 15000, (second,)),
                Song(453, "Harder Unplayed", "Artist", "maimai", 180, 15000, (third,)),
            )

        def version_name(self, _version):
            return "ORANGE PLUS"

    progress = await build_plate_progress(records(), Catalog(), "暁将")
    assert len(progress.entries) == 3
    assert len(progress.completed) == 1
    assert len(progress.unfinished) == 2
    assert [entry.chart.song_id for entry in progress.unfinished] == [453, 452]
    assert progress.unfinished[0].achievement == 0
