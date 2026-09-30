import io
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image

from maimai_bot.services.b50_image import CANVAS_SIZE, B50ImageService
from maimai_bot.services.daily_recommendation import DailyRecommendations, RecommendationItem
from maimai_bot.services.divingfish import Best50, PlayerRecords
from maimai_bot.services.gameplay import PlateEntry, PlateProgress
from maimai_bot.services.song_catalog import Chart, Song


def _record(index: int, *, is_new: bool) -> dict:
    return {
        "song_id": index + (12000 if is_new else 100),
        "title": f"测试曲目 {index}",
        "type": "DX" if index % 2 else "SD",
        "level_index": index % 5,
        "level": "14+",
        "ds": 14.7,
        "achievements": 100.5 + index / 10000,
        "ra": 320 - index,
        "rate": "sssp",
        "fc": "ap" if index % 3 == 0 else "",
        "fs": "fsdp" if index % 4 == 0 else "",
        "play_count": 174 if index == 0 else None,
    }


async def test_render_b50_png_with_missing_covers() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    best50 = Best50(
        nickname="测试玩家",
        rating=15752,
        additional_rating=10,
        plate="舞将",
        old=[_record(index, is_new=False) for index in range(35)],
        new=[_record(index, is_new=True) for index in range(15)],
    )
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = B50ImageService(client, background_path=background)
        result = await service.render(best50)

    image = Image.open(io.BytesIO(result))
    assert image.size == CANVAS_SIZE
    assert image.format == "PNG"
    assert len(result) > 100_000


async def test_render_100_item_score_table_as_long_png() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    records = [_record(index, is_new=index >= 50) for index in range(100)]
    player = PlayerRecords(
        nickname="测试玩家",
        rating=15752,
        additional_rating=10,
        plate="舞将",
        old=records[:50],
        new=records[50:],
    )
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = B50ImageService(client, background_path=background)
        result = await service.render_score_table(player, "Lv.14", records)

    image = Image.open(io.BytesIO(result))
    assert image.width == 1600
    assert image.height > CANVAS_SIZE[1]
    assert image.format == "PNG"


async def test_render_plate_progress_png() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    records = [_record(index, is_new=False) for index in range(8)]
    entries = tuple(
        PlateEntry(
            Chart(
                song_id=100 + index,
                title=f"未完成曲目 {index + 1}",
                artist="Artist",
                genre="maimai",
                version=14000,
                chart_type="standard",
                difficulty=3 if index < 5 else 2,
                level="13+",
                constant=13.8 - index / 10,
                designer="谱师",
            ),
            records[index],
        )
        for index in range(8)
    )
    progress = PlateProgress("橙将", "橙", "将", entries, (), entries)
    player = PlayerRecords("测试玩家", 15515, 0, "橙将", records, [])
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = B50ImageService(client, background_path=background)
        result = await service.render_plate_progress(player, progress)

    image = Image.open(io.BytesIO(result))
    assert image.size == CANVAS_SIZE
    assert image.format == "PNG"


async def test_render_single_song_detail_png() -> None:
    cover_output = io.BytesIO()
    Image.new("RGB", (256, 256), "navy").save(cover_output, format="PNG")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=cover_output.getvalue())

    charts = tuple(
        Chart(
            song_id=456,
            title="Glorious Crown",
            artist="xi",
            genre="maimai",
            version=16014,
            chart_type="standard",
            difficulty=index,
            level=("6", "9", "12+", "14+", "15")[index],
            constant=(6.0, 9.0, 12.7, 14.8, 15.0)[index],
            fit_constant=(6.03, None, 12.74, 14.72, 14.91)[index],
            designer="谱师",
            tap=300,
            hold=40,
            slide=50,
            touch=0,
            break_count=10,
        )
        for index in range(5)
    )
    song = Song(456, "Glorious Crown", "xi", "maimai", 225, 16000, charts)
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await B50ImageService(client, background_path=background).render_song_info(
            song, "PiNK"
        )

    image = Image.open(io.BytesIO(result))
    assert image.width == 1400
    assert image.height >= 1500
    assert image.format == "PNG"


async def test_render_ten_daily_recommendations() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    items = tuple(
        RecommendationItem(
            chart=Chart(
                song_id=500 + index,
                title=f"每日推荐曲目 {index + 1}",
                artist="Artist",
                genre="maimai",
                version=23000,
                chart_type="dx" if index % 2 else "standard",
                difficulty=3,
                level="14",
                constant=13.8 + index / 10,
                designer="谱师",
            ),
            current_achievement=None if index == 0 else 99.5,
            current_rating=0 if index == 0 else 300,
            target_rating=310,
            target_achievement=100.0 if index % 3 == 2 else 100.5,
            rating_gain=10,
            pool="B15" if index % 2 else "B35",
            in_best=index > 2,
            tier=("简单", "中等", "困难")[index % 3],
            pattern="BPM 180 · 16分交互 / Slide复合",
        )
        for index in range(10)
    )
    plan = DailyRecommendations(date(2026, 9, 24), "测试玩家", 15752, 14.2, items)
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await B50ImageService(
            client, background_path=background
        ).render_daily_recommendations(plan)

    image = Image.open(io.BytesIO(result))
    assert image.size == (1400, 1760)
    assert image.format == "PNG"


async def test_cover_download_retries_transient_error_and_caches_success() -> None:
    calls = 0
    cover_output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(cover_output, format="PNG")

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=cover_output.getvalue())

    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = B50ImageService(client, background_path=background)
        record = {"song_id": 123}
        assert await service._cover_for(record) is not None
        assert await service._cover_for(record) is not None

    assert calls == 2


async def test_cover_uses_lxns_fallback() -> None:
    cover_output = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(cover_output, format="PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.diving-fish.com":
            return httpx.Response(404)
        assert request.url.path.endswith("/maimai/jacket/456.png")
        return httpx.Response(200, content=cover_output.getvalue())

    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await B50ImageService(client, background_path=background)._cover_for(
            {"song_id": 456}
        )

    assert result is not None


async def test_dx_encoded_cover_uses_lxns_base_song_id() -> None:
    cover_output = io.BytesIO()
    Image.new("RGB", (32, 32), "purple").save(cover_output, format="PNG")
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.host == "www.diving-fish.com":
            return httpx.Response(404)
        assert request.url.path.endswith("/maimai/jacket/1860.png")
        return httpx.Response(200, content=cover_output.getvalue())

    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await B50ImageService(client, background_path=background)._cover_for(
            {"song_id": 11860}
        )

    assert result is not None
    assert "/covers/11860.png" in requested_paths
    assert "/covers/01860.png" in requested_paths
    assert requested_paths[-1].endswith("/maimai/jacket/1860.png")


async def test_cover_normalizes_non_png_waterfish_response() -> None:
    cover_output = io.BytesIO()
    Image.new("RGB", (32, 32), "orange").save(cover_output, format="JPEG")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=cover_output.getvalue())

    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await B50ImageService(client, background_path=background)._cover_for(
            {"song_id": 1901}
        )

    assert result is not None
    assert Image.open(io.BytesIO(result)).format == "PNG"


async def test_render_help_as_long_png() -> None:
    commands = tuple(
        SimpleNamespace(
            category="常用" if index < 3 else "曲目",
            name=f"command{index}",
            usage=f"/command{index} 参数",
            description=f"第 {index} 个测试功能",
        )
        for index in range(8)
    )
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient() as client:
        result = B50ImageService(client, background_path=background).render_help(commands)

    image = Image.open(io.BytesIO(result))
    assert image.width == 1600
    assert image.height > 500
    assert image.format == "PNG"


async def test_render_one_column_record_list_png() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    records = [_record(index, is_new=index >= 10) for index in range(20)]
    records[0]["_list_extra"] = "PC 174"
    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await B50ImageService(client, background_path=background).render_record_list(
            "成绩筛选「14+ 紫」",
            records,
            subtitle="测试玩家 · 共匹配 20 张谱面",
        )

    image = Image.open(io.BytesIO(result))
    assert image.width == 1200
    assert image.height == 245 + 20 * 150 + 100
    assert image.format == "PNG"


async def test_missing_cover_is_not_cached() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    background = Path(__file__).parents[1] / "assets" / "b50-background.png"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = B50ImageService(client, background_path=background)
        record = {"song_id": 456}
        assert await service._cover_for(record) is None
        assert await service._cover_for(record) is None

    assert calls == 4
