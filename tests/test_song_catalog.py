from datetime import date

import httpx

from maimai_bot.services.song_catalog import SongCatalogService

PAYLOAD = {
    "songs": [
        {
            "id": 100,
            "title": "Test Song",
            "artist": "Artist",
            "genre": "maimai",
            "bpm": 180,
            "version": 25000,
            "difficulties": {
                "standard": [],
                "dx": [
                    {
                        "difficulty": 3,
                        "level": "14+",
                        "level_value": 14.7,
                        "note_designer": "Designer",
                        "version": 25014,
                    }
                ],
            },
        },
        {
            "id": 200,
            "title": "Other",
            "artist": "Someone",
            "genre": "POPSアニメ",
            "bpm": 120,
            "version": 24000,
            "difficulties": {
                "standard": [
                    {
                        "difficulty": 2,
                        "level": "13",
                        "level_value": 13.0,
                        "version": 24000,
                    }
                ],
                "dx": [],
            },
        },
    ],
    "versions": [{"title": "舞萌DX 2025", "version": 25000}],
}
ALIASES = {"aliases": [{"song_id": 100, "aliases": ["测试歌"]}]}


async def test_catalog_search_range_and_filters() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/alias/list"):
            return httpx.Response(200, json=ALIASES)
        return httpx.Response(200, json=PAYLOAD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = SongCatalogService(client)
        assert (await catalog.search("100"))[0].title == "Test Song"
        assert (await catalog.search("test"))[0].id == 100
        assert (await catalog.search("测试歌"))[0].id == 100
        assert catalog.version_name(25014) == "舞萌DX 2025"
        assert (await catalog.charts_in_range(14.7, 14.7))[0].song_id == 100
        assert (await catalog.random_chart("dx master 14+")).song_id == 100
        assert (await catalog.daily("user", date(2026, 9, 23))).song_id in {100, 200}

    assert calls == 3


async def test_divingfish_style_id_is_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alias/list"):
            return httpx.Response(200, json=ALIASES)
        return httpx.Response(200, json=PAYLOAD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = SongCatalogService(client)
        assert (await catalog.search("10100"))[0].id == 100


async def test_catalog_attaches_divingfish_fitted_constant() -> None:
    stats = {
        "charts": {
            "10100": [{}, {}, {}, {"fit_diff": 14.23}],
            "200": [{}, {}, {"fit_diff": 12.81}],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alias/list"):
            return httpx.Response(200, json=ALIASES)
        if request.url.path.endswith("/chart_stats"):
            return httpx.Response(200, json=stats)
        return httpx.Response(200, json=PAYLOAD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        songs = await SongCatalogService(client).songs()

    assert songs[0].charts[0].fit_constant == 14.23
    assert songs[1].charts[0].fit_constant == 12.81


async def test_random_chart_supports_combined_ranges() -> None:
    payload = {
        "songs": [
            {
                "id": 301,
                "title": "PiNK Master",
                "artist": "Artist",
                "genre": "maimai",
                "bpm": 180,
                "version": 11000,
                "difficulties": {
                    "standard": [],
                    "dx": [
                        {
                            "difficulty": 3,
                            "level": "14",
                            "level_value": 14.2,
                            "version": 11000,
                        }
                    ],
                },
            },
            {
                "id": 302,
                "title": "MURASAKi Master",
                "artist": "Artist",
                "genre": "maimai",
                "bpm": 190,
                "version": 12000,
                "difficulties": {
                    "standard": [],
                    "dx": [
                        {
                            "difficulty": 3,
                            "level": "14+",
                            "level_value": 14.6,
                            "version": 12000,
                        }
                    ],
                },
            },
            {
                "id": 303,
                "title": "Current Expert",
                "artist": "Artist",
                "genre": "maimai",
                "bpm": 160,
                "version": 25000,
                "difficulties": {
                    "standard": [
                        {
                            "difficulty": 2,
                            "level": "13+",
                            "level_value": 13.7,
                            "version": 25000,
                        }
                    ],
                    "dx": [],
                },
            },
        ],
        "versions": [
            {"title": "PiNK", "version": 11000},
            {"title": "MURASAKi", "version": 12000},
            {"title": "舞萌DX 2025", "version": 25000},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alias/list"):
            return httpx.Response(200, json={"aliases": []})
        if request.url.path.endswith("/chart_stats"):
            return httpx.Response(200, json={"charts": {}})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = SongCatalogService(client)
        low = await catalog.random_chart("dx master 14.0-14.3 桃-紫")
        high = await catalog.random_chart("dx master 14.5-14.8 桃-紫")
        level = await catalog.random_chart("standard expert 13+-14+")
        current = await catalog.random_chart("新曲 standard expert")

    assert low.song_id == 301
    assert high.song_id == 302
    assert level.song_id == 303
    assert current.song_id == 303
