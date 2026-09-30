from maimai_bot.features import _song_resources
from maimai_bot.services.bilibili import (
    BilibiliVideo,
    ChartConfirmation,
    ChartConfirmationResult,
)
from maimai_bot.services.song_catalog import Chart


def _chart(chart_type: str, difficulty: int) -> Chart:
    return Chart(
        song_id=417,
        title="ウミユリ海底譚",
        artist="n-buna",
        genre="niconicoボーカロイド",
        version=15005,
        chart_type=chart_type,
        difficulty=difficulty,
        level="13",
        constant=13.0,
        designer="譜面-100号",
    )


def test_song_resources_build_preview_and_confirmation_buttons() -> None:
    master = _chart("standard", 3)
    remaster = _chart("dx", 4)
    result = ChartConfirmationResult(
        "ウミユリ海底譚",
        (
            ChartConfirmation(
                master,
                BilibiliVideo(
                    "BV1EXAMPLE01",
                    "chart video",
                    "Uploader",
                    source_url="https://www.bilibili.com/video/BV1EXAMPLE01",
                ),
            ),
            ChartConfirmation(remaster, None),
        ),
    )

    text, actions = _song_resources(result)

    assert "https://www.bilibili.com/video/BV1EXAMPLE01" in text
    assert "https://v.awmc.cc/preview?song=417&kind=standard&diff=5" in text
    assert "https://v.awmc.cc/preview?song=417&kind=dx&diff=6" in text
    assert [(action.label, action.url) for action in actions] == [
        ("🎬 紫SD确认", "https://www.bilibili.com/video/BV1EXAMPLE01"),
        ("▶️ 紫SD预览", "https://v.awmc.cc/preview?song=417&kind=standard&diff=5"),
        ("▶️ 白DX预览", "https://v.awmc.cc/preview?song=417&kind=dx&diff=6"),
    ]
