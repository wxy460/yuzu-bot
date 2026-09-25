from __future__ import annotations

import httpx

from maimai_bot.services.chart_analysis import ChartAnalysisService
from maimai_bot.services.song_catalog import Chart


def _chart(*, chart_type: str = "standard") -> Chart:
    return Chart(
        song_id=456,
        title="Glorious Crown",
        artist="xi",
        genre="maimai",
        version=16014,
        chart_type=chart_type,
        difficulty=3,
        level="14+",
        constant=14.8,
        designer="合作だよ",
        tap=900,
        hold=80,
        slide=120,
        touch=0,
        break_count=50,
    )


async def test_chart_analysis_uses_actual_simai_patterns_and_cache() -> None:
    calls = 0
    raw = """
&title=Glorious Crown
&inote_5=
(225){4}1/8,2h[4:1],3-6[8:1]*-8[8:1],4w8[8:1],1V35[8:1],
{24}1,2,3,4,5,6,7,8,
1,2,3,4,5,6,7,8,
E
"""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path.endswith("/456.txt")
        return httpx.Response(200, text=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChartAnalysisService(client)
        analysis = await service.analyze(_chart())
        again = await service.analyze(_chart())

    assert analysis == again
    assert calls == 1
    assert analysis.raw_available
    assert analysis.max_subdivision == 24
    assert analysis.chord_count == 1
    assert analysis.multi_slide_count == 1
    assert analysis.fan_count == 1
    assert analysis.fold_count == 1
    assert analysis.rotation_count == 2


async def test_chart_analysis_uses_dx_resource_and_falls_back_to_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/10456.txt")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await ChartAnalysisService(client).describe(_chart(chart_type="dx"), 225)

    assert "未返回完整 Simai 文件" in text
    assert "总物量 1150" in text
    assert "不冒充" not in text


async def test_brief_uses_real_chart_structures() -> None:
    raw = "&inote_5=\n{24}1/5,1w5*1V357,2h[4:1],A1,3b,"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await ChartAnalysisService(client).brief(_chart(), 180)

    assert "BPM 180" in text
    assert "24分爆发" in text
    assert "多重星星" in text
    assert "扇形Slide" in text
