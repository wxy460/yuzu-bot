import io
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw

from maimai_bot.services.b50_image import B50ImageService, CosmeticProfile
from maimai_bot.services.divingfish import PlayerRecords

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def capture_labels(monkeypatch):
    original = ImageDraw.ImageDraw.text
    labels = []

    def capture(draw, xy, text, *args, **kwargs):
        labels.append(str(text))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    return labels


async def test_pc50_is_one_ranked_board_with_50_covers_and_a_personal_header(monkeypatch):
    labels = capture_labels(monkeypatch)
    service = B50ImageService(None, background_path=ASSETS / "b50-background.png")
    service._cover_for = AsyncMock(return_value=None)
    service._avatar_for = AsyncMock(return_value=None)
    sample = {
        "level": "14",
        "level_index": 3,
        "type": "DX",
        "achievements": 100.5350,
        "fc": "fc",
        "fs": "sync",
        "rate": "sssp",
    }
    rows = [dict(sample, song_id=i + 1, title=f"Song {i + 1}", play_count=i + 1) for i in range(55)]
    rows.append(dict(sample, song_id=99, title="Also top", play_count="55"))
    rows.append(dict(sample, song_id=100, title="Not ranked", play_count=None))
    player = PlayerRecords("读谱练习", 15400, 0, "橙将", [], [])
    result = await service.render_pc50(player, rows, avatar_url="https://example.test/avatar")
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG" and image.width == 1600 and 2000 <= image.height < 2600
        image.verify()
    covered = [c.args[0]["title"] for c in service._cover_for.await_args_list]
    assert covered[:3] == ["Song 55", "Also top", "Song 54"]
    assert len(covered) == 50 and "Not ranked" not in covered and "Song 1" not in covered
    service._avatar_for.assert_awaited_once_with("https://example.test/avatar")
    assert list(filter(lambda t: t.startswith("#"), labels)) == [f"#{i}" for i in range(1, 51)]
    assert labels.count("PC") == 50
    assert {"55", "读谱练习", "50 张", "100.5350%"} <= set(labels)
    assert "BEST 35 · 旧版本" not in labels and "NEW 15 · 当前版本" not in labels


async def test_count_only_import_needs_no_private_profile_or_fabricated_score(monkeypatch):
    labels = capture_labels(monkeypatch)
    service = B50ImageService(None, background_path=ASSETS / "b50-background.png")
    service._cover_for = AsyncMock(return_value=b"corrupt-jacket")
    service._avatar_for = AsyncMock()
    service._nameplate_for = AsyncMock()

    def unexpected(*args):
        raise AssertionError("unknown badge statuses should stay blank")

    monkeypatch.setattr(service, "_draw_score_badges", unexpected)
    rows = [
        {"song_id": 834, "level_index": 3, "type": "standard", "play_count": "174"},
        {"song_id": 417, "level_index": 4, "title": "Fragrance", "type": "sd", "play_count": 0},
        {"play_count": -10},
        {"play_count": "bad"},
        {"play_count": True},
        {"play_count": None},
    ]
    result = await service.render_pc50(
        None, rows, CosmeticProfile(), avatar_url="https://example.test/avatar"
    )
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (1600, 427)
        image.verify()
    assert service._cover_for.await_count == 2
    service._avatar_for.assert_not_called()
    service._nameplate_for.assert_not_called()
    assert {
        "曲目 #834",
        "Fragrance",
        "#1",
        "#2",
        "174",
        "0",
        "ID 834",
        "ID 417",
        "2 张谱面",
    } <= set(labels)
    assert not any("%" in text for text in labels)


async def test_unknown_counts_show_an_empty_board_without_cover_downloads(monkeypatch):
    labels = capture_labels(monkeypatch)
    service = B50ImageService(None, background_path=ASSETS / "b50-background.png")
    service._cover_for = AsyncMock()
    png = await service.render_pc50(None, [{"play_count": None}])
    assert {"0 张谱面", "暂无带游玩次数的谱面"} <= set(labels) and "PC" not in labels
    service._cover_for.assert_not_awaited()
    with Image.open(io.BytesIO(png)) as im:
        im.verify()


async def test_utage_uses_a_text_type_without_an_invented_standard_difficulty(monkeypatch):
    labels = capture_labels(monkeypatch)
    service = B50ImageService(None, background_path=ASSETS / "b50-background.png")
    service._cover_for = AsyncMock(return_value=None)
    await service.render_pc50(
        None,
        [
            {
                "song_id": 100018,
                "type": "UTAGE",
                "level_index": 0,
                "playCount": 22,
                "title": "宴会场谱面",
            },
            {
                "song_id": 100019,
                "type": "SD",
                "playCount": 6,
                "play_count": 9,
                "title": "有冲突的次数",
            },
        ],
    )
    assert {"1 张谱面", "宴", "22", "ID 100018"} <= set(labels)
    assert not ({"SD", "DX", "绿谱", "紫谱", "有冲突的次数"} & set(labels))
