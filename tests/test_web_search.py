from __future__ import annotations

import httpx

from maimai_bot.services.web_search import WebSearchService, needs_web_search, search_query


async def test_brave_search_parses_results_and_uses_cache() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == "search.brave.com"
        return httpx.Response(
            200,
            text=(
                'before {title:"舞萌DX 更新",url:"https://example.com/news",'
                'full_title:void 0,description:"新增歌曲与谱面"}, after'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebSearchService(client)
        first = await service.search("舞萌DX 最新版本")
        second = await service.search("舞萌DX 最新版本")

    assert first == second
    assert calls == 1
    assert first[0].title == "舞萌DX 更新"
    assert first[0].url == "https://example.com/news"


async def test_search_falls_back_to_bing_rss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "search.brave.com":
            return httpx.Response(200, text="no results")
        return httpx.Response(
            200,
            text=(
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>官方公告</title><link>https://example.com/official</link>"
                "<description>今天发布更新。</description></item></channel></rss>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await WebSearchService(client).search("最新更新")

    assert results[0].title == "官方公告"


def test_search_trigger_avoids_ordinary_today_memory() -> None:
    assert needs_web_search("帮我联网查一下舞萌DX最新消息")
    assert needs_web_search("今天的天气如何")
    assert not needs_web_search("我今天早上去打了舞萌")
    assert search_query("帮我联网搜索：音击最新版本") == "音击最新版本"
