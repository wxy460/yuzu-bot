import io
from pathlib import Path

import httpx
from PIL import Image

from maimai_bot.domain import BotReply, MessageContext
from maimai_bot.router import CommandRouter
from maimai_bot.sticker_commands import register_sticker_commands


def setup(monkeypatch):
    art = io.BytesIO()
    Image.new("RGBA", (180, 160), "#7755DC").save(art, "PNG")
    img = art.getvalue()
    requests = []

    class Cosmetics:
        async def asset(self, kind, item_id):
            requests.append((kind, item_id))
            return img

    def get(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=img)

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client(transport=httpx.MockTransport(get)))
    router = CommandRouter(None)
    register_sticker_commands(router, Cosmetics())

    async def command(text):
        return await router.dispatch(MessageContext("u", "g", "group", text, "m"))

    return command, requests


async def test_pack_manifest_and_cached_number_match(monkeypatch):
    command, requests = setup(monkeypatch)
    menu = await command("/表情包 小勺薄盐")
    assert isinstance(menu, BotReply) and menu.quick_actions[-1].url.endswith("id=242177")
    assert len(requests) == 16
    cry = await command("/sticker 04 好耶\n再来一次！")
    by_name = await command("/舞萌表情 纱露朵/哭哭 好耶\n再来一次！")
    assert len(requests) == 16
    assert cry.image_png == by_name.image_png
    image = Image.open(io.BytesIO(cry.image_png))
    assert image.size == (512, 512) and image.getpixel((0, 0))[3] == 0
    assert "哭哭" in cry.text
    assert cry.image_png != (await command("/做表情 salt/哭哭 啊！")).image_png


async def test_marketplace_packs_have_independent_galleries_and_stable_ids(monkeypatch):
    command, calls = setup(monkeypatch)
    for name, pose, number, pack in (
        ("蓝莓布丁", "薯片", 23, 243044),
        ("香芋甜筒", "猫猫", 39, 242679),
    ):
        before = len(calls)
        gallery = await command(f"/表情包 {name}")
        assert gallery.image_png and len(calls) - before == 16
        assert gallery.quick_actions[-1].url.endswith(f"id={pack}")
        if name == "蓝莓布丁":
            assert "青叶" in gallery.followup_text
        by_name = await command(f"/表情包 {name}/{pose} 再来一把")
        by_id = await command(f"/表情包 {number} 再来一把")
        assert by_name.image_png and by_name == by_id
        assert len(calls) - before == 16
    menu = await command("/表情包")
    assert {"蓝莓布丁", "香芋甜筒"}.issubset({action.label for action in menu.quick_actions})


async def test_game_characters_and_input_limits(monkeypatch):
    command, requests = setup(monkeypatch)
    for name, icon in (("百合咲", 601), ("艾希德", 200405)):
        assert (await command(f"/表情包 {name} 测试")).image_png
        assert requests[-1] == ("icon", icon)
    assert "没有找到" in await command("/表情包 999 示例")
    assert "100 字" in await command("/表情包 1 " + "啊" * 101)
    assert "换行" in await command("/表情包 1 " + "啊\n" * 40)
    assert (await command("/表情包 1 " + "盐" * 100)).image_png
    assert (await command("/表情包")).image_png


def test_known_icon_ids_correspond_to_characters():
    # Regression guard against a catalogue update silently substituting another character.
    import json

    from maimai_bot.sticker_commands import TEMPLATES

    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "bot-homepage/dist/cosmetics.json").read_text()
    )
    names = {entry["id"]: entry["name"] for entry in catalog["icons"]}
    for template in TEMPLATES:
        if isinstance(template.source, int):
            assert names[template.source].startswith(
                {"艾希德": "アシッド", "百合咲": "百合咲"}[template.name]
            )
