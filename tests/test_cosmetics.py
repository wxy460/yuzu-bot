import io
import json

import httpx
from PIL import Image

from maimai_bot.services.cosmetics import CosmeticService


def _png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


async def test_search_choose_and_persist_cosmetics(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/plate/list"):
            return httpx.Response(
                200,
                json={"plates": [{"id": 6113, "name": "橙将", "genre": "実績"}]},
            )
        if request.url.path.endswith("/icon/list"):
            return httpx.Response(
                200,
                json={"icons": [{"id": 101, "name": "でらっくま"}]},
            )
        return httpx.Response(200, content=_png("purple"))

    state_path = tmp_path / "cosmetics.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CosmeticService(client, state_path=state_path)
        assert (await service.search("plate", "橙将"))[0].id == 6113
        await service.choose("user", "plate", 6113)
        await service.choose("user", "icon", 101)
        profile = await service.profile("user")

    assert profile.avatar_png is not None
    assert profile.nameplate_png is not None
    assert json.loads(state_path.read_text())["user"] == {"icon_id": 101, "plate_id": 6113}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        restored = CosmeticService(client, state_path=state_path)
        assert restored.selection("user").plate_id == 6113
        await restored.reset("user", "icon")

    assert json.loads(state_path.read_text())["user"] == {"plate_id": 6113}


async def test_default_selection_keeps_qq_avatar_and_automatic_plate(tmp_path) -> None:
    async with httpx.AsyncClient() as client:
        service = CosmeticService(client, state_path=tmp_path / "missing.json")
        profile = await service.profile("new-user")

    assert profile.avatar_png is None
    assert profile.nameplate_png is None
