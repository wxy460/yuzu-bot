"""Single chart leaderboard for play counts, using the player's B50 artwork."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageOps

from .b50_image import DIFFICULTY_COLORS, _badge_image, _cover_image, _fit_text, _font


def draw_pc50(service, player, ranked, cosmetics, covers) -> bytes:
    grid_top = 290 if player else 160
    rows = max(1, (len(ranked) + 3) // 4)
    footer_y = grid_top + rows * 150 + 12
    size = (1600, footer_y + 105)
    source = (
        io.BytesIO(cosmetics.background_png)
        if cosmetics.background_png
        else service._background_path
    )
    with Image.open(source) as image:
        canvas = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", size)
    draw = ImageDraw.Draw(layer)
    if player:
        service._draw_header(
            draw,
            layer,
            player,
            cosmetics,
            summary=(
                ("上榜谱面", f"{len(ranked)} 张"),
                ("榜内游玩", f"{sum(c for c, _ in ranked):,} 次"),
            ),
        )
        service._draw_section_label(draw, "PC50 · 游玩次数排行", grid_top - 63)
    else:
        draw.rounded_rectangle((55, 36, 1545, 135), radius=23, fill=(42, 37, 83, 238))
        draw.text((84, 84), "PC50", font=_font(42, bold=True), fill="#87E1D5", anchor="lm")
        draw.text((217, 84), "游玩次数排行", font=_font(37, bold=True), fill="white", anchor="lm")
        draw.text(
            (1505, 84),
            f"{len(ranked)} 张谱面",
            font=_font(27, bold=True),
            fill="white",
            anchor="rm",
        )
    for index, ((count, record), cover) in enumerate(zip(ranked, covers)):
        x = 55 + (index % 4) * 376
        y = grid_top + (index // 4) * 150
        _chart_card(service, layer, draw, x, y, index + 1, count, record, cover)
    if not ranked:
        draw.text(
            (800, grid_top + 60),
            "暂无带游玩次数的谱面",
            font=_font(33, bold=True),
            anchor="mm",
            fill="#40375F",
            stroke_width=3,
            stroke_fill="white",
        )
    service._draw_footer(draw, y=footer_y)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")
    out = io.BytesIO()
    canvas.save(out, format="PNG", compress_level=6)
    return out.getvalue()


def _chart_card(service, layer, draw, x, y, rank, count, record, cover):
    w, h = 362, 135
    typ = str(record.get("type") or "").lower()
    utage = typ in {"utage", "宴", "宴会场", "宴会場"}
    try:
        difficulty = int(record.get("level_index"))
    except (TypeError, ValueError):
        difficulty = None
    accent = "#DB906C" if utage else DIFFICULTY_COLORS.get(difficulty, "#8A82A4")
    draw.rounded_rectangle((x + 4, y + 6, x + w + 4, y + h + 6), radius=16, fill=(51, 38, 89, 72))
    draw.rounded_rectangle(
        (x, y, x + w, y + h), radius=16, fill=(255, 255, 255, 247), outline=accent, width=4
    )
    jacket = _cover_image(cover, 99, accent, radius=11)
    layer.alpha_composite(jacket, (x + 9, y + 9))
    # Place the position on a solid label so it stays legible even on a busy jacket.
    rank_color = ("#93601B", "#596B85", "#95563F")[rank - 1] if rank <= 3 else "#433A6A"
    draw.rounded_rectangle(
        (x + 8, y + 8, x + 56, y + 34), radius=7, fill=rank_color, outline="white", width=1
    )
    draw.text((x + 32, y + 21), f"#{rank}", font=_font(17, bold=True), fill="white", anchor="mm")
    type_asset = (
        "standard"
        if typ in {"sd", "standard", "标准"}
        else "dx"
        if typ in {"dx", "deluxe"}
        else None
    )
    badge = (
        _badge_image(service._type_path / f"{type_asset}.webp", (51, 17)) if type_asset else None
    )
    if badge:
        layer.alpha_composite(badge, (x + 9, y + 113))
    elif typ:
        draw.text(
            (x + 10, y + 114),
            _fit_text(draw, "宴" if utage else typ.upper(), _font(13, bold=True), 50),
            font=_font(13, bold=True),
            fill="#514969",
            anchor="lt",
        )
    difficulty_name = {0: "绿", 1: "黄", 2: "红", 3: "紫", 4: "白"}.get(difficulty, "")
    level = str(record.get("level") or "").strip()
    if utage or not difficulty_name:
        lv = f"Lv.{level}" if level else ""
    else:
        lv = f"{difficulty_name} {level}" if level else f"{difficulty_name}谱"
    draw.text((x + 108, y + 114), lv, font=_font(14, bold=True), fill="#554C6E", anchor="rt")

    tx = x + 122
    title_font = _font(23, bold=True)
    fallback = f"曲目 #{record['song_id']}" if record.get("song_id") else "未知曲目"
    title = _fit_text(draw, str(record.get("title") or fallback), title_font, w - 135)
    draw.text((tx, y + 13), title, font=title_font, fill="#29233E", anchor="lt")
    draw.rounded_rectangle((tx, y + 54, tx + 43, y + 81), radius=8, fill="#514777")
    draw.text((tx + 21, y + 67), "PC", font=_font(17, bold=True), fill="white", anchor="mm")
    value = f"{count:,}"
    count_size = 36
    while count_size > 12 and _font(count_size, bold=True).getlength(value) > w - 190:
        count_size -= 2
    draw.text(
        (tx + 52, y + 67), value, font=_font(count_size, bold=True), fill="#332655", anchor="lm"
    )
    draw.line((tx, y + 94, x + w - 12, y + 94), fill="#E0DAEB", width=2)
    detail = f"ID {record['song_id']}" if record.get("song_id") else ""
    achievement = record.get("achievements")
    if achievement is not None:
        try:
            detail = f"{float(achievement):.4f}%"
        except (TypeError, ValueError):
            pass
    draw.text((tx, y + 104), detail, font=_font(19, bold=True), fill="#584575", anchor="lt")
    if any(record.get(field) for field in ("rate", "fc", "fs")):
        service._draw_score_badges(layer, record, x + w - 111, y + 101)
