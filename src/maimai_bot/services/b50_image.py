from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from .daily_recommendation import DailyRecommendations, RecommendationItem
from .divingfish import Best50, PlayerRecords
from .gameplay import PlateEntry, PlateProgress
from .lxns_assets import LxnsAssetError, download_lxns_png
from .rating import rating_for
from .song_catalog import Chart, Song

logger = logging.getLogger(__name__)

CANVAS_SIZE = (1600, 1900)
FONT_CANDIDATES = (
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)
BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)
DIFFICULTY_COLORS = {
    0: "#42c96b",
    1: "#f2c94c",
    2: "#ef596f",
    3: "#9852d9",
    4: "#d6a3ff",
}
LXNS_PLATE_LIST_URL = "https://maimai.lxns.net/api/v0/maimai/plate/list"
BUNDLED_PLATES = {"橙将": "6113.png"}


class B50RenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CosmeticProfile:
    """Future-proof cosmetic inputs supplied by an optional collection provider.

    Diving-Fish exposes the nickname/rating/plate text used by the renderer,
    but not the selected in-game avatar ID. Explicit cosmetic bytes always take
    precedence over automatically resolved public assets.
    """

    avatar_png: bytes | None = None
    nameplate_png: bytes | None = None
    background_png: bytes | None = None


class B50ImageService:
    def __init__(self, http: httpx.AsyncClient, *, background_path: Path) -> None:
        self._http = http
        self._background_path = background_path
        self._badge_path = background_path.parent / "badges"
        self._rating_path = background_path.parent / "rating"
        self._type_path = background_path.parent / "types"
        self._cover_cache: dict[int, bytes | None] = {}
        self._cover_lock = asyncio.Lock()
        self._cover_slots = asyncio.Semaphore(8)
        self._avatar_cache: dict[str, bytes | None] = {}
        self._avatar_lock = asyncio.Lock()
        self._plate_ids: dict[str, int] | None = None
        self._plate_cache: dict[str, bytes | None] = {}
        self._plate_lock = asyncio.Lock()

    async def render(
        self,
        best50: Best50,
        cosmetics: CosmeticProfile | None = None,
        *,
        avatar_url: str | None = None,
    ) -> bytes:
        cosmetics = cosmetics or CosmeticProfile()
        records = [*best50.old[:35], *best50.new[:15]]
        cover_task = asyncio.gather(*(self._cover_for(item) for item in records))
        plate_task = (
            asyncio.sleep(0, result=cosmetics.nameplate_png)
            if cosmetics.nameplate_png
            else self._nameplate_for(best50.plate)
        )
        avatar_task = (
            asyncio.sleep(0, result=cosmetics.avatar_png)
            if cosmetics.avatar_png or not avatar_url
            else self._avatar_for(avatar_url)
        )
        covers, resolved_plate, resolved_avatar = await asyncio.gather(
            cover_task,
            plate_task,
            avatar_task,
        )
        if (resolved_plate and not cosmetics.nameplate_png) or (
            resolved_avatar and not cosmetics.avatar_png
        ):
            cosmetics = CosmeticProfile(
                avatar_png=cosmetics.avatar_png or resolved_avatar,
                nameplate_png=cosmetics.nameplate_png or resolved_plate,
                background_png=cosmetics.background_png,
            )
        try:
            # Pillow's FreeType backend can deadlock when first initialized in a
            # worker thread on some Arch Linux builds. Rendering takes < 1s at
            # this size, so keep it on the main thread for predictable behavior.
            return self._draw(best50, cosmetics, covers)
        except Exception as exc:
            raise B50RenderError(f"生成 B50 图片失败：{exc}") from exc

    async def render_score_table(
        self,
        player: PlayerRecords,
        selector: str,
        records: list[dict[str, Any]],
        cosmetics: CosmeticProfile | None = None,
        *,
        avatar_url: str | None = None,
    ) -> bytes:
        """Render a B50-style long image containing up to 100 personal scores."""
        cosmetics = cosmetics or CosmeticProfile()
        selected = records[:100]
        cover_task = asyncio.gather(*(self._cover_for(item) for item in selected))
        plate_task = (
            asyncio.sleep(0, result=cosmetics.nameplate_png)
            if cosmetics.nameplate_png
            else self._nameplate_for(player.plate)
        )
        avatar_task = (
            asyncio.sleep(0, result=cosmetics.avatar_png)
            if cosmetics.avatar_png or not avatar_url
            else self._avatar_for(avatar_url)
        )
        covers, resolved_plate, resolved_avatar = await asyncio.gather(
            cover_task,
            plate_task,
            avatar_task,
        )
        cosmetics = CosmeticProfile(
            avatar_png=cosmetics.avatar_png or resolved_avatar,
            nameplate_png=cosmetics.nameplate_png or resolved_plate,
            background_png=cosmetics.background_png,
        )
        try:
            return self._draw_score_table(player, selector, selected, cosmetics, list(covers))
        except Exception as exc:
            raise B50RenderError(f"生成分表图片失败：{exc}") from exc

    async def render_record_list(
        self,
        title: str,
        records: list[dict[str, Any]],
        *,
        subtitle: str = "",
    ) -> bytes:
        """Render a one-column score/catalogue list using the B50 visual language."""
        displayed = records[:50]
        covers = await asyncio.gather(
            *(
                asyncio.sleep(0, result=item.get("_image_png"))
                if item.get("_image_png")
                else self._cover_for(item)
                for item in displayed
            )
        )
        try:
            return self._draw_record_list(title, subtitle, displayed, list(covers))
        except Exception as exc:
            raise B50RenderError(f"生成列表图片失败：{exc}") from exc

    async def render_plate_progress(
        self,
        player: PlayerRecords,
        progress: PlateProgress,
        cosmetics: CosmeticProfile | None = None,
        *,
        avatar_url: str | None = None,
    ) -> bytes:
        """Render an illustrated plate-progress report with up to eight blockers."""
        cosmetics = cosmetics or CosmeticProfile()
        displayed = list(progress.unfinished[:8])
        cover_task = asyncio.gather(*(self._cover_for(_plate_record(entry)) for entry in displayed))
        plate_task = (
            asyncio.sleep(0, result=cosmetics.nameplate_png)
            if cosmetics.nameplate_png
            else self._nameplate_for(player.plate)
        )
        target_plate_task = self._nameplate_for(progress.plate)
        avatar_task = (
            asyncio.sleep(0, result=cosmetics.avatar_png)
            if cosmetics.avatar_png or not avatar_url
            else self._avatar_for(avatar_url)
        )
        covers, resolved_plate, target_plate, resolved_avatar = await asyncio.gather(
            cover_task,
            plate_task,
            target_plate_task,
            avatar_task,
        )
        cosmetics = CosmeticProfile(
            avatar_png=cosmetics.avatar_png or resolved_avatar,
            nameplate_png=cosmetics.nameplate_png or resolved_plate,
            background_png=cosmetics.background_png,
        )
        try:
            return self._draw_plate_progress(
                player,
                progress,
                displayed,
                cosmetics,
                list(covers),
                target_plate,
            )
        except Exception as exc:
            raise B50RenderError(f"生成牌子进度图片失败：{exc}") from exc

    async def render_song_info(
        self,
        song: Song,
        version_name: str,
        *,
        headline: str = "谱面详情",
        featured_chart: Chart | None = None,
    ) -> bytes:
        """Render a single-song catalogue result as a maimai-styled detail card."""
        cover = await self._cover_for({"song_id": song.id})
        try:
            return self._draw_song_info(song, version_name, cover, headline, featured_chart)
        except Exception as exc:
            raise B50RenderError(f"生成谱面详情图片失败：{exc}") from exc

    async def render_daily_recommendations(self, plan: DailyRecommendations) -> bytes:
        """Render ten personalised daily practice charts as a compact long image."""
        covers = await asyncio.gather(
            *(self._cover_for({"song_id": item.chart.song_id}) for item in plan.items)
        )
        try:
            return self._draw_daily_recommendations(plan, list(covers))
        except Exception as exc:
            raise B50RenderError(f"生成每日推歌图片失败：{exc}") from exc

    def render_help(self, commands: tuple[Any, ...]) -> bytes:
        """Render the complete command list for clients that cannot open the web help."""
        try:
            return self._draw_help(commands)
        except Exception as exc:
            raise B50RenderError(f"生成帮助菜单图片失败：{exc}") from exc

    async def _cover_for(self, record: dict[str, Any]) -> bytes | None:
        try:
            song_id = int(record.get("song_id", 0))
        except (TypeError, ValueError):
            return None
        if song_id <= 0:
            return None
        async with self._cover_lock:
            if song_id in self._cover_cache:
                return self._cover_cache[song_id]
        async with self._cover_slots:
            # Another task may have populated the cache while this one waited.
            async with self._cover_lock:
                if song_id in self._cover_cache:
                    return self._cover_cache[song_id]
            content = await self._download_cover(song_id)
            # Never cache failures. A transient CDN/network error must be able
            # to recover on the next /b50 instead of becoming a permanent hole.
            if content is not None:
                async with self._cover_lock:
                    if len(self._cover_cache) >= 256:
                        self._cover_cache.pop(next(iter(self._cover_cache)))
                    self._cover_cache[song_id] = content
            return content

    async def _download_cover(self, song_id: int) -> bytes | None:
        candidates = _cover_id_candidates(song_id)
        for candidate_index, candidate in enumerate(candidates):
            url = f"https://www.diving-fish.com/covers/{candidate:05d}.png"
            attempts = 3 if candidate_index == 0 else 1
            for attempt in range(attempts):
                try:
                    response = await self._http.get(url, timeout=20)
                    response.raise_for_status()
                    content = _normalize_image_png(response.content)
                    if content is not None:
                        return content
                    logger.warning("曲目 %s 的水鱼封面响应不是有效图片", candidate)
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status not in (408, 425, 429) and status < 500:
                        break
                except httpx.RequestError:
                    pass
                if attempt < attempts - 1:
                    await asyncio.sleep(0.35 * (attempt + 1))

        # Diving-Fish encodes DX chart IDs as 1xxxx, whereas LXNS stores the
        # corresponding jacket under the base song ID (for example 11860 ->
        # 1860). Try that canonical ID first, then the raw ID for completeness.
        for candidate in reversed(candidates):
            try:
                content = await download_lxns_png(self._http, "jacket", candidate)
                logger.info("曲目 %s 使用 LXNS 曲绘后备源（素材 ID %s）", song_id, candidate)
                return content
            except LxnsAssetError:
                continue
        logger.info("曲目 %s 的封面暂不可用，使用占位图且不缓存失败", song_id)
        return None

    async def _avatar_for(self, url: str) -> bytes | None:
        if not url.startswith("https://"):
            return None
        async with self._avatar_lock:
            if url in self._avatar_cache:
                return self._avatar_cache[url]
        try:
            response = await self._http.get(url, timeout=15)
            response.raise_for_status()
            content = response.content if len(response.content) <= 5_000_000 else None
        except httpx.HTTPError:
            logger.info("QQ 头像暂不可用，使用默认头像")
            content = None
        async with self._avatar_lock:
            if len(self._avatar_cache) >= 256:
                self._avatar_cache.pop(next(iter(self._avatar_cache)))
            self._avatar_cache[url] = content
        return content

    async def _nameplate_for(self, plate_name: str) -> bytes | None:
        """Resolve a Diving-Fish plate title to the matching original-game asset."""
        name = plate_name.strip()
        use_default = not name or name == "舞萌玩家"
        if use_default:
            name = "デフォルト"
        bundled_name = BUNDLED_PLATES.get(name)
        if bundled_name:
            bundled_path = self._background_path.parent / "plates" / bundled_name
            try:
                return bundled_path.read_bytes()
            except OSError:
                logger.warning("本地姓名框素材 %s 无法读取，尝试在线获取", bundled_path)
        async with self._plate_lock:
            if name in self._plate_cache:
                return self._plate_cache[name]
            try:
                if self._plate_ids is None:
                    response = await self._http.get(LXNS_PLATE_LIST_URL, timeout=20)
                    response.raise_for_status()
                    plates = response.json().get("plates", [])
                    self._plate_ids = {
                        str(item["name"]): int(item["id"])
                        for item in plates
                        if item.get("name") and item.get("id") is not None
                    }
                plate_id = 1 if use_default else self._plate_ids.get(name)
                if plate_id is None:
                    content = None
                else:
                    content = await download_lxns_png(self._http, "plate", plate_id)
            except (
                httpx.HTTPError,
                LxnsAssetError,
                TypeError,
                ValueError,
                AttributeError,
            ):
                logger.info("姓名框 %s 的原游戏素材暂不可用，使用文字样式", name)
                content = None
            # Do not make a transient CDN/catalogue failure permanent for the
            # lifetime of the bot. Successful assets can still be reused.
            if content is not None:
                self._plate_cache[name] = content
            return content

    def _draw_help(self, commands: tuple[Any, ...]) -> bytes:
        categories: dict[str, list[Any]] = {}
        for command in commands:
            categories.setdefault(str(command.category), []).append(command)

        section_heights = {
            name: 58 + ((len(items) + 1) // 2) * 98 + 18 for name, items in categories.items()
        }
        canvas_size = (1600, 245 + sum(section_heights.values()) + 120)
        background = Image.open(self._background_path).convert("RGB")
        canvas = ImageOps.fit(background, canvas_size, method=Image.Resampling.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=1.0))
        overlay = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            (38, 30, 1562, canvas_size[1] - 30),
            radius=40,
            fill=(246, 248, 255, 235),
            outline=(255, 255, 255, 250),
            width=5,
        )

        draw.rounded_rectangle((70, 58, 1530, 196), radius=34, fill=(42, 37, 83, 238))
        draw.rounded_rectangle((70, 58, 890, 69), radius=5, fill="#60ded1")
        draw.rounded_rectangle((890, 58, 1530, 69), radius=5, fill="#d995ee")
        draw.text((112, 80), "柚子菜单", font=_font(49, bold=True), fill="white")
        draw.text(
            (112, 143),
            "舞萌 Bot 指令中心 · 指令前的 / 不可省略",
            font=_font(23, bold=True),
            fill="#d8d4fa",
        )
        tip = "@机器人 + 普通文字：和藤泽柚子聊天"
        tip_font = _font(24, bold=True)
        draw.text(
            (1488 - draw.textlength(tip, font=tip_font), 111),
            tip,
            font=tip_font,
            fill="#91ece3",
        )

        y = 225
        accents = ("#6d63d9", "#8e55d7", "#3d9f9a", "#d36f9e")
        for section_index, (category, items) in enumerate(categories.items()):
            accent = accents[section_index % len(accents)]
            draw.rounded_rectangle((72, y, 1528, y + 48), radius=24, fill=accent)
            draw.text((102, y + 8), category, font=_font(25, bold=True), fill="white")
            y += 62
            for index, command in enumerate(items):
                col, row = index % 2, index // 2
                x = 74 + col * 730
                card_y = y + row * 98
                draw.rounded_rectangle(
                    (x, card_y, x + 708, card_y + 82),
                    radius=20,
                    fill=(255, 255, 255, 238),
                    outline=accent,
                    width=3,
                )
                usage = str(command.usage or f"/{command.name}")
                usage = _fit_text(draw, usage, _font(23, bold=True), 655)
                description = _fit_text(draw, str(command.description), _font(19), 655)
                draw.text((x + 22, card_y + 12), usage, font=_font(23, bold=True), fill=accent)
                draw.text((x + 22, card_y + 48), description, font=_font(19), fill="#4d4868")
            y += ((len(items) + 1) // 2) * 98 + 16

        footer_y = canvas_size[1] - 92
        footer = "无法打开网页也没关系：/help 永远会直接发送这张完整菜单"
        footer_font = _font(22, bold=True)
        footer_width = draw.textlength(footer, font=footer_font)
        draw.text(
            ((1600 - footer_width) / 2, footer_y),
            footer,
            font=footer_font,
            fill="#5a527d",
        )

        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_record_list(
        self,
        title: str,
        subtitle: str,
        records: list[dict[str, Any]],
        covers: list[bytes | None],
    ) -> bytes:
        row_height = 150
        canvas_size = (1200, 245 + max(1, len(records)) * row_height + 100)
        source = Image.open(self._background_path).convert("RGB")
        tile = ImageOps.fit(source, (1200, 1500), method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", canvas_size)
        for y in range(0, canvas_size[1], tile.height):
            canvas.paste(tile, (0, y))
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.65))
        overlay = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            (34, 28, 1166, canvas_size[1] - 28),
            radius=36,
            fill=(246, 248, 255, 232),
            outline=(255, 255, 255, 248),
            width=4,
        )

        draw.rounded_rectangle((62, 54, 1138, 190), radius=30, fill=(42, 37, 83, 238))
        draw.rounded_rectangle((62, 54, 660, 64), radius=5, fill="#61ded2")
        draw.rounded_rectangle((660, 54, 1138, 64), radius=5, fill="#d995ee")
        draw.text(
            (98, 78),
            _fit_text(draw, title, _font(42, bold=True), 1000),
            font=_font(42, bold=True),
            fill="white",
        )
        if subtitle:
            draw.text(
                (100, 139),
                _fit_text(draw, subtitle, _font(21, bold=True), 990),
                font=_font(21, bold=True),
                fill="#c9c5f3",
            )

        start_y = 214
        if not records:
            message = "没有符合条件的结果"
            width = draw.textlength(message, font=_font(32, bold=True))
            draw.text(
                ((1200 - width) / 2, start_y + 45),
                message,
                font=_font(32, bold=True),
                fill="#645b85",
            )
        for index, (record, cover_bytes) in enumerate(zip(records, covers, strict=True), 1):
            self._draw_record_list_row(
                draw, overlay, record, cover_bytes, index, start_y + (index - 1) * row_height
            )

        footer_y = canvas_size[1] - 72
        credit = "List Visual Design by 酸柚子制糖 · Generated by Fujisawa Yuzu"
        font = _font(18, bold=True)
        width = draw.textlength(credit, font=font)
        draw.text(((1200 - width) / 2, footer_y), credit, font=font, fill="#5a527d")
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_record_list_row(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        record: dict[str, Any],
        cover_bytes: bytes | None,
        index: int,
        y: int,
    ) -> None:
        x, width, height = 72, 1056, 132
        level_index = int(record.get("level_index", 3) or 3)
        accent = DIFFICULTY_COLORS.get(level_index, "#9852d9")
        draw.rounded_rectangle(
            (x + 7, y + 8, x + width + 7, y + height + 8), radius=19, fill=(43, 34, 77, 65)
        )
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=18,
            fill=(255, 255, 255, 244),
            outline=accent,
            width=4,
        )
        cover = _cover_image(cover_bytes, 116, accent, radius=16)
        overlay.alpha_composite(cover, (x + 8, y + 8))

        tx = x + 142
        draw.text((tx, y + 10), f"#{index}", font=_font(18, bold=True), fill="#655d7d")
        type_name = str(record.get("type") or "DX").casefold()
        type_asset = "standard" if type_name in {"sd", "standard", "标准"} else "dx"
        type_badge = _badge_image(self._type_path / f"{type_asset}.webp", (64, 22))
        if type_badge:
            overlay.alpha_composite(type_badge, (tx + 52, y + 7))
        else:
            draw.text(
                (tx + 55, y + 10),
                "SD" if type_asset == "standard" else "DX",
                font=_font(17, bold=True),
                fill="#655d7d",
            )
        title_text = _fit_text(
            draw, str(record.get("title") or "未知曲目"), _font(27, bold=True), 675
        )
        draw.text((tx + 130, y + 5), title_text, font=_font(27, bold=True), fill="#29233e")

        achievement = record.get("achievements")
        value_text = str(record.get("_list_value") or "")
        if achievement is not None:
            try:
                value_text = f"{float(achievement):.4f}%"
            except (TypeError, ValueError):
                pass
        if value_text:
            draw.text(
                (tx, y + 44),
                _fit_text(draw, value_text, _font(31, bold=True), 445),
                font=_font(31, bold=True),
                fill=accent,
            )

        level = str(record.get("level") or "?")
        ds = record.get("ds")
        try:
            ds_text = f"{float(ds):.1f}"
        except (TypeError, ValueError):
            ds_text = "?"
        detail = str(
            record.get("_list_detail")
            or f"Lv.{level} · 定数 {ds_text} · ID {record.get('song_id', '?')}"
        )
        draw.text(
            (tx, y + 91),
            _fit_text(draw, detail, _font(18, bold=True), 690),
            font=_font(18, bold=True),
            fill="#5d5570",
        )
        extra = str(record.get("_list_extra") or "")
        if extra:
            extra_text = _fit_text(draw, extra, _font(21, bold=True), 260)
            extra_w = draw.textlength(extra_text, font=_font(21, bold=True))
            draw.text(
                (x + width - 24 - extra_w, y + 49),
                extra_text,
                font=_font(21, bold=True),
                fill="#524a75",
            )
        if achievement is not None:
            self._draw_score_badges(overlay, record, x + width - 132, y + 94)

    def _draw_song_info(
        self,
        song: Song,
        version_name: str,
        cover_bytes: bytes | None,
        headline: str,
        featured_chart: Chart | None,
    ) -> bytes:
        charts = sorted(song.charts, key=lambda item: (item.difficulty, item.chart_type))
        chart_top = 520
        chart_header_h = 62
        chart_row_h = 92
        chart_bottom = chart_top + chart_header_h + len(charts) * chart_row_h
        rating_charts = [chart for chart in charts if chart.difficulty >= 2]
        rating_top = chart_bottom + 34
        rating_header_h = 68
        rating_row_h = 74
        rating_bottom = rating_top + rating_header_h + len(rating_charts) * rating_row_h
        footer_y = rating_bottom + 38
        canvas_size = (1400, max(1250, footer_y + 100))

        background = Image.open(self._background_path).convert("RGB")
        canvas = ImageOps.fit(background, canvas_size, method=Image.Resampling.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.8))
        overlay = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            (34, 28, 1366, canvas_size[1] - 28),
            radius=36,
            fill=(246, 248, 255, 225),
            outline=(255, 255, 255, 245),
            width=4,
        )

        title_box = (440, 48, 960, 126)
        draw.rounded_rectangle(title_box, radius=38, fill="#7568e8", outline="#55ded1", width=8)
        title = headline
        title_font = _font(39, bold=True)
        title_width = draw.textlength(title, font=title_font)
        draw.text(((1400 - title_width) / 2, 62), title, font=title_font, fill="white")

        info_box = (70, 158, 1330, 486)
        draw.rounded_rectangle(
            info_box, radius=28, fill=(244, 252, 255, 245), outline="#53ddd3", width=6
        )
        cover = _cover_image(cover_bytes, 280, "#53ddd3", radius=24)
        overlay.alpha_composite(cover, (92, 182))

        info_x = 410
        song_title = _fit_text(draw, song.title, _font(37, bold=True), 855)
        draw.text((info_x, 192), song_title, font=_font(37, bold=True), fill="#6559d8")
        draw.line((info_x, 244, 1280, 244), fill="#796de6", width=5)
        artist = _fit_text(draw, song.artist or "未知艺术家", _font(27, bold=True), 850)
        draw.text((info_x, 260), artist, font=_font(27, bold=True), fill="#655d9a")

        draw.rounded_rectangle((info_x, 320, 610, 374), radius=25, fill=(113, 104, 232, 28))
        draw.text((432, 329), f"BPM  {song.bpm or '?'}", font=_font(25, bold=True), fill="#6559d8")
        draw.rounded_rectangle((625, 320, 850, 374), radius=25, fill=(83, 221, 211, 35))
        draw.text((650, 329), f"ID  {song.id}", font=_font(25, bold=True), fill="#3d958e")

        types = sorted({chart.chart_type for chart in charts})
        type_x = 875
        for chart_type in types:
            type_asset = "dx" if chart_type == "dx" else "standard"
            badge = _badge_image(self._type_path / f"{type_asset}.webp", (105, 35))
            if badge:
                overlay.alpha_composite(badge, (type_x, 329))
            else:
                draw.text(
                    (type_x, 330),
                    "DX" if chart_type == "dx" else "SD",
                    font=_font(24, bold=True),
                    fill="#6559d8",
                )
            type_x += 122

        genre = _fit_text(draw, f"分类  {song.genre or '未知'}", _font(23, bold=True), 420)
        version = _fit_text(draw, f"版本  {version_name}", _font(23, bold=True), 420)
        draw.text((info_x, 400), genre, font=_font(23, bold=True), fill="#4f496c")
        draw.text((850, 400), version, font=_font(23, bold=True), fill="#4f496c")

        columns = (
            ("难度 / 类型", 240),
            ("定数", 110),
            ("TOTAL", 110),
            ("TAP", 100),
            ("HOLD", 100),
            ("SLIDE", 100),
            ("TOUCH", 100),
            ("BREAK", 100),
            ("谱师", 300),
        )
        self._draw_song_table(
            draw,
            overlay,
            charts,
            chart_top,
            columns,
            chart_header_h,
            chart_row_h,
            featured_chart,
        )
        if rating_charts:
            self._draw_song_rating_table(
                draw,
                overlay,
                rating_charts,
                rating_top,
                rating_header_h,
                rating_row_h,
            )

        draw.rounded_rectangle(
            (70, footer_y, 1330, footer_y + 62), radius=28, fill=(46, 40, 91, 226)
        )
        credit = "Song Detail Visual Design by 酸柚子制糖 · Generated by Fujisawa Yuzu"
        credit_font = _font(20, bold=True)
        credit_width = draw.textlength(credit, font=credit_font)
        draw.text(
            ((1400 - credit_width) / 2, footer_y + 18),
            credit,
            font=credit_font,
            fill="white",
        )

        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_daily_recommendations(
        self,
        plan: DailyRecommendations,
        covers: list[bytes | None],
    ) -> bytes:
        canvas_size = (1400, 1760)
        background = Image.open(self._background_path).convert("RGB")
        canvas = ImageOps.fit(background, canvas_size, method=Image.Resampling.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.7))
        overlay = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            (35, 30, 1365, 1728),
            radius=34,
            fill=(245, 247, 255, 232),
            outline=(255, 255, 255, 248),
            width=4,
        )

        draw.text((74, 62), "每日推歌", font=_font(54, bold=True), fill="#292342")
        draw.text(
            (76, 128),
            f"{plan.player_name} · {plan.day:%Y-%m-%d}",
            font=_font(23, bold=True),
            fill="#5d5578",
        )
        summary_box = (745, 55, 1325, 174)
        draw.rounded_rectangle(summary_box, radius=25, fill=(42, 37, 83, 238))
        draw.rounded_rectangle((745, 55, 1035, 65), radius=5, fill="#61ded2")
        draw.rounded_rectangle((1035, 55, 1325, 65), radius=5, fill="#d995ee")
        draw.line((1035, 73, 1035, 158), fill=(255, 255, 255, 75), width=2)
        self._draw_rating_total(draw, "DX RATING", str(plan.player_rating), 778, 78, "#8ce9df")
        self._draw_rating_total(
            draw, "推荐中心定数", f"{plan.target_constant:.1f}", 1068, 78, "#e6adf4"
        )
        draw.rounded_rectangle((72, 196, 1328, 252), radius=25, fill=(104, 88, 187, 225))
        subtitle = "困难谱目标 SSS · 易谱目标 SSS+ · 按水鱼拟合定数与 B35 / B15 增量推荐"
        subtitle_w = draw.textlength(subtitle, font=_font(23, bold=True))
        draw.text(((1400 - subtitle_w) / 2, 210), subtitle, font=_font(23, bold=True), fill="white")

        card_w, card_h = 615, 245
        gap_x, gap_y = 26, 22
        start_x, start_y = 72, 282
        for index, (item, cover_bytes) in enumerate(zip(plan.items, covers, strict=True), 1):
            col, row = (index - 1) % 2, (index - 1) // 2
            x = start_x + col * (card_w + gap_x)
            y = start_y + row * (card_h + gap_y)
            self._draw_daily_card(draw, overlay, item, cover_bytes, index, x, y, card_w, card_h)

        note_y = start_y + 5 * (card_h + gap_y) + 2
        draw.rounded_rectangle((72, note_y, 1328, note_y + 70), radius=26, fill=(42, 37, 83, 226))
        note = "每天刷新一次 · 同一用户当天结果固定 · 配置标签来自 LXNS 完整 Simai 谱面"
        note_w = draw.textlength(note, font=_font(20, bold=True))
        draw.text(((1400 - note_w) / 2, note_y + 21), note, font=_font(20, bold=True), fill="white")
        credit = "Daily Recommendation Visual Design by 酸柚子制糖 · Generated by Fujisawa Yuzu"
        credit_w = draw.textlength(credit, font=_font(18, bold=True))
        draw.text(((1400 - credit_w) / 2, 1687), credit, font=_font(18, bold=True), fill="#51496d")

        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_daily_card(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        item: RecommendationItem,
        cover_bytes: bytes | None,
        index: int,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        chart = item.chart
        tier_colors = {
            "简单": (67, 194, 112, 255),
            "中等": (235, 179, 53, 255),
            "困难": (235, 91, 108, 255),
        }
        accent = tier_colors.get(item.tier, (117, 104, 232, 255))
        draw.rounded_rectangle(
            (x + 5, y + 8, x + width + 5, y + height + 8),
            radius=22,
            fill=(43, 34, 75, 60),
        )
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=20,
            fill=(255, 255, 255, 246),
            outline=accent,
            width=4,
        )
        cover = _cover_image(cover_bytes, 174, "#7568e8", radius=18)
        overlay.alpha_composite(cover, (x + 16, y + 55))

        pill = (x + 16, y + 13, x + 102, y + 47)
        draw.rounded_rectangle(pill, radius=17, fill=accent)
        tier_w = draw.textlength(item.tier, font=_font(18, bold=True))
        draw.text((x + 59 - tier_w / 2, y + 18), item.tier, font=_font(18, bold=True), fill="white")
        draw.text((x + 113, y + 16), f"#{index}", font=_font(19, bold=True), fill="#5f5877")

        tx = x + 207
        title = _fit_text(draw, chart.title, _font(25, bold=True), width - 226)
        draw.text((tx, y + 15), title, font=_font(25, bold=True), fill="#29233e")
        type_asset = "dx" if chart.chart_type == "dx" else "standard"
        badge = _badge_image(self._type_path / f"{type_asset}.webp", (72, 25))
        if badge:
            overlay.alpha_composite(badge, (tx, y + 54))
        else:
            draw.text((tx, y + 54), chart.type_label, font=_font(18, bold=True), fill="#6559d8")
        fit_text = f" / 拟{chart.fit_constant:.2f}" if chart.fit_constant is not None else ""
        difficulty_short = {
            "EXPERT": "EXP",
            "MASTER": "MAS",
            "Re:MASTER": "Re:M",
        }.get(chart.difficulty_name, chart.difficulty_name)
        chart_line = f"{difficulty_short} Lv.{chart.level} · 官{chart.constant:.1f}{fit_text}"
        chart_line = _fit_text(draw, chart_line, _font(18, bold=True), width - 309)
        draw.text((tx + 83, y + 55), chart_line, font=_font(18, bold=True), fill="#6559d8")

        current = (
            "当前  未游玩"
            if item.current_achievement is None
            else f"当前  {item.current_achievement:.4f}%  ·  RA {item.current_rating}"
        )
        draw.text((tx, y + 92), current, font=_font(21, bold=True), fill="#3280ab")
        target = f"目标  {item.target_achievement:.4f}%  ·  单谱 RA {item.target_rating}"
        draw.text((tx, y + 125), target, font=_font(21, bold=True), fill="#d78b0e")
        position = "已在榜提升" if item.in_best else "替换当前底分"
        gain = f"{item.pool} {position} · 总 Rating +{item.gain}"
        draw.text((tx, y + 158), gain, font=_font(18, bold=True), fill="#458a58")
        pattern = _fit_text(draw, item.pattern or "公开物量画像", _font(17, bold=True), width - 226)
        draw.rounded_rectangle(
            (tx, y + 191, x + width - 15, y + 226), radius=14, fill=(117, 104, 232, 24)
        )
        draw.text((tx + 10, y + 198), pattern, font=_font(17, bold=True), fill="#51496d")
        draw.text(
            (x + 23, y + 228), f"ID {chart.song_id}", font=_font(13, bold=True), fill="#726a88"
        )

    def _draw_song_table(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        charts: list[Chart],
        top: int,
        columns: tuple[tuple[str, int], ...],
        header_h: int,
        row_h: int,
        featured_chart: Chart | None,
    ) -> None:
        left, right = 70, 1330
        bottom = top + header_h + len(charts) * row_h
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=24,
            fill=(248, 255, 253, 242),
            outline="#53ddd3",
            width=5,
        )
        draw.rounded_rectangle((left, top, right, top + header_h), radius=22, fill="#7568e8")
        x = left
        for label, width in columns:
            label_font = _font(20, bold=True)
            label_width = draw.textlength(label, font=label_font)
            draw.text(
                (x + (width - label_width) / 2, top + 18), label, font=label_font, fill="white"
            )
            x += width
            if x < right:
                draw.line((x, top, x, bottom), fill="#53ddd3", width=3)

        for index, chart in enumerate(charts):
            y = top + header_h + index * row_h
            accent = DIFFICULTY_COLORS.get(chart.difficulty, "#9852d9")
            if index:
                draw.line((left, y, right, y), fill="#53ddd3", width=3)
            draw.rectangle((left + 3, y, left + 239, y + row_h), fill=accent)
            draw.text(
                (left + 18, y + 11), chart.difficulty_name, font=_font(21, bold=True), fill="white"
            )
            draw.text(
                (left + 18, y + 48), f"Lv.{chart.level}", font=_font(24, bold=True), fill="white"
            )
            type_asset = "dx" if chart.chart_type == "dx" else "standard"
            badge = _badge_image(self._type_path / f"{type_asset}.webp", (72, 24))
            if badge:
                overlay.alpha_composite(badge, (left + 150, y + 51))

            values = (
                f"{chart.constant:.1f}",
                str(chart.note_count),
                str(chart.tap),
                str(chart.hold),
                str(chart.slide),
                str(chart.touch),
                str(chart.break_count),
            )
            x = left + columns[0][1]
            for value, (_, width) in zip(values, columns[1:8], strict=True):
                value_font = _font(24, bold=True)
                value_width = draw.textlength(value, font=value_font)
                draw.text(
                    (x + (width - value_width) / 2, y + 31), value, font=value_font, fill="#665bd4"
                )
                x += width
            designer = _fit_text(
                draw, chart.designer or "未知", _font(19, bold=True), columns[-1][1] - 24
            )
            draw.text((x + 12, y + 33), designer, font=_font(19, bold=True), fill="#4d4765")
            if featured_chart and _same_chart(chart, featured_chart):
                draw.rounded_rectangle(
                    (left + 2, y + 2, right - 2, y + row_h - 2),
                    radius=12,
                    outline="#f2ad35",
                    width=7,
                )

    def _draw_song_rating_table(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        charts: list[Chart],
        top: int,
        header_h: int,
        row_h: int,
    ) -> None:
        left, right = 70, 1330
        label_width = 420
        ranks = (
            ("sssp", 100.5),
            ("sss", 100.0),
            ("ssp", 99.5),
            ("ss", 99.0),
            ("sp", 98.0),
            ("s", 97.0),
        )
        rank_width = (right - left - label_width) // len(ranks)
        bottom = top + header_h + len(charts) * row_h
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=24,
            fill=(250, 252, 255, 242),
            outline="#53ddd3",
            width=5,
        )
        draw.rounded_rectangle(
            (left, top, left + label_width, top + header_h), radius=22, fill="#7568e8"
        )
        draw.text((left + 92, top + 19), "单谱 Rating", font=_font(24, bold=True), fill="white")
        for index, (rank, achievement) in enumerate(ranks):
            x = left + label_width + index * rank_width
            draw.rectangle((x, top, x + rank_width, top + header_h), fill="#7568e8")
            badge = _badge_image(self._badge_path / f"rank-{rank}.png", (76, 36))
            if badge:
                overlay.alpha_composite(badge, (x + (rank_width - badge.width) // 2, top + 7))
            draw.text(
                (x + 43, top + 44), f"{achievement:g}%", font=_font(12, bold=True), fill="white"
            )
            draw.line((x, top, x, bottom), fill="#53ddd3", width=3)

        for index, chart in enumerate(charts):
            y = top + header_h + index * row_h
            if index:
                draw.line((left, y, right, y), fill="#53ddd3", width=3)
            accent = DIFFICULTY_COLORS.get(chart.difficulty, "#9852d9")
            draw.rectangle((left + 3, y, left + 145, y + row_h), fill=accent)
            draw.text(
                (left + 15, y + 23), chart.difficulty_name, font=_font(18, bold=True), fill="white"
            )
            designer = _fit_text(draw, chart.designer or "未知", _font(18, bold=True), 245)
            draw.text((left + 158, y + 24), designer, font=_font(18, bold=True), fill="#514a6a")
            for rank_index, (_, achievement) in enumerate(ranks):
                value = str(rating_for(chart.constant, achievement))
                x = left + label_width + rank_index * rank_width
                value_font = _font(23, bold=True)
                value_width = draw.textlength(value, font=value_font)
                draw.text(
                    (x + (rank_width - value_width) / 2, y + 22),
                    value,
                    font=value_font,
                    fill="#665bd4",
                )

    def _draw(
        self,
        best50: Best50,
        cosmetics: CosmeticProfile,
        covers: list[bytes | None],
    ) -> bytes:
        background_bytes = cosmetics.background_png
        if background_bytes:
            background = Image.open(io.BytesIO(background_bytes)).convert("RGB")
        else:
            background = Image.open(self._background_path).convert("RGB")
        canvas = ImageOps.fit(background, CANVAS_SIZE, method=Image.Resampling.LANCZOS)
        overlay = Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        self._draw_header(draw, overlay, best50, cosmetics)
        old_records = best50.old[:35]
        new_records = best50.new[:15]
        self._draw_section_label(draw, "BEST 35 · 旧版本", 225)
        for index, record in enumerate(old_records):
            self._draw_card(draw, overlay, record, covers[index], index + 1, 290, index)

        self._draw_section_label(draw, "NEW 15 · 当前版本", 1205)
        offset = len(old_records)
        for index, record in enumerate(new_records):
            self._draw_card(
                draw,
                overlay,
                record,
                covers[offset + index],
                index + 1,
                1270,
                index,
            )

        self._draw_footer(draw)
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_score_table(
        self,
        player: PlayerRecords,
        selector: str,
        records: list[dict[str, Any]],
        cosmetics: CosmeticProfile,
        covers: list[bytes | None],
    ) -> bytes:
        rows = max(1, (len(records) + 4) // 5)
        footer_y = 290 + rows * 130 + 30
        canvas_size = (1600, footer_y + 120)
        background_bytes = cosmetics.background_png
        if background_bytes:
            background = Image.open(io.BytesIO(background_bytes)).convert("RGB")
        else:
            background = Image.open(self._background_path).convert("RGB")
        canvas = ImageOps.fit(background, canvas_size, method=Image.Resampling.LANCZOS)
        overlay = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        best = player.best50()
        self._draw_header(
            draw,
            overlay,
            best,
            cosmetics,
            summary=(("查询范围", selector), ("显示谱面", str(len(records)))),
        )
        self._draw_section_label(draw, f"{selector} 分表 · 按达成率排序 · TOP 100", 225)
        for index, record in enumerate(records):
            self._draw_card(draw, overlay, record, covers[index], index + 1, 290, index)
        if not records:
            message = "水鱼中没有符合条件的已游玩成绩"
            width = draw.textlength(message, font=_font(32, bold=True))
            draw.text(((1600 - width) / 2, 340), message, font=_font(32, bold=True), fill="#40375f")
        self._draw_footer(draw, y=footer_y)
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_plate_progress(
        self,
        player: PlayerRecords,
        progress: PlateProgress,
        displayed: list[PlateEntry],
        cosmetics: CosmeticProfile,
        covers: list[bytes | None],
        target_plate: bytes | None,
    ) -> bytes:
        canvas_size = (1600, 1900)
        if cosmetics.background_png:
            background = Image.open(io.BytesIO(cosmetics.background_png)).convert("RGB")
        else:
            background = Image.open(self._background_path).convert("RGB")
        canvas = ImageOps.fit(background, canvas_size, method=Image.Resampling.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.7))
        overlay = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        # Pale veil keeps the report readable while retaining the B50 background.
        draw.rounded_rectangle(
            (35, 28, 1565, 1860), radius=36, fill=(248, 244, 255, 218), outline="#ffffff", width=4
        )
        draw.text(
            (76, 54), f"{progress.plate} · 牌子进度", font=_font(44, bold=True), fill="#34295e"
        )
        percent = progress.percent
        bar_box = (76, 125, 1050, 180)
        draw.rounded_rectangle(
            bar_box, radius=27, fill=(205, 198, 238, 210), outline="white", width=4
        )
        bar_width = round((bar_box[2] - bar_box[0]) * min(100, percent) / 100)
        if bar_width:
            draw.rounded_rectangle(
                (bar_box[0], bar_box[1], bar_box[0] + bar_width, bar_box[3]),
                radius=27,
                fill="#63ded1",
            )
        draw.text(
            (1100, 113),
            f"{percent:.1f}% 已完成",
            font=_font(38, bold=True),
            fill="#5a4da0",
        )

        if displayed:
            for index, (entry, cover) in enumerate(zip(displayed, covers, strict=True)):
                self._draw_plate_entry(draw, overlay, entry, cover, 225 + index * 134)
        else:
            message = "全部条件已经达成！"
            width = draw.textlength(message, font=_font(58, bold=True))
            draw.text(((1600 - width) / 2, 570), message, font=_font(58, bold=True), fill="#7956c8")

        self._draw_plate_details(draw, progress, 1320)
        self._draw_plate_profile(draw, overlay, player, progress, cosmetics, target_plate, 1605)
        credit = "Plate Progress Visual Design by 酸柚子制糖 · Generated by Fujisawa Yuzu"
        font = _font(19, bold=True)
        width = draw.textlength(credit, font=font)
        draw.text(((1600 - width) / 2, 1820), credit, font=font, fill="#635a82")

        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=6)
        return output.getvalue()

    def _draw_plate_entry(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        entry: PlateEntry,
        cover_bytes: bytes | None,
        y: int,
    ) -> None:
        chart, record = entry.chart, entry.record or {}
        accent = DIFFICULTY_COLORS.get(chart.difficulty, "#9852d9")
        emblem = (78, y + 12, 330, y + 117)
        draw.rounded_rectangle(
            emblem, radius=24, fill=(108, 90, 190, 215), outline="white", width=4
        )
        emblem_text = "未达成"
        emblem_font = _font(29, bold=True)
        emblem_width = draw.textlength(emblem_text, font=emblem_font)
        draw.text(
            ((emblem[0] + emblem[2] - emblem_width) / 2, y + 43),
            emblem_text,
            font=emblem_font,
            fill="white",
        )

        box = (365, y, 1520, y + 126)
        draw.rounded_rectangle((371, y + 7, 1526, y + 133), radius=18, fill=(45, 34, 80, 55))
        draw.rounded_rectangle(box, radius=17, fill=(255, 255, 255, 246), outline=accent, width=5)
        cover = _cover_image(cover_bytes, 112, accent, radius=14)
        overlay.alpha_composite(cover, (373, y + 7))

        tx = 500
        draw.rounded_rectangle((tx, y + 7, 1498, y + 37), radius=14, fill=accent)
        draw.text((tx + 13, y + 7), chart.difficulty_name, font=_font(21, bold=True), fill="white")
        title = _fit_text(draw, chart.title, _font(20, bold=True), 660)
        draw.text((tx + 205, y + 9), title, font=_font(20, bold=True), fill="white")
        draw.text((tx + 13, y + 44), "ACHIEVEMENT", font=_font(16, bold=True), fill="#4f4670")
        achievement_color = "#e28b20" if entry.achievement else "#3189d8"
        draw.text(
            (tx + 13, y + 63),
            f"{entry.achievement:.4f}%",
            font=_font(35, bold=True),
            fill=achievement_color,
        )
        draw.text((1380, y + 51), f"Lv.{chart.level}", font=_font(28, bold=True), fill=accent)
        footer = f"定数 {chart.constant:.1f}  ·  谱师 {chart.designer or '未知'}"
        draw.text((tx + 13, y + 104), footer, font=_font(16, bold=True), fill="#342e48")
        if record:
            self._draw_score_badges(overlay, record, 1260, y + 88)

    def _draw_plate_details(
        self,
        draw: ImageDraw.ImageDraw,
        progress: PlateProgress,
        y: int,
    ) -> None:
        draw.rounded_rectangle(
            (75, y, 1525, y + 245), radius=26, fill=(42, 37, 64, 238), outline="#c5b0ef", width=5
        )
        title_box = (500, y - 27, 1100, y + 35)
        draw.rounded_rectangle(title_box, radius=30, fill="#6f5bb8", outline="white", width=4)
        title = "详细信息"
        title_width = draw.textlength(title, font=_font(31, bold=True))
        draw.text(
            ((1600 - title_width) / 2, y - 18), title, font=_font(31, bold=True), fill="white"
        )
        remain = len(progress.unfinished)
        rows = progress.remaining_by_difficulty
        diff_text = (
            "  ·  ".join(
                f"{name} {rows[index]}"
                for index, name in enumerate(("绿", "黄", "红", "紫", "白"))
                if rows[index]
            )
            or "无"
        )
        conditions = {
            "将": "全部指定谱面达到 100.0000%（SSS）或以上",
            "极": "全部指定谱面取得 FC 或以上",
            "神": "全部指定谱面取得 AP 或以上",
            "舞": "全部指定谱面取得 FDX 或以上",
            "舞舞": "全部指定谱面取得 FDX 或以上",
            "者": "全部指定谱面达到 80.0000% 或以上",
        }
        lines = (
            f"目标条件：{conditions.get(progress.goal, progress.goal)}",
            f"总体进度：{len(progress.completed)} / {len(progress.entries)}，还剩 {remain} 张谱面",
            f"剩余难度：{diff_text}",
            "中间严格按谱面定数从高到低展示前 8 张；未游玩按 0.0000% 计算。",
        )
        for index, line in enumerate(lines):
            draw.text(
                (125, y + 53 + index * 42),
                line,
                font=_font(23, bold=index < 3),
                fill="#f5e9b4" if index < 3 else "white",
            )

    def _draw_plate_profile(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        player: PlayerRecords,
        progress: PlateProgress,
        cosmetics: CosmeticProfile,
        target_plate: bytes | None,
        y: int,
    ) -> None:
        draw.rounded_rectangle(
            (75, y, 1525, y + 180), radius=28, fill=(255, 255, 255, 238), outline="#8a68d1", width=5
        )
        avatar = _avatar_image(cosmetics.avatar_png, player.nickname, 142)
        overlay.alpha_composite(avatar, (100, y + 19))
        draw.rounded_rectangle((100, y + 19, 242, y + 161), radius=20, outline="#6651bf", width=5)
        # Keep the whole player identity group inside a compact 620 x 125 area.
        # The previous layout used a 124 px-high nameplate while the nickname
        # box reached 134 px below its top, so its orange border visibly hung
        # outside the official asset.  Rating/name are intentionally narrower
        # than the complete nameplate: its right-hand title artwork must remain
        # unobstructed.  The freed space belongs to the target-plate column.
        profile_left = 260
        profile_top = y + 27
        profile_width = 620
        profile_height = 125
        identity_left = 275
        identity_right = 610
        if cosmetics.nameplate_png:
            try:
                plate = Image.open(io.BytesIO(cosmetics.nameplate_png)).convert("RGBA")
                plate = ImageOps.contain(
                    plate, (profile_width, profile_height), Image.Resampling.LANCZOS
                )
                plate_x = profile_left + (profile_width - plate.width) // 2
                plate_y = profile_top + (profile_height - plate.height) // 2
                overlay.alpha_composite(plate, (plate_x, plate_y))
            except (OSError, ValueError, UnidentifiedImageError):
                pass
        # Keep the original DX Rating asset's ~5.1:1 aspect ratio. Stretching
        # it vertically makes both the built-in label and number slots drift.
        rating = _rating_plate(self._rating_path, player.rating, (335, 66))
        if rating:
            overlay.alpha_composite(rating, (identity_left, y + 31))
        name_box = (identity_left, y + 100, identity_right, y + 144)
        draw.rounded_rectangle(
            name_box,
            radius=10,
            fill=(255, 255, 255, 238),
            outline="#ef6526",
            width=3,
        )
        _draw_vertically_centered_text(
            draw,
            _truncate(player.nickname, 16),
            x=identity_left + 16,
            box=name_box,
            font=_font(25, bold=True),
            fill="#211d35",
        )
        target_left = 920
        target_width = 560
        draw.text((target_left, y + 23), "目标牌子", font=_font(21, bold=True), fill="#6a5a94")
        if target_plate:
            try:
                target = Image.open(io.BytesIO(target_plate)).convert("RGBA")
                target = ImageOps.contain(target, (target_width, 112), Image.Resampling.LANCZOS)
                target_x = target_left + (target_width - target.width) // 2
                target_y = y + 50 + (112 - target.height) // 2
                overlay.alpha_composite(target, (target_x, target_y))
            except (OSError, ValueError, UnidentifiedImageError):
                target_plate = None
        if not target_plate:
            draw.rounded_rectangle(
                (target_left, y + 55, target_left + target_width, y + 157),
                radius=18,
                fill=(239, 232, 255, 245),
                outline="#8a68d1",
                width=3,
            )
            width = draw.textlength(progress.plate, font=_font(48, bold=True))
            draw.text(
                (target_left + target_width / 2 - width / 2, y + 78),
                progress.plate,
                font=_font(48, bold=True),
                fill="#6548b3",
            )

    def _draw_header(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        best50: Best50,
        cosmetics: CosmeticProfile,
        summary: tuple[tuple[str, str], tuple[str, str]] | None = None,
    ) -> None:
        panel = (55, 42, 1545, 210)
        _rounded_shadow(draw, panel, 30)
        draw.rounded_rectangle(
            panel, radius=28, fill=(255, 255, 255, 232), outline="#ffffff", width=3
        )

        avatar_box = (78, 63, 204, 189)
        avatar = _avatar_image(cosmetics.avatar_png, best50.nickname, 126)
        overlay.alpha_composite(avatar, (78, 63))
        draw.rounded_rectangle(avatar_box, radius=20, outline="#5f50c8", width=6)

        has_original_plate = False
        if cosmetics.nameplate_png:
            try:
                plate_image = Image.open(io.BytesIO(cosmetics.nameplate_png)).convert("RGBA")
                plate_image = plate_image.resize((720, 116), Image.Resampling.LANCZOS)
                overlay.alpha_composite(plate_image, (220, 68))
                has_original_plate = True
            except (OSError, ValueError, UnidentifiedImageError):
                logger.warning("自定义姓名框无法解码，使用默认样式")

        rating_plate = _rating_plate(self._rating_path, best50.rating, (350, 72))
        if rating_plate:
            overlay.alpha_composite(rating_plate, (235, 68))

        draw.rounded_rectangle(
            (235, 137, 585, 180),
            radius=10,
            fill=(255, 255, 255, 238),
            outline=(239, 101, 38, 230),
            width=3,
        )
        _draw_vertically_centered_text(
            draw,
            _truncate(best50.nickname, 15),
            x=251,
            box=(235, 137, 585, 180),
            font=_font(30, bold=True),
            fill="#211d35",
        )
        if not has_original_plate:
            draw.text(
                (585, 143),
                _truncate(best50.plate or "舞萌玩家", 12),
                font=_font(22, bold=True),
                fill="#665ba4",
            )

        old_total = sum(int(item.get("ra", 0) or 0) for item in best50.old[:35])
        new_total = sum(int(item.get("ra", 0) or 0) for item in best50.new[:15])
        left_summary, right_summary = summary or (
            ("BEST 35", str(old_total)),
            ("NEW 15", str(new_total)),
        )
        summary = (980, 66, 1505, 184)
        draw.rounded_rectangle(summary, radius=24, fill=(42, 37, 83, 238))
        draw.rounded_rectangle((980, 66, 1242, 75), radius=5, fill="#64ded2")
        draw.rounded_rectangle((1243, 66, 1505, 75), radius=5, fill="#d995ee")
        draw.line((1242, 82, 1242, 170), fill=(255, 255, 255, 70), width=2)
        self._draw_rating_total(draw, *left_summary, 1008, 86, "#8ce9df")
        self._draw_rating_total(draw, *right_summary, 1270, 86, "#e6adf4")

    def _draw_rating_total(
        self,
        draw: ImageDraw.ImageDraw,
        label: str,
        total: str,
        x: int,
        y: int,
        accent: str,
    ) -> None:
        draw.text((x, y), label, font=_font(21, bold=True), fill=accent)
        value_font = _font(43 if len(total) <= 5 else 30, bold=True)
        draw.text((x, y + 32), total, font=value_font, fill="white")

    def _draw_card(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: Image.Image,
        record: dict[str, Any],
        cover_bytes: bytes | None,
        display_index: int,
        start_y: int,
        grid_index: int,
    ) -> None:
        gap_x, gap_y = 14, 14
        card_w, card_h = 288, 116
        col, row = grid_index % 5, grid_index // 5
        x = 55 + col * (card_w + gap_x)
        y = start_y + row * (card_h + gap_y)
        box = (x, y, x + card_w, y + card_h)
        level_index = int(record.get("level_index", 3) or 3)
        accent = DIFFICULTY_COLORS.get(level_index, "#9852d9")
        draw.rounded_rectangle(
            (x + 4, y + 7, x + card_w + 4, y + card_h + 7), radius=13, fill=(51, 38, 89, 70)
        )
        draw.rounded_rectangle(box, radius=12, fill=(255, 255, 255, 244), outline=accent, width=4)

        cover = _cover_image(cover_bytes, 102, accent, radius=15)
        overlay.alpha_composite(cover, (x + 7, y + 7))

        tx = x + 117
        index_text = f"#{display_index}"
        index_font = _font(17, bold=True)
        draw.text((tx, y + 6), index_text, font=index_font, fill="#655d7d")
        type_name = str(record.get("type") or "DX").casefold()
        type_asset = "standard" if type_name in {"sd", "standard"} else "dx"
        type_badge = _badge_image(self._type_path / f"{type_asset}.webp", (60, 20))
        badge_x = round(tx + draw.textlength(index_text, font=index_font) + 7)
        if type_badge:
            overlay.alpha_composite(type_badge, (badge_x, y + 4))
        else:
            draw.text(
                (badge_x, y + 6),
                "SD" if type_asset == "standard" else "DX",
                font=index_font,
                fill="#655d7d",
            )
        title_font = _font(20, bold=True)
        title = _fit_text(draw, str(record.get("title", "未知曲目")), title_font, card_w - 128)
        draw.text((tx, y + 29), title, font=title_font, fill="#29233e")
        draw.text(
            (tx, y + 54),
            f"{float(record.get('achievements', 0)):.4f}%",
            font=_font(28, bold=True),
            fill=accent,
        )
        ds_value = record.get("ds")
        try:
            ds_text = f"{float(ds_value):.1f}"
        except (TypeError, ValueError):
            ds_text = "?"
        footer = f"{ds_text}→{int(record.get('ra', 0))}"
        draw.text((tx, y + 90), footer, font=_font(14, bold=True), fill="#5d5570")
        self._draw_score_badges(overlay, record, x + 187, y + 87)

    def _draw_score_badges(
        self,
        overlay: Image.Image,
        record: dict[str, Any],
        x: int,
        y: int,
    ) -> None:
        rate = str(record.get("rate") or "").lower()
        fc = str(record.get("fc") or "").lower()
        fs = str(record.get("fs") or "").lower()
        rank = _badge_image(self._badge_path / f"rank-{rate}.png", (44, 21))
        combo = _badge_image(
            self._badge_path / f"bonus-{fc if fc in {'fc', 'fcp', 'ap', 'app'} else 'blank'}.png",
            (22, 22),
        )
        sync = _badge_image(
            self._badge_path
            / f"bonus-{fs if fs in {'sync', 'fs', 'fsp', 'fsd', 'fsdp'} else 'blank'}.png",
            (22, 22),
        )
        if rank:
            overlay.alpha_composite(rank, (x, y))
        if combo:
            overlay.alpha_composite(combo, (x + 47, y))
        if sync:
            overlay.alpha_composite(sync, (x + 72, y))

    def _draw_section_label(self, draw: ImageDraw.ImageDraw, text: str, y: int) -> None:
        draw.rounded_rectangle((590, y, 1010, y + 48), radius=24, fill=(42, 37, 83, 225))
        width = draw.textlength(text, font=_font(24, bold=True))
        draw.text(((1600 - width) / 2, y + 8), text, font=_font(24, bold=True), fill="white")

    def _draw_footer(self, draw: ImageDraw.ImageDraw, *, y: int = 1780) -> None:
        draw.rounded_rectangle((55, y, 1545, y + 70), radius=20, fill=(42, 37, 83, 220))
        credit = "B50 Visual Design by 酸柚子制糖 · Generated by Fujisawa Yuzu"
        font = _font(22, bold=True)
        width = draw.textlength(credit, font=font)
        draw.text(((1600 - width) / 2, y + 20), credit, font=font, fill="white")


def _same_chart(left: Chart, right: Chart) -> bool:
    return (
        left.song_id == right.song_id
        and left.chart_type == right.chart_type
        and left.difficulty == right.difficulty
    )


def _cover_id_candidates(song_id: int) -> tuple[int, ...]:
    """Return public-catalogue IDs for a score record without losing its cache identity."""
    if 10000 < song_id < 20000:
        return song_id, song_id - 10000
    return (song_id,)


def _normalize_image_png(content: bytes) -> bytes | None:
    """Validate image responses and normalize WebP/JPEG CDN responses to PNG."""
    if not content or len(content) > 10_000_000:
        return None
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
        if image.format == "PNG":
            return content
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="PNG", compress_level=6)
        return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError):
        return None


@lru_cache(maxsize=32)
def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = BOLD_FONT_CANDIDATES if bold else FONT_CANDIDATES
    path = next((item for item in candidates if Path(item).exists()), None)
    if path is None:
        raise B50RenderError("系统中找不到可用字体，请安装 noto-fonts-cjk")
    return ImageFont.truetype(path, size)


@lru_cache(maxsize=64)
def _badge_image(path: Path, size: tuple[int, int]) -> Image.Image | None:
    try:
        image = Image.open(path).convert("RGBA")
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return image
    except (OSError, ValueError, UnidentifiedImageError):
        logger.warning("成绩徽章素材无法读取：%s", path)
        return None


@lru_cache(maxsize=128)
def _rating_plate(path: Path, rating: int, size: tuple[int, int]) -> Image.Image | None:
    tier = _rating_tier(rating)
    try:
        plate = Image.open(path / f"UI_CMN_DXRating_{tier:02d}.png").convert("RGBA")
        plate = plate.resize(size, Image.Resampling.LANCZOS)
        box_left = round(size[0] * 307 / 664)
        box_right = round(size[0] * 580 / 664)
        box_top = round(size[1] * 23 / 130)
        box_bottom = round(size[1] * 108 / 130)
        slot_width = (box_right - box_left) / 5
        target_size = (round(slot_width * 0.8), round((box_bottom - box_top) * 0.74))
        digits = str(max(0, rating))[-5:]
        first_slot = 5 - len(digits)
        for index, digit in enumerate(digits, start=first_slot):
            number = Image.open(path / f"UI_NUM_Drating_{digit}.png").convert("RGBA")
            alpha_box = number.getchannel("A").getbbox()
            if alpha_box:
                number = number.crop(alpha_box)
            number.thumbnail(target_size, Image.Resampling.LANCZOS)
            slot_x = box_left + index * slot_width
            digit_x = round(slot_x + (slot_width - number.width) / 2)
            digit_y = round(box_top + (box_bottom - box_top - number.height) / 2)
            plate.alpha_composite(number, (digit_x, digit_y))
        return plate
    except (OSError, ValueError, UnidentifiedImageError):
        logger.warning("DX Rating 日服素材无法读取")
        return None


def _rating_tier(rating: int) -> int:
    if rating < 1000:
        return 1
    if rating < 2000:
        return 2
    if rating < 4000:
        return 3
    if rating < 7000:
        return 4
    if rating < 10000:
        return 5
    if rating < 12000:
        return 6
    if rating < 13000:
        return 7
    if rating < 14000:
        return 8
    if rating < 14500:
        return 9
    if rating < 15000:
        return 10
    return 11


def _rounded_shadow(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle((x1 + 8, y1 + 10, x2 + 8, y2 + 10), radius=radius, fill=(45, 35, 85, 72))


def _draw_vertically_centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    x: int,
    box: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont,
    fill: str,
) -> None:
    _, top, _, bottom = draw.textbbox((0, 0), text, font=font)
    y = box[1] + ((box[3] - box[1]) - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=fill)


def _avatar_image(data: bytes | None, name: str, size: int) -> Image.Image:
    if data:
        try:
            image = Image.open(io.BytesIO(data)).convert("RGBA")
            image = ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)
        except (OSError, ValueError, UnidentifiedImageError):
            image = _monogram(name, size)
    else:
        image = _monogram(name, size)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=18,
        fill=255,
    )
    image.putalpha(mask)
    return image


def _monogram(name: str, size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), "#8071e8")
    draw = ImageDraw.Draw(image)
    for inset, color in ((8, "#6edfd2"), (24, "#9d83ee"), (42, "#f0a8d0")):
        draw.ellipse((inset, inset, size - inset, size - inset), fill=color)
    letter = (name.strip() or "M")[0].upper()
    font = _font(48, bold=True)
    bounds = draw.textbbox((0, 0), letter, font=font)
    draw.text(((size - (bounds[2] - bounds[0])) / 2, 33), letter, font=font, fill="white")
    return image


def _cover_image(data: bytes | None, size: int, accent: str, *, radius: int) -> Image.Image:
    if data:
        try:
            image = ImageOps.fit(
                Image.open(io.BytesIO(data)).convert("RGBA"),
                (size, size),
                method=Image.Resampling.LANCZOS,
            )
        except (OSError, ValueError, UnidentifiedImageError):
            logger.debug("封面图片无法解码，使用占位图")
            image = _placeholder_cover(size, accent)
    else:
        image = _placeholder_cover(size, accent)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    image.putalpha(mask)
    return image


def _placeholder_cover(size: int, accent: str) -> Image.Image:
    image = Image.new("RGBA", (size, size), accent)
    draw = ImageDraw.Draw(image)
    draw.ellipse((16, 16, size - 16, size - 16), outline="white", width=5)
    draw.ellipse((42, 42, size - 42, size - 42), fill="white")
    return image.filter(ImageFilter.GaussianBlur(0.2))


def _plate_record(entry: PlateEntry) -> dict[str, Any]:
    chart = entry.chart
    record = dict(entry.record or {})
    record.setdefault("song_id", chart.song_id)
    record.setdefault("title", chart.title)
    record.setdefault("type", "DX" if chart.chart_type == "dx" else "SD")
    record.setdefault("level_index", chart.difficulty)
    record.setdefault("level", chart.level)
    record.setdefault("ds", chart.constant)
    record.setdefault("achievements", 0)
    return record


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    shortened = text
    while shortened and draw.textlength(shortened + "…", font=font) > max_width:
        shortened = shortened[:-1]
    return shortened + "…"
