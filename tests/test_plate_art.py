import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw, ImageOps

from maimai_bot.services import plate_art
from maimai_bot.services.b50_image import B50ImageService
from maimai_bot.services.divingfish import PlayerRecords
from maimai_bot.services.gameplay import PlateEntry, PlateProgress
from maimai_bot.services.plate_names import normalize_plate_name
from maimai_bot.services.song_catalog import Chart

ASSETS = Path(__file__).resolve().parents[1] / "assets"


@pytest.mark.parametrize(
    ("alias", "name"),
    [
        ("曉極", "暁極"),
        ("晓將", "暁将"),
        ("桜神", "櫻神"),
        ("樱舞舞", "櫻舞舞"),
        ("辉极", "輝極"),
        ("华神", "華神"),
        ("堇將", "菫将"),
        ("雙神", "双神"),
        ("镜极", "鏡極"),
        ("彩將", "彩将"),
        ("霸者", "覇者"),
        ("舞舞", "舞舞舞"),
        ("夜空中的桜花", "夜空中的桜花"),
        ("猫極", "猫極"),
    ],
)
def test_achievement_name_aliases_do_not_rewrite_unrelated_collectibles(alias, name):
    assert normalize_plate_name(alias) == name


async def test_bundled_progress_plates_resolve_offline_and_render_the_target_not_the_equipped_plate():
    def offline(request):
        pytest.fail(f"bundled artwork unexpectedly requested {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as http:
        renderer = B50ImageService(http, background_path=ASSETS / "b50-background.png")
        for alias, item in (
            ("镜将", 559102),
            ("彩极", 609101),
            ("彩将", 609102),
            ("彩神", 609103),
            ("彩舞舞", 609104),
            ("雙神", 459103),
            ("霸者", 6148),
            ("舞舞", 6152),
            ("桃将", 6121),
        ):
            assert (
                await renderer._nameplate_for(alias)
                == (ASSETS / "plates" / f"{item}.png").read_bytes()
            )
        # The player equips 橙将 while their requested target is the newer 彩将.
        player = PlayerRecords("演示", 15752, 0, "橙将", [], [])
        progress = PlateProgress("彩将", "彩", "将", (), (), ())
        image = await renderer.render_plate_progress(player, progress)
    with Image.open(io.BytesIO(image)) as out, Image.open(ASSETS / "plates/609102.png") as source:
        target = ImageOps.contain(source.convert("RGBA"), (560, 112), Image.Resampling.LANCZOS)
        left, top = 920 + (560 - target.width) // 2, 1605 + 50 + (112 - target.height) // 2
        points = [
            (x, y)
            for x in range(9, target.width, 20)
            for y in range(5, target.height, 8)
            if target.getpixel((x, y))[3] == 255
        ]
        assert len(points) > 25
        for x, y in points:
            assert out.getpixel((left + x, top + y)) == target.getpixel((x, y))[:3]


def png():
    stream = io.BytesIO()
    Image.new("RGB", (500, 80), (30, 192, 87)).save(stream, "PNG")
    return stream.getvalue()


async def test_directory_updates_retry_after_outage_and_cdn_failure(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=80.0)
    monkeypatch.setattr(plate_art, "time", SimpleNamespace(monotonic=lambda: clock.now))
    calls, images, revision = [], [], 0

    def upstream(request):
        if request.url.path.endswith("/list"):
            calls.append(revision)
            if revision == 1:
                return httpx.Response(503)
            rows = [{"id": 31, "name": "旧收藏品"}]
            if revision > 1:
                rows.append({"id": 25, "name": "新收藏品"})
            return httpx.Response(200, json={"plates": rows})
        images.append(request.url.path)
        return httpx.Response(503) if len(images) == 1 else httpx.Response(200, content=png())

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        art = plate_art.PlateArtwork(http, tmp_path)
        assert await art.resolve("新收藏品") is None
        revision = 1
        clock.now += plate_art.CATALOG_TTL + 1
        assert await art.resolve("新收藏品") is None
        assert await art.resolve("旧收藏品") is None  # old ID still usable, CDN temporarily down
        assert await art.resolve("旧收藏品") == png()
        revision = 2
        clock.now += 61
        assert await art.resolve("新收藏品") == png()
    assert calls == [0, 1, 2]
    assert images == ["/maimai/plate/31.png", "/maimai/plate/31.png", "/maimai/plate/25.png"]


async def test_corrupt_asset_recovers_from_the_exact_id_and_unknown_target_is_labeled(
    tmp_path, monkeypatch
):
    plates = tmp_path / "plates"
    plates.mkdir()
    (plates / "index.json").write_text(json.dumps({"彩将": 609102}), encoding="utf-8")
    (plates / "609102.png").write_bytes(b"partial PNG")
    urls = []

    def upstream(request):
        urls.append(request.url.path)
        if request.url.path.endswith("/609102.png"):
            return httpx.Response(200, content=png())
        return httpx.Response(200, json={"plates": [{"name": "彩将", "id": 609102}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        art = plate_art.PlateArtwork(http, plates)
        assert await art.resolve("彩將") == png()
        assert await art.resolve("未来尚未发行牌") is None
        renderer = B50ImageService(http, background_path=ASSETS / "b50-background.png")
        player = PlayerRecords("演示", 15000, 0, "橙将", [], [])
        progress = PlateProgress("未来尚未发行牌", "未", "将", (), (), ())
        original = ImageDraw.ImageDraw.text
        labels = []

        def capture(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original(draw, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
        result = await renderer.render_plate_progress(player, progress)
        assert result.startswith(b"\x89PNG")
        assert "未来尚未发行牌" in labels and "原版素材暂缺" in labels
        assert "曲库暂无该版本的谱面数据" in labels and "进度待定" in labels
        assert not any(
            "全部条件已经达成" in text or "0 / 0" in text or "0.0% 已完成" in text
            for text in labels
        )
        labels.clear()
        entry = PlateEntry(
            Chart(1, "演示", "演示", "", 10000, "standard", 3, "12", 12.1, ""),
            {"achievements": 100.5},
        )
        complete = PlateProgress("彩将", "彩", "将", (entry,), (entry,), ())
        await renderer.render_plate_progress(player, complete)
        assert "全部条件已经达成！" in labels and "100.0% 已完成" in labels
    assert urls.count("/maimai/plate/609102.png") == 1
    assert not any(url.endswith("/1.png") for url in urls)
