"""Original school stationery for the bot, inspired by ONGEKI's ASTERISM and Yuzu's candy."""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw

from ..help_catalog import command_usage, group_commands, group_topics, topic_names
from .b50_image import _font
from .result_card import _wrap

PAPER = "#FCF9EF"
INK = "#213C39"
GREEN = "#216457"
MINT = "#BEE3C9"
YELLOW = "#FFD773"
RULE = "#D6DFD2"
MUTED = "#52655C"
WIDTH = 880


def _star(draw, x, y, radius, color):
    points = []
    for i in range(10):
        angle = math.pi * (i / 5 - 0.5)
        r = radius if i % 2 == 0 else radius * 0.43
        points.append((x + math.cos(angle) * r, y + math.sin(angle) * r))
    draw.polygon(points, fill=color)


@lru_cache(maxsize=4)
def _candy(size: int) -> Image.Image:
    # Hand-drawn wrapper with a citrus-slice centre, not a copied character or game logo.
    panel = Image.new("RGBA", (560, 360))
    d = ImageDraw.Draw(panel)
    for left in (True, False):

        def pt(x, y, left=left):
            return (x if left else 560 - x, y)

        wrapper = [
            pt(x, y)
            for x, y in (
                (216, 153),
                (105, 94),
                (113, 124),
                (88, 136),
                (102, 153),
                (83, 184),
                (103, 192),
                (94, 221),
                (115, 229),
                (111, 247),
                (218, 208),
            )
        ]
        d.polygon(wrapper, fill=MINT)
        d.line(wrapper + [wrapper[0]], fill=INK, width=6, joint="curve")
        for tip in [(135, 143), (128, 199)]:
            d.line([pt(*tip), pt(193, 176)], fill=GREEN, width=4)
    d.ellipse((198, 100, 362, 264), fill=YELLOW, outline=INK, width=7)
    d.ellipse((215, 117, 345, 247), outline="#FFF6CC", width=7)
    for i in range(8):
        a = math.tau * i / 8
        d.line(
            [
                (280 + 15 * math.cos(a), 182 + 15 * math.sin(a)),
                (280 + 55 * math.cos(a), 182 + 55 * math.sin(a)),
            ],
            fill="#FFFAE8",
            width=6,
        )
    d.ellipse((269, 171, 291, 193), fill="#FFF8D8")
    panel = panel.rotate(17, Image.Resampling.BICUBIC)
    return panel.resize((size, round(size * 360 / 560)), Image.Resampling.LANCZOS)


def draw_header(im: Image.Image, title: str, section: str, scale: float = 1) -> int:
    """A ruled school masthead with a separate protected space for the emblem."""
    d = ImageDraw.Draw(im)
    w = im.width

    def k(v):
        return round(v * scale)

    titles = _wrap(title, k(48), w - k(282), bold=True)
    h = k(124 + 65 * len(titles))
    d.rectangle((0, 0, w, h), fill=PAPER)
    d.rectangle((0, 0, w, k(7)), fill=INK)
    start = w - k(218)
    d.polygon(
        [(start + k(53), k(8)), (w, k(8)), (w, h - k(15)), (start - k(4), h - k(15))],
        fill="#E7F0DF",
    )
    for y in range(k(20), h - k(25), k(12)):
        for x in range(start + k(55), w - k(12), k(12)):
            d.ellipse((x, y, x + k(1.5), y + k(1.5)), fill="#B4D1B4")
    cx, cy = w - k(100), h // 2
    candy = _candy(k(230))
    im.paste(candy, (cx - candy.width // 2, cy - candy.height // 2), candy)
    for x, y, r, col in ((-88, -33, 10, YELLOW), (53, 42, 8, GREEN), (70, -55, 6, "#EB8F88")):
        _star(d, cx + k(x), cy + k(y), k(r), col)
    left = k(39)
    d.rectangle((left, k(35), left + k(7), k(58)), fill=GREEN)
    d.text(
        (left + k(17), k(34)),
        "奏坂高校  /  " + section,
        font=_font(k(23), bold=True),
        fill=GREEN,
        anchor="lt",
    )
    for i, line in enumerate(titles):
        d.text((left, k(79 + i * 65)), line, font=_font(k(48), bold=True), fill=INK, anchor="lt")
    d.rectangle((left, h - k(25), w - left, h - k(23)), fill=INK)
    for off, size, color in [(0, 41, YELLOW), (44, 20, MINT), (67, 11, "#F0A9A1")]:
        d.rectangle((left + k(off), h - k(29), left + k(off + size), h - k(23)), fill=color)
    return h


def _foot(im, y, margin, font_size):
    d = ImageDraw.Draw(im)
    d.line((margin, y, im.width - margin, y), fill=RULE, width=2)
    d.text((margin, y + 10), "藤泽柚子", font=_font(font_size, bold=True), fill=GREEN, anchor="lt")
    d.text(
        (im.width - margin, y + 12),
        "A S T E R I S M",
        font=_font(font_size - 2, bold=True),
        fill=MUTED,
        anchor="rt",
    )


def _png(im):
    output = io.BytesIO()
    im.save(output, format="PNG", compress_level=5)
    return output.getvalue()


def draw_card(title, rows, section, metric, note) -> bytes:
    # Side-by-side fields for short labels. Long commands/sentences get their own line.
    header_h = 124 + 65 * len(_wrap(title, 48, WIDTH - 282, bold=True))
    measured = []
    for label, value in rows:
        stacked = bool(label and _font(23, bold=True).getlength(label) > 171)
        labels = _wrap(label, 23, 750 if stacked else 171, bold=True) if label else []
        values = _wrap(value, 28, 750 if not labels or stacked else 544)
        value_h = 42 * len(values)
        height = 34 * len(labels) + value_h if stacked else max(34 * len(labels), value_h)
        measured.append((labels, values, stacked, height + 34))
    labels = _wrap(metric[0], 23, 728, True) if metric else []
    metric_lines = _wrap(metric[1], 53, 728, True) if metric else []
    metric_h = 24 + len(labels) * 35 + len(metric_lines) * 68 + 13 if metric else 0
    notes = _wrap(note, 23, 745) if note else []
    height = (
        header_h
        + metric_h
        + sum(h for _, _, _, h in measured)
        + (14 if measured else 0)
        + (20 + len(notes) * 34 if notes else 0)
        + 73
    )
    if height > 5200:
        raise ValueError("academy card exceeds image height")
    im = Image.new("RGB", (WIDTH, height), PAPER)
    d = ImageDraw.Draw(im)
    y = draw_header(im, title, section)
    if metric:
        d.rounded_rectangle((38, y, 842, y + metric_h - 9), radius=18, fill=INK)
        d.rounded_rectangle((55, y + 18, 61, y + metric_h - 27), radius=3, fill=YELLOW)
        text_y = y + 18
        for line in labels:
            d.text((79, text_y), line, font=_font(23, bold=True), fill=MINT, anchor="lt")
            text_y += 35
        for line in metric_lines:
            d.text((77, text_y), line, font=_font(53, bold=True), fill="#FFE19A", anchor="lt")
            text_y += 68
        y += metric_h
    if measured:
        top = y + 3
        bottom = top + sum(h for _, _, _, h in measured)
        d.rounded_rectangle(
            (38, top, 842, bottom), radius=16, fill="#FFFEF9", outline=RULE, width=2
        )
        y = top
        for i, (field_labels, values, stacked, h) in enumerate(measured):
            if i:
                d.line((60, y, 820, y), fill=RULE, width=2)
            for j, line in enumerate(field_labels):
                d.text(
                    (62, y + 19 + 34 * j), line, font=_font(23, bold=True), fill=GREEN, anchor="lt"
                )
            tx = 259 if field_labels and not stacked else 62
            ty = y + 16 + (34 * len(field_labels) if stacked else 0)
            for j, line in enumerate(values):
                d.text((tx, ty + j * 42), line, font=_font(28), fill=INK, anchor="lt")
            y += h
        y += 14
    if notes:
        y += 12
        d.rectangle((42, y + 2, 46, y + max(16, len(notes) * 34 - 7)), fill="#DD9A55")
        for j, line in enumerate(notes):
            d.text((64, y + j * 34), line, font=_font(23), fill=MUTED, anchor="lt")
    _foot(im, height - 48, 39, 19)
    return _png(im)


def draw_menu(commands: Sequence[Any], *, title="柚子菜单", legacy=False, by_topic=False) -> bytes:
    sections = group_topics(commands) if by_topic else group_commands(commands, legacy=legacy)
    blocks = []
    for category, items in sections.items():
        pairs = []
        for offset in range(0, len(items), 2):
            pair = []
            for c in items[offset : offset + 2]:
                usage = _wrap(command_usage(c), 30, 690, True)
                desc = _wrap(c.description, 24, 690)
                pair.append((usage, desc))
            height = max(42 * len(u) + 34 * len(t) for u, t in pair) + 44
            pairs.append((pair, height))
        topics = "、".join(topic_names(category, items)) if not by_topic and not legacy else ""
        blocks.append((category, pairs, topics))
    header_h = round(1.35 * (124 + 65 * len(_wrap(title, 65, 1219, bold=True))))
    height = (
        header_h
        + 44
        + sum(94 + (43 if topics else 0) + sum(h for _, h in pairs) for _, pairs, topics in blocks)
        + 100
    )
    im = Image.new("RGB", (1600, height), PAPER)
    y = draw_header(im, title, "指令手册", 1.35)
    d = ImageDraw.Draw(im)
    d.text(
        (54, y + 1),
        "/help 舞萌 成绩 查看分类　·　/help pc50 查看完整用法　·　/help 文字 打开文字目录",
        font=_font(27),
        fill=MUTED,
        anchor="lt",
    )
    y += 60
    for category, pairs, topics in blocks:
        d.rounded_rectangle((54, y, 1546, y + 58), radius=10, fill=INK)
        d.rectangle((54, y + 11, 62, y + 47), fill=YELLOW)
        d.text((78, y + 12), category, font=_font(32, bold=True), fill=PAPER, anchor="lt")
        d.text(
            (1520, y + 18),
            f"{sum(len(pair) for pair, _ in pairs)} 项",
            font=_font(22),
            fill=MINT,
            anchor="rt",
        )
        y += 73
        if topics:
            d.text((78, y), f"子分类　{topics}", font=_font(26), fill=GREEN, anchor="lt")
            y += 43
        for pair, h in pairs:
            for col, (usage, desc) in enumerate(pair):
                x = 54 + col * 752
                d.rounded_rectangle(
                    (x, y, x + 738, y + h - 10), radius=12, fill="#FFFEF9", outline=RULE, width=2
                )
                ty = y + 15
                for line in usage:
                    d.text((x + 21, ty), line, font=_font(30, bold=True), fill=GREEN, anchor="lt")
                    ty += 42
                for line in desc:
                    d.text((x + 21, ty), line, font=_font(24), fill=INK, anchor="lt")
                    ty += 34
            y += h
        y += 21
    _foot(im, height - 63, 54, 24)
    return _png(im)
