from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from .song_catalog import Chart, Song

logger = logging.getLogger(__name__)

VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
UPLOADER_NAMES = ("Peace_Walker", "DJNaughty", "風又ねリ")

# Small, manually verified overrides stay ahead of the imported metadata. This
# also guarantees a useful result if the external index is accidentally absent.
_VIDEO_SEEDS = (
    {
        "bvid": "BV1rD4y1F72q",
        "title": "【谱面确认】【MAIMAI DX】【Fragrance】 Master 14",
        "source": "DJNaughty",
        "song_title": "Fragrance",
        "artist": "Tsukasa(Arte Refact)",
        "chart_type": "standard",
        "difficulty": 3,
    },
    {
        "bvid": "BV1QjtLesEKc",
        "title": "【谱面确认】【MAIMAI DX】【Fragrance】 Re:Master",
        "source": "DJNaughty",
        "song_title": "Fragrance",
        "artist": "Tsukasa(Arte Refact)",
        "chart_type": "standard",
        "difficulty": 4,
    },
    {
        "bvid": "BV1fi421e7FX",
        "title": "【谱面确认】【MAIMAI DX】【ぽっぴっぽー】 Master 13",
        "source": "DJNaughty（人工核验）",
        "song_title": "ぽっぴっぽー",
        "artist": "",
        "chart_type": "standard",
        "difficulty": 3,
    },
)


@dataclass(frozen=True, slots=True)
class BilibiliVideo:
    bvid: str
    title: str
    uploader: str
    song_title: str = ""
    artist: str = ""
    chart_type: str = ""
    difficulty: int = -1
    source_url: str = ""

    @property
    def direct_url(self) -> str:
        return self.source_url or VIDEO_URL.format(bvid=self.bvid)


@dataclass(frozen=True, slots=True)
class ChartConfirmation:
    chart: Chart
    video: BilibiliVideo | None


@dataclass(frozen=True, slots=True)
class ChartConfirmationResult:
    song_title: str
    entries: tuple[ChartConfirmation, ...]

    def as_text(self) -> str:
        if not self.entries:
            return ""
        rows = [f"{self.song_title}｜B站谱面确认直链："]
        for entry in self.entries:
            label = chart_label(entry.chart)
            if entry.video:
                rows.extend(
                    (f"{label} · {entry.video.uploader}", entry.video.direct_url)
                )
            else:
                rows.append(f"{label}：本地审核索引暂未收录")
        rows.append("来源：本地审核谱面库；查询时不会调用 B 站搜索接口，避免触发风控。")
        return "\n".join(rows)


class BilibiliChartService:
    """Resolve reviewed chart videos exclusively from a local metadata index.

    The HTTP client is retained in the constructor for API compatibility, but
    user queries deliberately perform no Bilibili requests. The previous live
    space-search implementation regularly triggered HTTP 412 risk control.
    """

    def __init__(self, http: httpx.AsyncClient, *, index_path: Path | None = None) -> None:
        self._http = http
        self._index_path = index_path
        self._persistent_videos = self._load_index()

    async def confirmation_links(self, song: Song) -> str:
        return (await self.confirmations(song)).as_text()

    async def confirmations(self, song: Song) -> ChartConfirmationResult:
        charts = sorted(
            (chart for chart in song.charts if chart.difficulty in {3, 4}),
            key=lambda chart: (chart.difficulty, chart.chart_type),
        )
        if not charts:
            return ChartConfirmationResult(song.title, ())

        matched: dict[Chart, BilibiliVideo] = {}
        for chart in charts:
            video = self._find_match(song, chart, self._persistent_videos)
            if video:
                matched[chart] = video

        # Compatibility hook for tests and optional hand-curated archives. The
        # base implementation below is deliberately local-only.
        for uploader in UPLOADER_NAMES:
            if len(matched) == len(charts):
                break
            videos = await self._uploader_videos(0, uploader, song.title)
            for chart in charts:
                if chart not in matched:
                    video = self._find_match(song, chart, videos)
                    if video:
                        matched[chart] = video

        return ChartConfirmationResult(
            song.title,
            tuple(ChartConfirmation(chart, matched.get(chart)) for chart in charts),
        )

    def _find_match(
        self, song: Song, chart: Chart, videos: tuple[BilibiliVideo, ...]
    ) -> BilibiliVideo | None:
        structured = tuple(video for video in videos if video.song_title)
        exact = tuple(
            video
            for video in structured
            if _normalize(video.song_title) == _normalize(song.title)
            and video.chart_type == chart.chart_type
            and video.difficulty == chart.difficulty
        )
        if exact:
            artist_matches = tuple(
                video
                for video in exact
                if not video.artist or _normalize(video.artist) == _normalize(song.artist)
            )
            return (artist_matches or exact)[0]

        # Legacy/manual records contain only a video title. Keep the conservative
        # matcher so small hand-curated overrides remain easy to add.
        types_at_difficulty = {
            item.chart_type for item in song.charts if item.difficulty == chart.difficulty
        }
        require_explicit_type = len(types_at_difficulty) > 1
        return next(
            (
                item
                for item in videos
                if not item.song_title
                and _matches_video(song, chart, item.title, require_explicit_type)
            ),
            None,
        )

    async def _uploader_videos(
        self, uid: int, uploader: str, keyword: str
    ) -> tuple[BilibiliVideo, ...]:
        """Return local legacy entries; never access Bilibili at request time."""
        del uid
        needle = _normalize(keyword)
        return tuple(
            video
            for video in self._persistent_videos
            if video.uploader == uploader
            and not video.song_title
            and needle in _normalize(video.title)
        )

    def _load_index(self) -> tuple[BilibiliVideo, ...]:
        raw: list[object] = list(_VIDEO_SEEDS)
        if self._index_path and self._index_path.exists():
            try:
                loaded = json.loads(self._index_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    loaded = loaded.get("videos", [])
                if isinstance(loaded, list):
                    raw.extend(loaded)
            except (OSError, json.JSONDecodeError):
                logger.warning("无法读取B站谱面直链索引：%s", self._index_path, exc_info=True)

        videos: list[BilibiliVideo] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            video = _parse_video(item)
            if video:
                videos.append(video)
        return _merge_videos((), tuple(videos))


def _parse_video(item: dict[object, object]) -> BilibiliVideo | None:
    bvid = str(item.get("bvid") or item.get("source_id") or "").strip()
    title = str(item.get("title") or item.get("source_title") or "").strip()
    source = str(item.get("source") or item.get("uploader") or "离线审核谱面库").strip()
    song_title = str(item.get("song_title") or "").strip()
    artist = str(item.get("artist") or "").strip()
    chart_type = str(item.get("chart_type") or "").strip()
    try:
        difficulty = int(item.get("difficulty", -1))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        difficulty = -1
    source_url = _safe_video_url(str(item.get("url") or item.get("source_url") or ""), bvid)
    if not re.fullmatch(r"BV[0-9A-Za-z]{10,}", bvid) or not title:
        return None
    if song_title and (chart_type not in {"standard", "dx"} or difficulty not in {3, 4}):
        return None
    return BilibiliVideo(
        bvid=bvid,
        title=title,
        uploader=source or "离线审核谱面库",
        song_title=song_title,
        artist=artist,
        chart_type=chart_type,
        difficulty=difficulty,
        source_url=source_url,
    )


def _safe_video_url(value: str, bvid: str) -> str:
    """Accept only canonical HTTPS Bilibili video URLs from imported data."""
    fallback = VIDEO_URL.format(bvid=bvid)
    if not value:
        return fallback
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in {"www.bilibili.com", "bilibili.com"}:
        return fallback
    if parsed.path.rstrip("/") != f"/video/{bvid}":
        return fallback
    page = parse_qs(parsed.query).get("p", [""])[0]
    return f"{fallback}?p={int(page)}" if page.isdecimal() and int(page) > 0 else fallback


def _matches_video(song: Song, chart: Chart, title: str, require_explicit_type: bool) -> bool:
    normalized_title = _normalize(title)
    if _normalize(song.title) not in normalized_title:
        return False
    is_remaster = "remaster" in normalized_title or "白谱" in title
    is_master = (
        "master" in normalized_title
        or "紫谱" in title
        or bool(re.search(r"(?:^|\s)MA(?:\s|$)", title, re.IGNORECASE))
    )
    if chart.difficulty == 4 and not is_remaster:
        return False
    if chart.difficulty == 3 and (not is_master or is_remaster):
        return False
    explicit_dx = bool(re.search(r"DX\s*谱面|[\[【(（]\s*DX\s*[\]】)）]", title, re.IGNORECASE))
    explicit_sd = bool(re.search(r"(?:标准|SD)\s*谱面", title, re.IGNORECASE))
    if require_explicit_type:
        if chart.chart_type == "dx":
            return explicit_dx
        return explicit_sd or not explicit_dx
    if chart.chart_type == "dx" and explicit_sd:
        return False
    return not (chart.chart_type == "standard" and explicit_dx)


def chart_label(chart: Chart) -> str:
    color = "紫谱" if chart.difficulty == 3 else "白谱"
    difficulty = "MASTER" if chart.difficulty == 3 else "Re:MASTER"
    return f"{color} · {chart.type_label} {difficulty}"


def _merge_videos(
    first: tuple[BilibiliVideo, ...], second: tuple[BilibiliVideo, ...]
) -> tuple[BilibiliVideo, ...]:
    result: list[BilibiliVideo] = []
    seen: set[tuple[str, str, str, int]] = set()
    for video in (*first, *second):
        key = (_normalize(video.song_title), video.chart_type, video.bvid, video.difficulty)
        if key in seen:
            continue
        seen.add(key)
        result.append(video)
    return tuple(result)


def _normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
