from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.features import _masked_guess_title, build_router
from maimai_bot.services.song_catalog import Chart, Song


class GuessCatalogStub:
    def __init__(self) -> None:
        self.last_random_argument = ""
        self.cover_levels: list[int] = []
        self.chart = Chart(
            song_id=1234,
            title="Glorious Crown",
            artist="xi",
            genre="maimai",
            version=16000,
            chart_type="standard",
            difficulty=3,
            level="14+",
            constant=14.8,
            designer="某谱师",
            tap=800,
            hold=60,
            slide=90,
            touch=0,
            break_count=20,
        )
        self.song = Song(
            id=1234,
            title=self.chart.title,
            artist=self.chart.artist,
            genre=self.chart.genre,
            bpm=225,
            version=self.chart.version,
            charts=(self.chart,),
        )

    async def random_chart(self, argument: str) -> Chart:
        self.last_random_argument = argument
        return self.chart

    async def cover_clue(self, _song_id: int, *, reveal_level: int = 0) -> bytes:
        self.cover_levels.append(reveal_level)
        return f"cover-{reveal_level}".encode()

    async def aliases_for(self, _query: str):
        return self.song, ("皇冠",)

    async def songs(self):
        return (self.song,)

    def version_name(self, _version: int) -> str:
        return "PiNK"


class GuessImageStub:
    async def render_song_info(self, song: Song, version_name: str, **kwargs) -> bytes:
        assert song.id == 1234
        assert version_name == "PiNK"
        assert kwargs["headline"] == "猜歌答案"
        return b"song-detail"


def context(content: str) -> MessageContext:
    return MessageContext("user", "group", "group", content, "message")


async def test_guess_hints_accumulate_and_advance() -> None:
    unused = object()
    router = build_router(
        unused, unused, unused, unused, GuessCatalogStub(), unused, unused  # type: ignore[arg-type]
    )

    start = await router.dispatch(context("/guess"))
    first = await router.dispatch(context("/hint"))
    second = await router.dispatch(context("/hint"))

    assert "艺术家：xi" in start
    assert "增量提示 1/" in first
    assert "增量提示 2/" in second
    assert "歌曲 ID" not in first + second
    assert "1. 歌名是英文/拉丁字母标题，共 2 个词。" in first
    assert "1. 歌名是英文/拉丁字母标题，共 2 个词。" in second


async def test_guess_accepts_alias_after_using_hints() -> None:
    unused = object()
    router = build_router(
        unused,
        unused,
        GuessImageStub(),
        unused,
        GuessCatalogStub(),
        unused,
        unused,  # type: ignore[arg-type]
    )

    await router.dispatch(context("/guess note"))
    await router.dispatch(context("提示"))
    result = await router.dispatch(context("答 皇冠"))

    assert isinstance(result, BotReply)
    assert result.image_png == b"song-detail"
    assert "答对啦！答案是 Glorious Crown（ID 1234）。" in result.text
    assert "艺术家：xi" in result.text
    assert "当前没有" in await router.dispatch(context("提示"))


async def test_reveal_includes_song_detail_image_without_video_link() -> None:
    unused = object()
    router = build_router(
        unused,
        unused,
        GuessImageStub(),
        unused,
        GuessCatalogStub(),
        unused,
        unused,  # type: ignore[arg-type]
    )

    await router.dispatch(context("/guess"))
    result = await router.dispatch(context("答案"))

    assert isinstance(result, BotReply)
    assert result.image_png == b"song-detail"
    assert "答案是 Glorious Crown（ID 1234）。" in result.text
    assert not result.followup_text


def test_masked_title_is_helpful_without_revealing_full_answer() -> None:
    masked = _masked_guess_title("Glorious Crown")

    assert masked != "Glorious Crown"
    assert masked.startswith("G")
    assert " " in masked
    assert "□" in masked


async def test_guess_combines_constant_version_and_type_filters() -> None:
    unused = object()
    catalog = GuessCatalogStub()
    router = build_router(
        unused, unused, unused, unused, catalog, unused, unused  # type: ignore[arg-type]
    )

    result = await router.dispatch(context("/guess 14.0-14.5 桃-紫 DX"))

    assert "限定范围：14.0-14.5 桃-紫 DX" in result
    assert catalog.last_random_argument == "master 14.0-14.5 桃-紫 DX"


async def test_natural_cover_guess_accepts_filters() -> None:
    unused = object()
    catalog = GuessCatalogStub()
    router = build_router(
        unused, unused, unused, unused, catalog, unused, unused  # type: ignore[arg-type]
    )

    result = await router.dispatch(context("曲绘猜歌 14+ DX"))

    assert isinstance(result, BotReply)
    assert result.image_png == b"cover-0"
    assert "限定范围：14+ DX" in result.text
    assert catalog.last_random_argument == "master 14+ DX"


async def test_cover_guess_expands_the_same_cover_on_hint() -> None:
    unused = object()
    catalog = GuessCatalogStub()
    router = build_router(
        unused, unused, unused, unused, catalog, unused, unused  # type: ignore[arg-type]
    )

    await router.dispatch(context("曲绘猜歌 14+ DX"))
    first = await router.dispatch(context("提示"))

    assert isinstance(first, BotReply)
    assert first.image_png == b"cover-1"
    assert "曲绘可见范围第 1 次扩大" in first.text
    assert catalog.cover_levels == [0, 1]


async def test_guess_hints_do_not_repeat_selected_scope() -> None:
    unused = object()
    router = build_router(
        unused,
        unused,
        unused,
        unused,
        GuessCatalogStub(),
        unused,
        unused,  # type: ignore[arg-type]
    )

    await router.dispatch(context("/guess 桃代 14+ DX"))
    messages = [str(await router.dispatch(context("提示"))) for _ in range(8)]
    combined = "\n".join(messages)

    assert "初次收录于 PiNK" not in combined
    assert "游戏内等级是 14+" not in combined
    assert "定数是 14.8" not in combined
    assert "属于SD谱面" not in combined


async def test_explicit_difficulty_does_not_force_master() -> None:
    unused = object()
    catalog = GuessCatalogStub()
    router = build_router(
        unused, unused, unused, unused, catalog, unused, unused  # type: ignore[arg-type]
    )

    await router.dispatch(context("/guess 红 13+-14+"))

    assert catalog.last_random_argument == "红 13+-14+"


async def test_guess_help_explains_composable_scopes() -> None:
    unused = object()
    catalog = GuessCatalogStub()
    router = build_router(
        unused, unused, unused, unused, catalog, unused, unused  # type: ignore[arg-type]
    )

    result = await router.dispatch(context("/guess help"))

    assert "桃-紫 DX" in result
    assert "未指定难度时默认 MASTER" in result
    assert catalog.last_random_argument == ""
