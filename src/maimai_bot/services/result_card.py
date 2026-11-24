"""Mobile-readable result cards for commands without a dedicated game sheet."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
from collections.abc import Sequence

from PIL import Image, ImageDraw

from ..domain import BotReply, QuickAction
from .b50_image import B50RenderError, _font

logger = logging.getLogger(__name__)
WIDTH = 880
ACCENTS = {
    "曲目": "#5046AB",
    "工具": "#116F6D",
    "牌子": "#70459B",
    "成绩": "#4945AB",
    "娱乐": "#9D4265",
    "外观": "#865380",
    "帮助": "#465B9C",
}


def _wrap(text: str, size: int, width: int, bold: bool = False) -> list[str]:
    font = _font(size, bold=bold)
    lines = []
    # Keep Latin words, percentages and command names together when possible.
    for paragraph in text.splitlines() or [""]:
        current = ""
        for token in re.findall(r"[\x21-\x7e]+|\s+|.", paragraph):
            if current and font.getlength(current + token) > width:
                lines.append(current.rstrip())
                current = ""
            if font.getlength(token) > width:
                for char in token:
                    if current and font.getlength(current + char) > width:
                        lines.append(current)
                        current = ""
                    current += char
            elif current or not token.isspace():
                current += token
        lines.append(current.rstrip())
    return lines


def _draw(title, rows, section, metric, note) -> bytes:
    accent = ACCENTS.get(section, "#5A4695")
    ink = "#211E39"
    titles = _wrap(title, 43, 610, bold=True)
    header_h = 106 + len(titles) * 56
    measured = []
    for label, value in rows:
        stacked = bool(label and _font(24, bold=True).getlength(label) > 168)
        labels = _wrap(label, 24, 759 if stacked else 168, bold=True) if label else []
        values = _wrap(value, 28, 575 if label and not stacked else 759)
        label_h = len(labels) * 36
        value_h = len(values) * 43
        row_h = label_h + value_h if stacked else max(label_h, value_h)
        measured.append((labels, values, stacked, row_h + 36))
    metric_lines = _wrap(metric[1], 56, 756, bold=True) if metric else []
    metric_labels = _wrap(metric[0], 24, 756, bold=True) if metric else []
    metric_h = len(metric_labels) * 33 + len(metric_lines) * 70 + 42 if metric else 0
    notes = _wrap(note, 23, 752) if note else []
    height = header_h + metric_h + sum(h for _, _, _, h in measured) + len(notes) * 36 + 58
    if height > 5200:
        raise ValueError("command card exceeds image height")
    canvas = Image.new("RGB", (WIDTH, height), "#EFECF9")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, header_h - 1), fill="#272442")
    # A cabinet ring with eight touch points, inset behind the title.
    cx, cy = WIDTH - 100, header_h // 2
    for radius in (46, 65, 77):
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius), outline="#575375", width=3
        )
    for i in range(8):
        angle = math.tau * i / 8
        x, y = cx + 65 * math.cos(angle), cy + 65 * math.sin(angle)
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=("#79E5C5", "#DCB7EE")[i % 2])
    draw.text(
        (39, 28), f"藤泽柚子  /  {section}", font=_font(23, bold=True), fill="#9EE8D8", anchor="lt"
    )
    y = 74
    for line in titles:
        draw.text((38, y), line, font=_font(43, bold=True), fill="white", anchor="lt")
        y += 56
    y = header_h + 18
    if metric:
        draw.rounded_rectangle((21, y, WIDTH - 21, y + metric_h - 8), radius=18, fill=accent)
        ty = y + 18
        for line in metric_labels:
            draw.text((44, ty), line, font=_font(24, bold=True), fill="white", anchor="lt")
            ty += 33
        for line in metric_lines:
            draw.text((42, ty), line, font=_font(56, bold=True), fill="white", anchor="lt")
            ty += 70
        y += metric_h
    for labels, values, stacked, row_h in measured:
        draw.rounded_rectangle((21, y, WIDTH - 21, y + row_h - 7), radius=13, fill="white")
        for i, line in enumerate(labels):
            draw.text(
                (42, y + 16 + i * 36), line, font=_font(24, bold=True), fill=accent, anchor="lt"
            )
        for i, line in enumerate(values):
            tx = 224 if labels and not stacked else 42
            ty = y + 15 + (len(labels) * 36 if stacked else 0)
            draw.text((tx, ty + i * 43), line, font=_font(28), fill=ink, anchor="lt")
        y += row_h
    y += 8
    for line in notes:
        draw.text((43, y), line, font=_font(23), fill="#514D66", anchor="lt")
        y += 36
    draw.rectangle((0, height - 8, WIDTH // 2, height - 1), fill="#7DDCC1")
    draw.rectangle((WIDTH // 2, height - 8, WIDTH, height - 1), fill="#C9A8EC")
    out = io.BytesIO()
    canvas.save(out, format="PNG", compress_level=4)
    return out.getvalue()


async def result_card(
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    section: str = "舞萌",
    metric: tuple[str, str] | None = None,
    note: str = "",
    followup: str = "",
    actions: tuple[QuickAction, ...] = (),
) -> BotReply:
    return await _reply(_draw, title, rows, section, metric, note, followup, actions)


async def academy_card(
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    section: str = "校园互动",
    metric: tuple[str, str] | None = None,
    note: str = "",
    followup: str = "",
    actions: tuple[QuickAction, ...] = (),
) -> BotReply:
    """A school-style result with the same accessible text, links and QQ buttons."""
    from .academy import draw_card
    return await _reply(draw_card, title, rows, section, metric, note, followup, actions)


async def _reply(renderer, title, rows, section, metric, note, followup, actions):
    fields = ([metric] if metric else []) + list(rows)
    text = "\n".join(
        filter(
            None,
            (
                title,
                *(f"{label}：{value}" if label else value for label, value in fields),
                note,
            ),
        )
    )
    full_text = text + ("\n" + followup if followup else "")
    if len(text) <= 5000:
        try:
            image = await asyncio.to_thread(renderer, title, rows, section, metric, note)
            return BotReply(full_text, image, followup or (title if actions else ""), actions)
        except (OSError, ValueError, B50RenderError) as exc:
            logger.warning("无法绘制命令结果图，将返回文字 (%s)", type(exc).__name__)
    return BotReply(text=full_text, quick_actions=actions)
