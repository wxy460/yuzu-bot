from __future__ import annotations

import asyncio
import hashlib
import io
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageFilter

from .lxns_assets import LxnsAssetError, download_lxns_png

SONG_LIST_URL = "https://maimai.lxns.net/api/v0/maimai/song/list"
ALIAS_LIST_URL = "https://maimai.lxns.net/api/v0/maimai/alias/list"
CHART_STATS_URL = "https://www.diving-fish.com/api/maimaidxprober/chart_stats"
DIFFICULTY_NAMES = ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")
TYPE_ALIASES = {"dx": "dx", "sd": "standard", "standard": "standard", "标准": "standard"}
PLATE_VERSION_ALIASES = {
    "初": ("maimai",),
    "真": ("maimai", "maimai PLUS"),
    "超": ("GreeN",),
    "檄": ("GreeN PLUS",),
    "橙": ("ORANGE",),
    "晓": ("ORANGE PLUS",),
    "暁": ("ORANGE PLUS",),
    "桃": ("PiNK",),
    "樱": ("PiNK PLUS",),
    "櫻": ("PiNK PLUS",),
    "紫": ("MURASAKi",),
    "堇": ("MURASAKi PLUS",),
    "菫": ("MURASAKi PLUS",),
    "白": ("MiLK",),
    "雪": ("MiLK PLUS",),
    "辉": ("FiNALE",),
    "輝": ("FiNALE",),
    "熊": ("舞萌DX",),
    "华": ("舞萌DX",),
    "華": ("舞萌DX",),
    "爽": ("舞萌DX 2021",),
    "煌": ("舞萌DX 2021",),
    "宙": ("舞萌DX 2022",),
    "星": ("舞萌DX 2022",),
    "祭": ("舞萌DX 2023",),
    "祝": ("舞萌DX 2023",),
    "双": ("舞萌DX 2024",),
    "宴": ("舞萌DX 2024",),
    "镜": ("舞萌DX 2025",),
    "鏡": ("舞萌DX 2025",),
    "彩": ("舞萌DX 2026",),
}
DIFFICULTY_ALIASES = {
    "basic": 0,
    "绿": 0,
    "advanced": 1,
    "黄": 1,
    "expert": 2,
    "红": 2,
    "master": 3,
    "mas": 3,
    "紫": 3,
    "remaster": 4,
    "re:master": 4,
    "remas": 4,
    "白": 4,
}


class SongCatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Chart:
    song_id: int
    title: str
    artist: str
    genre: str
    version: int
    chart_type: str
    difficulty: int
    level: str
    constant: float
    designer: str
    tap: int = 0
    hold: int = 0
    slide: int = 0
    touch: int = 0
    break_count: int = 0
    fit_constant: float | None = None

    @property
    def difficulty_name(self) -> str:
        if 0 <= self.difficulty < len(DIFFICULTY_NAMES):
            return DIFFICULTY_NAMES[self.difficulty]
        return f"难度 {self.difficulty}"

    @property
    def type_label(self) -> str:
        return "DX" if self.chart_type == "dx" else "SD"

    @property
    def note_count(self) -> int:
        return self.tap + self.hold + self.slide + self.touch + self.break_count


@dataclass(frozen=True, slots=True)
class Song:
    id: int
    title: str
    artist: str
    genre: str
    bpm: int
    version: int
    charts: tuple[Chart, ...]


class SongCatalogService:
    """Cached read-only access to LXNS's public maimai song catalogue."""

    def __init__(self, http: httpx.AsyncClient, *, cache_seconds: int = 21_600) -> None:
        self._http = http
        self._cache_seconds = cache_seconds
        self._songs: tuple[Song, ...] = ()
        self._versions: dict[int, str] = {}
        self._aliases: dict[int, tuple[str, ...]] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def songs(self) -> tuple[Song, ...]:
        if self._songs and time.monotonic() - self._loaded_at < self._cache_seconds:
            return self._songs
        async with self._lock:
            if self._songs and time.monotonic() - self._loaded_at < self._cache_seconds:
                return self._songs
            try:
                response = await self._http.get(SONG_LIST_URL, params={"notes": "true"}, timeout=30)
                response.raise_for_status()
                payload = response.json()
                fit_stats: dict[str, Any] = {}
                try:
                    stats_response = await self._http.get(CHART_STATS_URL, timeout=30)
                    stats_response.raise_for_status()
                    fit_stats = stats_response.json().get("charts", {})
                except (httpx.HTTPError, TypeError, ValueError, AttributeError):
                    # Fitted constants improve recommendations but are not
                    # required for ordinary catalogue/search functionality.
                    fit_stats = {}
                songs = tuple(
                    _parse_song(item, fit_stats) for item in payload.get("songs", [])
                )
                versions = {
                    int(item["version"]): str(item["title"])
                    for item in payload.get("versions", [])
                    if item.get("version") is not None and item.get("title")
                }
            except (httpx.HTTPError, TypeError, ValueError, KeyError) as exc:
                if self._songs:
                    return self._songs
                raise SongCatalogError(f"读取曲目目录失败：{exc}") from exc
            aliases: dict[int, tuple[str, ...]] = {}
            try:
                alias_response = await self._http.get(ALIAS_LIST_URL, timeout=20)
                alias_response.raise_for_status()
                aliases = {
                    int(item["song_id"]): tuple(str(alias) for alias in item.get("aliases", []))
                    for item in alias_response.json().get("aliases", [])
                    if item.get("song_id") is not None
                }
            except (httpx.HTTPError, TypeError, ValueError, KeyError):
                # Aliases improve discovery but must not make the core song
                # catalogue unavailable when that auxiliary endpoint is down.
                aliases = {}
            self._songs = tuple(song for song in songs if song.charts)
            self._versions = versions
            self._aliases = aliases
            self._loaded_at = time.monotonic()
            return self._songs

    async def search(self, query: str, *, limit: int = 10) -> list[Song]:
        query = query.strip()
        if not query:
            return []
        songs = await self.songs()
        if query.isdecimal():
            song_id = int(query)
            exact = [song for song in songs if song.id == song_id]
            if not exact and song_id >= 10000:
                exact = [song for song in songs if song.id == song_id - 10000]
            if exact:
                return exact
        needle = _normalize(query)
        matches = [
            song
            for song in songs
            if needle in _normalize(song.title)
            or needle in _normalize(song.artist)
            or any(needle in _normalize(alias) for alias in self._aliases.get(song.id, ()))
        ]
        return sorted(
            matches,
            key=lambda song: (
                _normalize(song.title) != needle,
                not _normalize(song.title).startswith(needle),
                len(song.title),
                song.id,
            ),
        )[:limit]

    async def resolve(self, query: str) -> Song | None:
        matches = await self.search(query, limit=20)
        if not matches:
            return None
        needle = _normalize(query.removeprefix("id").strip())
        numeric_ids = {needle}
        if needle.isdecimal() and int(needle) >= 10000:
            numeric_ids.add(str(int(needle) - 10000))
        exact = [
            song
            for song in matches
            if str(song.id) in numeric_ids
            or _normalize(song.title) == needle
            or any(_normalize(alias) == needle for alias in self._aliases.get(song.id, ()))
        ]
        return exact[0] if len(exact) == 1 else matches[0] if len(matches) == 1 else None

    async def aliases_for(self, query: str) -> tuple[Song, tuple[str, ...]] | None:
        song = await self.resolve(query)
        if song is None:
            return None
        return song, self._aliases.get(song.id, ())

    async def alias_matches(self, alias: str) -> list[Song]:
        await self.songs()
        needle = _normalize(alias)
        ids = {
            song_id
            for song_id, aliases in self._aliases.items()
            if any(_normalize(item) == needle for item in aliases)
        }
        return [song for song in self._songs if song.id in ids]

    async def chart(self, query: str, difficulty: int = 3) -> Chart | None:
        song = await self.resolve(query)
        if song is None:
            return None
        candidates = [chart for chart in song.charts if chart.difficulty == difficulty]
        return next((chart for chart in candidates if chart.chart_type == "dx"), None) or (
            candidates[0] if candidates else None
        )

    async def charts_in_range(self, low: float, high: float) -> list[Chart]:
        songs = await self.songs()
        return sorted(
            (chart for song in songs for chart in song.charts if low <= chart.constant <= high),
            key=lambda chart: (chart.constant, chart.song_id, chart.chart_type, chart.difficulty),
        )

    async def random_chart(self, argument: str) -> Chart:
        songs = await self.songs()
        candidates = [chart for song in songs for chart in song.charts]
        words = argument.casefold().split()
        title_words: list[str] = []
        current_version = max((song.version for song in songs), default=0)
        for word in words:
            level_range = re.fullmatch(
                r"(\d{1,2}\+?)(?:-|~|～|至|到)(\d{1,2}\+?)",
                word,
            )
            constant_range = re.fullmatch(
                r"(\d+(?:\.\d+)?)(?:-|~|～|至|到)(\d+(?:\.\d+)?)",
                word,
            )
            version_range = re.fullmatch(
                r"([初真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉熊華华爽煌宙星祭祝双宴镜鏡彩])"
                r"(?:-|~|～|至|到)"
                r"([初真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉熊華华爽煌宙星祭祝双宴镜鏡彩])(?:代)?",
                word,
            )
            if level_range:
                low, high = sorted(_level_rank(value) for value in level_range.groups())
                candidates = [chart for chart in candidates if low <= _level_rank(chart.level) <= high]
            elif constant_range:
                low, high = sorted(float(value) for value in constant_range.groups())
                candidates = [chart for chart in candidates if low <= chart.constant <= high]
            elif version_range:
                low, high = self._version_range(*version_range.groups())
                candidates = [chart for chart in candidates if low <= chart.version <= high]
            elif word in TYPE_ALIASES:
                chart_type = TYPE_ALIASES[word]
                candidates = [chart for chart in candidates if chart.chart_type == chart_type]
            elif word in DIFFICULTY_ALIASES:
                difficulty = DIFFICULTY_ALIASES[word]
                candidates = [chart for chart in candidates if chart.difficulty == difficulty]
            elif word in {"新曲", "当前版本", "new"}:
                candidates = [chart for chart in candidates if chart.version == current_version]
            elif (version_word := word.removesuffix("代")) in PLATE_VERSION_ALIASES:
                version_titles = PLATE_VERSION_ALIASES[version_word]
                # A random "真代" means PLUS; the combined 初+真 rule only
                # applies to the 真-series plate.
                if version_word == "真":
                    version_titles = ("maimai PLUS",)
                candidates = [
                    chart
                    for chart in candidates
                    if self.version_name(chart.version) in version_titles
                ]
            elif re.fullmatch(r"\d{1,2}(?:\+|\.\d)?", word):
                if word.endswith("+") or "." not in word:
                    candidates = [chart for chart in candidates if chart.level == word]
                else:
                    number = float(word)
                    candidates = [chart for chart in candidates if chart.constant == number]
            else:
                title_words.append(word)
        if title_words:
            needle = _normalize(" ".join(title_words))
            candidates = [
                chart
                for chart in candidates
                if needle in _normalize(chart.title) or needle in _normalize(chart.genre)
            ]
        if not candidates:
            raise SongCatalogError("没有符合条件的谱面，请减少筛选条件后重试。")
        return random.SystemRandom().choice(candidates)

    def _version_range(self, start: str, end: str) -> tuple[int, int]:
        def boundaries(mark: str) -> tuple[int, int]:
            titles = PLATE_VERSION_ALIASES[mark]
            ids = [version for version, title in self._versions.items() if title in titles]
            if not ids:
                raise SongCatalogError(f"曲目目录中无法识别版本「{mark}代」。")
            return min(ids), max(ids)

        start_low, start_high = boundaries(start)
        end_low, end_high = boundaries(end)
        return min(start_low, end_low), max(start_high, end_high)

    async def daily(self, user_id: str, day: date | None = None) -> Chart:
        songs = await self.songs()
        candidates = [chart for song in songs for chart in song.charts if chart.difficulty >= 2]
        if not candidates:
            raise SongCatalogError("曲目目录中没有可推荐的谱面。")
        today = day or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        digest = hashlib.sha256(f"{today.isoformat()}:{user_id}".encode()).digest()
        return candidates[int.from_bytes(digest[:8], "big") % len(candidates)]

    async def cover_clue(self, song_id: int, *, reveal_level: int = 0) -> bytes:
        try:
            raw = await download_lxns_png(self._http, "jacket", song_id)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            ratios = (0.22, 0.36, 0.52, 0.72, 1.0)
            level = max(0, min(reveal_level, len(ratios) - 1))
            base_side = min(image.width, image.height)
            side = max(40, round(base_side * ratios[level]))
            digest = hashlib.sha256(f"cover-clue:{song_id}".encode()).digest()
            center_x = image.width * (0.3 + digest[0] / 255 * 0.4)
            center_y = image.height * (0.3 + digest[1] / 255 * 0.4)
            left = round(max(0, min(image.width - side, center_x - side / 2)))
            top = round(max(0, min(image.height - side, center_y - side / 2)))
            clue = image.crop((left, top, left + side, top + side)).resize(
                (480, 480),
                Image.Resampling.LANCZOS,
            )
            blur_radius = (1.2, 0.8, 0.45, 0.15, 0.0)[level]
            if blur_radius:
                clue = clue.filter(ImageFilter.GaussianBlur(radius=blur_radius))
            output = io.BytesIO()
            clue.save(output, format="PNG")
            return output.getvalue()
        except (LxnsAssetError, OSError, ValueError) as exc:
            raise SongCatalogError(f"生成曲绘题目失败：{exc}") from exc

    def version_name(self, version: int) -> str:
        exact = self._versions.get(version)
        if exact is not None:
            return exact
        # LXNS chart versions may contain an in-version update suffix, e.g.
        # Glorious Crown is 16014 while the PiNK base version is 16000.
        # Resolve such values to the nearest known base version below them.
        base = max((item for item in self._versions if item <= version), default=None)
        return self._versions[base] if base is not None else str(version)


def format_song(song: Song, version_name: str) -> str:
    rows = [
        f"{song.title}（ID {song.id}）",
        f"艺术家：{song.artist}",
        f"分类：{song.genre}　BPM：{song.bpm}　版本：{version_name}",
        "谱面：",
    ]
    rows.extend(
        f"{chart.type_label} {chart.difficulty_name} {chart.level}"
        f"（定数 {chart.constant:.1f} / 拟合定数 "
        f"{f'{chart.fit_constant:.2f}' if chart.fit_constant is not None else '—'}）"
        for chart in song.charts
    )
    return "\n".join(rows)


def format_chart(chart: Chart) -> str:
    notes = (
        f"\n物量：{chart.note_count}（TAP {chart.tap} / HOLD {chart.hold} / "
        f"SLIDE {chart.slide} / TOUCH {chart.touch} / BREAK {chart.break_count}）"
        if chart.note_count
        else ""
    )
    return (
        f"{chart.title}（ID {chart.song_id}）\n"
        f"{chart.type_label} {chart.difficulty_name} {chart.level} / 定数 {chart.constant:.1f}\n"
        f"谱师：{chart.designer or '未知'}　分类：{chart.genre}{notes}"
    )


def _level_rank(level: str) -> int:
    match = re.fullmatch(r"(\d{1,2})(\+?)", level.strip())
    if match is None:
        return -1
    return int(match.group(1)) * 2 + bool(match.group(2))


def _parse_song(item: dict[str, Any], fit_stats: dict[str, Any] | None = None) -> Song:
    song_id = int(item["id"])
    title = str(item.get("title") or "未知曲目")
    artist = str(item.get("artist") or "未知")
    genre = str(item.get("genre") or "未知")
    version = int(item.get("version") or 0)
    charts: list[Chart] = []
    difficulties = item.get("difficulties") or {}
    for chart_type in ("standard", "dx"):
        for chart in difficulties.get(chart_type, []) or []:
            notes = chart.get("notes") or {}
            if isinstance(notes, list):
                values = [int(value or 0) for value in notes]
                notes = dict(zip(("tap", "hold", "slide", "touch", "break"), values))
            charts.append(
                Chart(
                    song_id=song_id,
                    title=title,
                    artist=artist,
                    genre=genre,
                    version=int(chart.get("version") or version),
                    chart_type=chart_type,
                    difficulty=int(chart.get("difficulty") or 0),
                    level=str(chart.get("level") or "?"),
                    constant=float(chart.get("level_value") or 0),
                    designer=str(chart.get("note_designer") or ""),
                    tap=int(notes.get("tap") or 0),
                    hold=int(notes.get("hold") or 0),
                    slide=int(notes.get("slide") or 0),
                    touch=int(notes.get("touch") or 0),
                    break_count=int(notes.get("break") or notes.get("brk") or 0),
                    fit_constant=_fit_constant(
                        fit_stats or {}, song_id, chart_type, int(chart.get("difficulty") or 0)
                    ),
                )
            )
    return Song(
        id=song_id,
        title=title,
        artist=artist,
        genre=genre,
        bpm=int(item.get("bpm") or 0),
        version=version,
        charts=tuple(charts),
    )


def _fit_constant(
    stats: dict[str, Any], song_id: int, chart_type: str, difficulty: int
) -> float | None:
    ids = (song_id + 10_000, song_id) if chart_type == "dx" else (song_id,)
    for candidate_id in ids:
        charts = stats.get(str(candidate_id), stats.get(candidate_id))
        if not isinstance(charts, list) or difficulty >= len(charts):
            continue
        item = charts[difficulty]
        if not isinstance(item, dict):
            continue
        try:
            value = float(item.get("fit_diff"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())
