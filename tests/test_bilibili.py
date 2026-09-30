import json
from pathlib import Path

import httpx

from maimai_bot.services.bilibili import BilibiliChartService, BilibiliVideo
from maimai_bot.services.song_catalog import Chart, Song


def _chart(chart_type: str, difficulty: int) -> Chart:
    return Chart(
        song_id=1628,
        title="WE'RE BACK!!",
        artist="K-forest",
        genre="maimai",
        version=23000,
        chart_type=chart_type,
        difficulty=difficulty,
        level="14",
        constant=14.0,
        designer="譜面-100号",
    )


class StubBilibiliService(BilibiliChartService):
    async def _uploader_videos(
        self, uid: int, uploader: str, keyword: str
    ) -> tuple[BilibiliVideo, ...]:
        assert keyword == "WE'RE BACK!!"
        videos = {
            "Peace_Walker": (
                BilibiliVideo("BV1DXMASTER1", "WE'RE BACK!! [DX] Master 14", uploader),
                BilibiliVideo("BV1AMBIGUOUS", "WE'RE BACK!! Master 14", uploader),
            ),
            "DJNaughty": (
                BilibiliVideo("BV1SDMASTER1", "WE'RE BACK!! 标准谱面 Master 14", uploader),
                BilibiliVideo("BV1DXREMAST1", "WE'RE BACK!! DX谱面 Re:Master 14+", uploader),
            ),
            "風又ねリ": (),
        }
        return videos[uploader]


async def test_confirmation_links_are_direct_bv_urls_and_verify_chart_type() -> None:
    song = Song(
        1628,
        "WE'RE BACK!!",
        "K-forest",
        "maimai",
        180,
        23000,
        (_chart("dx", 3), _chart("standard", 3), _chart("dx", 4), _chart("dx", 2)),
    )
    async with httpx.AsyncClient() as client:
        text = await StubBilibiliService(client).confirmation_links(song)

    assert "https://www.bilibili.com/video/BV1DXMASTER1" in text
    assert "https://www.bilibili.com/video/BV1AMBIGUOUS" in text
    assert "https://www.bilibili.com/video/BV1DXREMAST1" in text
    assert "紫谱 · DX MASTER · Peace_Walker" in text
    assert "紫谱 · SD MASTER · Peace_Walker" in text
    assert "白谱 · DX Re:MASTER · DJNaughty" in text
    assert "search.bilibili.com" not in text


async def test_confirmation_result_exposes_structured_chart_links() -> None:
    song = Song(
        1628,
        "WE'RE BACK!!",
        "K-forest",
        "maimai",
        180,
        23000,
        (_chart("dx", 3), _chart("standard", 4)),
    )
    async with httpx.AsyncClient() as client:
        result = await StubBilibiliService(client).confirmations(song)

    assert [(item.chart.chart_type, item.chart.difficulty) for item in result.entries] == [
        ("dx", 3),
        ("standard", 4),
    ]
    assert result.entries[0].video is not None


async def test_confirmation_links_are_empty_without_purple_or_white_chart() -> None:
    song = Song(1, "Only Expert", "Artist", "maimai", 120, 23000, (_chart("dx", 2),))
    async with httpx.AsyncClient() as client:
        text = await StubBilibiliService(client).confirmation_links(song)

    assert text == ""


async def test_fragrance_seed_survives_bilibili_rate_control() -> None:
    charts = (
        Chart(
            100, "Fragrance", "Tsukasa", "maimai", 23000, "standard", 3, "14", 14.2, "譜面-100号"
        ),
        Chart(
            100, "Fragrance", "Tsukasa", "maimai", 23000, "standard", 4, "14+", 14.7, "譜面-100号"
        ),
    )
    song = Song(100, "Fragrance", "Tsukasa", "maimai", 180, 23000, charts)
    async with httpx.AsyncClient() as client:
        service = BilibiliChartService(client)
        service._blocked_until = float("inf")
        text = await service.confirmation_links(song)

    assert "https://www.bilibili.com/video/BV1rD4y1F72q" in text
    assert "https://www.bilibili.com/video/BV1QjtLesEKc" in text
    assert "B站接口正处于风控" not in text


async def test_popipo_manual_seed_covers_the_only_upstream_gap() -> None:
    chart = Chart(
        259,
        "ぽっぴっぽー",
        "",
        "niconicoボーカロイド",
        23000,
        "standard",
        3,
        "13",
        13.0,
        "譜面-100号",
    )
    song = Song(259, "ぽっぴっぽー", "", "niconicoボーカロイド", 150, 23000, (chart,))
    async with httpx.AsyncClient() as client:
        text = await BilibiliChartService(client).confirmation_links(song)

    assert "https://www.bilibili.com/video/BV1fi421e7FX" in text
    assert "紫谱 · SD MASTER" in text


async def test_structured_offline_index_is_exact_and_never_calls_bilibili(tmp_path: Path) -> None:
    path = tmp_path / "bilibili.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "videos": [
                    {
                        "song_title": "WE'RE BACK!!",
                        "artist": "K-forest",
                        "chart_type": "dx",
                        "difficulty": 3,
                        "bvid": "BV1OFFLINE01",
                        "url": "https://www.bilibili.com/video/BV1OFFLINE01?p=1",
                        "title": "WE'RE BACK!! DX Master 14",
                        "source": "reviewed offline test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    song = Song(
        1628,
        "WE'RE BACK!!",
        "K-forest",
        "maimai",
        180,
        23000,
        (_chart("dx", 3),),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as client:
        text = await BilibiliChartService(client, index_path=path).confirmation_links(song)

    assert "https://www.bilibili.com/video/BV1OFFLINE01?p=1" in text
    assert "reviewed offline test" in text
    assert "不会调用 B 站搜索接口" in text


async def test_unknown_song_returns_missing_without_network() -> None:
    def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    song = Song(9, "No Offline Video", "Nobody", "maimai", 120, 23000, (_chart("dx", 3),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as client:
        text = await BilibiliChartService(client).confirmation_links(song)

    assert "本地审核索引暂未收录" in text


async def test_imported_url_is_restricted_to_canonical_bilibili_video(tmp_path: Path) -> None:
    path = tmp_path / "bilibili.json"
    path.write_text(
        json.dumps(
            {
                "videos": [
                    {
                        "song_title": "WE'RE BACK!!",
                        "artist": "K-forest",
                        "chart_type": "dx",
                        "difficulty": 3,
                        "bvid": "BV1OFFLINE01",
                        "url": "https://example.com/phishing",
                        "title": "WE'RE BACK!! DX Master 14",
                        "source": "test",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    song = Song(1628, "WE'RE BACK!!", "K-forest", "maimai", 180, 23000, (_chart("dx", 3),))
    async with httpx.AsyncClient() as client:
        text = await BilibiliChartService(client, index_path=path).confirmation_links(song)

    assert "https://www.bilibili.com/video/BV1OFFLINE01" in text
    assert "example.com" not in text
