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
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maimai_bot.domain import MessageContext
from maimai_bot.features import build_router


async def test_legacy_plates_use_cabinet_group_and_specific_remasters():
    def make(song_id, version, kind="standard"):
        charts = tuple(replace(chart(), song_id=song_id, version=version, chart_type=kind,
                               difficulty=d) for d in range(5))
        return Song(song_id, str(song_id), "", "", 0, version, charts)

    # 70 is needed for the legacy group, but not 真. Link's Re:MASTER debuted on DX.
    songs = (make(70, 10000), make(131, 15000), make(456, 16000), make(853, 19900),
             make(456, 21000), make(70, 23000, "dx"))
    catalog = SimpleNamespace(songs=AsyncMock(return_value=songs),
                              version_name=lambda ver: {10000: "maimai", 16000: "PiNK"}.get(ver))
    player = replace(records(), old=[{"song_id": 70, "level_index": 0, "type": "SD",
                                      "achievements": 80, "fc": "", "fs": ""}])
    for name in ("舞将", "舞極", "舞神", "舞舞舞", "舞", "霸者", "覇者"):
        progress = await build_plate_progress(player, catalog, name)
        ids = {(e.chart.song_id, e.chart.difficulty) for e in progress.entries}
        assert ids == {(i, d) for i in (70, 131, 456) for d in range(4)} | {(70, 4)}
        assert all(e.chart.chart_type == "standard" and e.chart.version < 20000 for e in progress.entries)
        assert len(progress.completed) == (1 if progress.plate == "霸者" else 0)
    assert not (await build_plate_progress(player, catalog, "真极")).entries
    assert len((await build_plate_progress(player, catalog, "桃将")).entries) == 4
    with pytest.raises(ValueError, match="没有发行真将"):
        await build_plate_progress(player, catalog, "真将")


async def test_group_command_recognizes_legacy_plate_names_without_mention():
    router = build_router(None, None, None, None, None, None, None)
    for text in ("舞将进度", "舞舞舞", "舞极完成表", "舞神进度", "覇者进度", "霸者", "舞进度"):
        ctx = MessageContext("u", "g", "group", text, "id")
        assert router.recognizes_command(text, ctx)


@pytest.mark.parametrize(("alias", "official", "version"), [
    ("镜將", "鏡将", "舞萌DX 2025"),
    ("彩将", "彩将", "舞萌DX 2026"),
    ("雙極", "双极", "舞萌DX 2024"),
    ("曉神", "暁神", "ORANGE PLUS"),
    ("桜舞舞", "櫻舞舞", "PiNK PLUS"),
])
async def test_plate_alias_reaches_the_same_progress_and_artwork(alias, official, version):
    router = build_router(None, None, None, None, None, None, None)
    query = f"{alias}进度"
    assert router.recognizes_command(query, MessageContext("u", "g", "group", query, "id"))
    song = Song(417, "Fragrance", "", "", 0, 15000, (chart(),))
    catalog = SimpleNamespace(songs=AsyncMock(return_value=[song]), version_name=lambda _: version)
    progress = await build_plate_progress(records(), catalog, alias)
    assert progress.plate == official
    assert len(progress.entries) == 1
