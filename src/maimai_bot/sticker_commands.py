"""Caption public maimai artwork with a compact, transparent rhythm-game wordmark."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import httpx
from PIL import Image, ImageColor, ImageDraw, ImageOps

from .domain import BotReply, CommandRequest, QuickAction
from .router import CommandRouter
from .services.b50_image import B50RenderError, _font
from .services.cosmetics import CosmeticError, CosmeticService

# Tencent's public 242177_android.json manifest. Stable IDs keep old chat
# commands reproducible even if the pack is reordered. No artwork is bundled.
SALT_ITEMS = (
    ("心心", "cf61c140c873b7165a684dde91b7f1fd"),
    ("哭哭", "1340a42204c1ece1b28e7258bae19d43"),
    ("怒怒", "f27e95e64278985454a6256acb2655aa"),
    ("晚安", "383d2e522cef768f1b12de4bc36eb7f1"),
    ("问号", "4d88f8a4e392c1fe272740863106d447"),
    ("生气", "7005bf9d5e689ff19697e58f8ec006b7"),
    ("震惊", "a322b9f5bf8e2f0f2370d933f6fd4239"),
    ("困困", "8553c571967bbc2578f10e64b62c6cf6"),
    ("好好", "2c277ad30b3c52843297a228c9920aff"),
    ("憋憋", "75593bbd429cff293c86cbbf36f9919c"),
    ("兴奋", "2276929e5e7bd7897b8e4b025e4aeaa8"),
    ("哼哼", "d31f034c5bff2359d1adf9497918da84"),
    ("求求", "25d5935ad7592f0228f433d63c927cfb"),
    ("害怕", "aab9b89db49655966185f5a3f9add6a3"),
    ("吃面包", "69b08cc9034b3d025b6230540411bc8e"),
    ("嫌弃", "2843e0d66029d3cc7875f7746d7e5862"),
)


# Tencent pack 243044 · 蓝莓布丁
BLUEBERRY_ITEMS = (
    ("震惊", "0f9835b66b88a92094dfc27fc493ea34"),
    ("害羞", "f55a231ea7aba99308c9c15dd4adf728"),
    ("薯片", "7470cbd203f6218b0a71be124daacc1e"),
    ("给心心", "d244ae297bd39d44dec6359d4a7b349d"),
    ("未响应", "ad5a2bf3c49c6eb0c4ae1ae5a21d7647"),
    ("典型的", "3e83ce33927b3039a463e12de14dd409"),
    ("哭哭", "dbd495869c0e85fc1e7108a478caedde"),
    ("穷", "823590cdbe25ae525f097a88cf5c145f"),
    ("花花", "745a082bcfd22aa06e5930178088facf"),
    ("晚安", "56a7fbfbb95dd773b9e132bf1c420c8b"),
    ("困困", "3705b3b72703b2c64354c1969f9fdaa4"),
    ("大哭", "b737542056360519aa147c33fe841f0f"),
    ("怒怒", "70099a8ca09bb3fea7090eba71455526"),
    ("嘲笑", "0c2af3f73c55204122d6dc3616f7036b"),
    ("投降", "9f142d16069e67cb17fe64c2bf4ebc5a"),
    ("打分", "c1a115f1393b02c8076a6acd827672f6"),
)

# Tencent pack 242679 · 香芋甜筒
TARO_ITEMS = (
    ("花花", "5d6bd8bf05f1097ca50eaf3afe2be5cc"),
    ("投降了", "c5c56a7734574e91a6dd040ce3c4f9ad"),
    ("猫猫", "8a2231dfb1b5e3df41723f3212cb33a6"),
    ("吃面包", "bcbfd757e165b806b070bc64b9248853"),
    ("修正", "d400618fc8922b2a6c69ecc2032c7a54"),
    ("怒怒", "75d1a901a60e17b08b59db723555aa46"),
    ("哭哭", "84718c65837703841b40182112c8e313"),
    ("无语", "daffc7ff989db4ede83909b2fa1ade89"),
    ("穷了", "51564c44a5e88287828a798a6d6ef484"),
    ("气鼓鼓", "b2c5eb17b4bca940d927e80b1f6e9107"),
    ("未响应", "8c177874e742f5246b32d32cffa91f7d"),
    ("震惊", "8ab359cd9d3b05e9f737f70e9466c370"),
    ("困困", "9d1314e01dde083877f4d9700ffe82d5"),
    ("打分", "4a5838a12e9da054df4ec09b7893804b"),
    ("晚安", "24c44c8557896e0df9a2ccead0f2993e"),
    ("给心心", "0229b18e1990a07a23b073582ad0a8cc"),
)


@dataclass(frozen=True)
class Template:
    name: str
    pose: str
    source: int | str  # public LXNS icon ID or Tencent sticker ID
    color: str


TEMPLATES = (
    Template("百合咲", "经典", 601, "#DB277F"),
    Template("艾希德", "经典", 200405, "#6543A3"),
    *(Template("纱露朵", name, key, "#8050BB") for name, key in SALT_ITEMS),
    Template("百合咲", "形象二", 100501, "#CF3D83"),
    Template("艾希德", "幼年", 400405, "#5250BC"),
    *(Template("蓝莓布丁", name, key, "#6653A7") for name, key in BLUEBERRY_ITEMS),
    *(Template("香芋甜筒", name, key, "#9D468C") for name, key in TARO_ITEMS),
)
ALIASES = {
    "mika": "百合咲",
    "百合咲美嘉": "百合咲",
    "百合咲ミカ": "百合咲",
    "acid": "艾希德",
    "アシッド": "艾希德",
    "salt": "纱露朵",
    "ソルト": "纱露朵",
    "小勺薄盐": "纱露朵",
}
EXAMPLE = "/表情包 纱露朵/心心 今天也要鸟加！"
PACKS = {
    "纱露朵": ("小勺薄盐", 242177, ""),
    "蓝莓布丁": ("蓝莓布丁", 243044, "绘：青叶"),
    "香芋甜筒": ("香芋甜筒", 242679, ""),
}
GALLERY_NAMES = ("百合咲", "纱露朵", "艾希德", "蓝莓布丁", "香芋甜筒")


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _art(raw: bytes, size: tuple[int, int]) -> Image.Image:
    with Image.open(io.BytesIO(raw)) as src:
        if src.width * src.height > 4_000_000:
            raise ValueError("底图像素过大")
        art = ImageOps.exif_transpose(src).convert("RGBA")
    bounds = art.getbbox()
    if bounds:
        art = art.crop(bounds)
    return ImageOps.contain(art, size, Image.Resampling.LANCZOS)


def _caption_layer(caption: str, accent: str) -> Image.Image:
    # Pick the largest font that fits the actual ink, including punctuation,
    # long Latin strings and the angled outline. Explicit newlines are preserved.
    initial_size = 90
    if len(caption) <= 12 and "\n" not in caption:
        original_width = _font(initial_size, bold=True).getlength(caption)
        initial_size = min(initial_size, int(initial_size * 440 / max(original_width, 1)))
    for size in range(initial_size, 13, -2):
        font = _font(size, bold=True)
        rows = []
        for paragraph in caption.split("\n"):
            line = ""
            for char in paragraph:
                if line and font.getlength(line + char) > 440:
                    rows.append(line)
                    line = ""
                line += char
            rows.append(line)
        metrics = [font.getbbox(row or "国") for row in rows]
        heights = [max(1, box[3] - box[1]) for box in metrics]
        height = sum(heights) + (len(rows) - 1) * 12
        if height <= 165:
            break
    else:
        raise ValueError("文字行数过多，请减少换行后重试。")
    width = max(box[2] - box[0] for box in metrics)
    layer = Image.new("RGBA", (width + 32, height + 36))
    draw = ImageDraw.Draw(layer)
    y = 12
    for row, box, row_height in zip(rows, metrics, heights, strict=True):
        x = (layer.width - (box[2] - box[0])) // 2 - box[0]
        pos = (x, y - box[1])
        draw.text(
            (x + 2, pos[1] + 5),
            row,
            font=font,
            fill="#FFCB55",
            stroke_width=9,
            stroke_fill="#43304F",
        )
        draw.text(pos, row, font=font, fill=accent, stroke_width=7, stroke_fill="white")
        y += row_height + 12
    ink = layer.getbbox()
    if not ink:
        raise ValueError("请输入可显示的文字。")
    return layer.crop(ink).rotate(3, Image.Resampling.BICUBIC, expand=True)


def render_sticker(template: Template, raw: bytes, caption: str) -> bytes:
    if len(caption) > 100:
        raise ValueError("台词太长了，每张表情最多 100 字。")
    letters = _caption_layer(caption, template.color)
    canvas = Image.new("RGBA", (512, 512))
    art = _art(raw, (480, 490 - letters.height + 22))
    canvas.alpha_composite(art, ((512 - art.width) // 2, 8))
    canvas.alpha_composite(
        letters, ((512 - letters.width) // 2, min(504 - letters.height, art.height))
    )
    return _png(canvas)


def _gallery(templates: tuple[Template, ...], images: list[bytes | None], title: str) -> bytes:
    columns = min(len(templates), 3 if title == "舞萌表情包" else 4)
    rows = (len(templates) + columns - 1) // columns
    width = 48 + columns * 174
    canvas = Image.new("RGB", (width, 110 + rows * 182), "#FCF8FF")
    draw = ImageDraw.Draw(canvas)
    draw.text((25, 16), title, fill="#422C58", font=_font(34, bold=True))
    draw.text((27, 62), "发送 /表情包 编号 台词", fill="#725B81", font=_font(20))
    for i, (template, raw) in enumerate(zip(templates, images, strict=True)):
        x, y = 24 + i % columns * 174, 95 + i // columns * 182
        rgb = ImageColor.getrgb(template.color)
        tint = tuple(217 + v * 38 // 255 for v in rgb)
        draw.rounded_rectangle((x, y, x + 162, y + 171), radius=15, fill=tint)
        if raw:
            art = _art(raw, (148, 126))
            canvas.paste(art, (x + (162 - art.width) // 2, y + 4), art)
        else:
            draw.text((x + 12, y + 48), "底图暂缺", font=_font(20), fill="#725B81")
        number = TEMPLATES.index(template) + 1
        label = template.pose if title != "舞萌表情包" else template.name
        draw.text(
            (x + 9, y + 133), f"{number:02}  {label}", font=_font(21, bold=True), fill="#382140"
        )
    return _png(canvas)


def register_sticker_commands(router: CommandRouter, cosmetics: CosmeticService) -> None:
    cache: dict[str, bytes] = {}
    downloads = asyncio.Semaphore(4)
    renders = asyncio.Semaphore(2)

    async def fetch(template: Template, http: httpx.AsyncClient) -> bytes:
        if isinstance(template.source, int):
            raw = await cosmetics.asset("icon", template.source)
            if raw is None:
                raise ValueError("角色底图暂时不可用。")
            return raw
        key = template.source
        async with downloads:
            if key not in cache:
                url = f"https://gxh.vip.qq.com/club/item/parcel/item/{key[:2]}/{key}/300x300.png"
                response = await http.get(url, timeout=10)
                response.raise_for_status()
                if (
                    not response.content.startswith(b"\x89PNG\r\n\x1a\n")
                    or len(response.content) > 1_000_000
                ):
                    raise ValueError("腾讯商城暂未返回可读取的底图。")
                cache[key] = response.content
        return cache[key]

    @router.command(
        "sticker",
        "看图选角色或表情，把自定义台词做成舞萌贴纸",
        aliases=("表情包", "舞萌表情", "做表情"),
        category="娱乐",
        usage=EXAMPLE,
    )
    async def sticker(request: CommandRequest) -> str | BotReply:
        argument = request.argument.strip()
        tokens = argument.split(maxsplit=1)
        selector, caption = tokens[0] if tokens else "", tokens[1] if len(tokens) == 2 else ""
        name, _, pose = selector.partition("/")
        name = ALIASES.get(name.casefold(), name)
        templates = tuple(t for t in TEMPLATES if t.name == name and (not pose or t.pose == pose))
        if selector.isascii() and selector.isdecimal() and len(selector) <= 3:
            index = int(selector)
            templates = (TEMPLATES[index - 1],) if 1 <= index <= len(TEMPLATES) else ()
        is_menu = argument in {"", "列表", "目录", "帮助"}
        if is_menu:
            templates = tuple(
                next(t for t in TEMPLATES if t.name == group) for group in GALLERY_NAMES
            )
        if not templates:
            return "没有找到这张底图。发送 /表情包 查看角色；发送 /表情包 小勺薄盐 查看整套编号。"
        try:
            async with httpx.AsyncClient() as http:
                if not caption:

                    async def thumbnail(t: Template) -> bytes | None:
                        try:
                            return await fetch(t, http)
                        except (httpx.HTTPError, CosmeticError, ValueError):
                            return None

                    raws = await asyncio.gather(*(thumbnail(t) for t in templates))
                    async with renders:
                        png = await asyncio.to_thread(
                            _gallery,
                            templates,
                            raws,
                            ("舞萌表情包" if is_menu else f"{templates[0].name} · 选底图"),
                        )
                    group = templates[0].name
                    if is_menu:
                        actions = tuple(QuickAction(n, f"/表情包 {n}") for n in GALLERY_NAMES)
                        instructions = "点选角色或表情包查看全部底图。纱露朵使用《小勺薄盐》。"
                    else:
                        example = f"/表情包 {group}/{templates[0].pose} "
                        actions = (QuickAction("填写台词", example, False),)
                        instructions = (
                            f"发送 /表情包 编号 台词，或用名称，例如 {example}今天也要鸟加！"
                        )
                        if group in PACKS:
                            pack, pack_id, credit = PACKS[group]
                            url = f"https://gxh.vip.qq.com/club/themes/mobile/bq/html/detail.html?id={pack_id}"
                            actions += (QuickAction(f"{pack} · 原作", url=url),)
                            instructions += f"\nQQ 表情商城《{pack}》" + (
                                f" · {credit}" if credit else ""
                            )
                    return BotReply(
                        text="舞萌表情包底图目录",
                        image_png=png,
                        followup_text=instructions,
                        quick_actions=actions,
                    )
                template = templates[0]
                if len(caption) > 100:
                    return "台词太长了，每张表情最多 100 字。"
                raw = await fetch(template, http)
                async with renders:
                    png = await asyncio.to_thread(render_sticker, template, raw, caption)
                return BotReply(text=f"{template.name} · {template.pose}", image_png=png)
        except (ValueError, B50RenderError) as exc:
            return str(exc)
        except (httpx.HTTPError, CosmeticError, OSError, Image.DecompressionBombError):
            return "这张底图暂时无法读取，请稍后重试，或从 /表情包 选择其他底图。"
