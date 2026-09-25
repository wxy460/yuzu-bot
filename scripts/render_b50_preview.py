from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx

from maimai_bot.services.b50_image import B50ImageService
from maimai_bot.services.divingfish import Best50

TITLES = (
    "QUATTUORUX",
    "SUPER AMBULANCE",
    "Tricksters!!",
    "Chronomia",
    "系ぎて",
    "VERTeX",
    "Raven Emperor",
    "BLACK SWAN",
    "Luminaria",
    "tiny tales continue",
)


def record(index: int, *, is_new: bool) -> dict:
    return {
        "song_id": 100 + index,
        "title": TITLES[index % len(TITLES)],
        "type": "DX" if index % 3 else "SD",
        "level_index": 3 if index % 5 else 4,
        "level": "14+" if index % 4 else "15",
        "ds": 14.2 + (index % 8) / 10,
        "achievements": 100.0 + ((index * 173) % 10000) / 10000,
        "ra": 326 - index,
        "rate": "sssp" if index % 3 else "sss",
        "fc": "app" if index % 5 == 0 else ("fc" if index % 4 == 0 else ""),
        "fs": "fsdp" if index % 7 == 0 else ("fsp" if index % 4 == 0 else ""),
        "is_new": is_new,
    }


async def main(*, offline: bool = False) -> None:
    root = Path(__file__).parents[1]
    output = root / "artifacts" / "b50-preview.png"
    output.parent.mkdir(exist_ok=True)
    best50 = Best50(
        nickname="SIYUE²",
        rating=15752,
        additional_rating=10,
        plate="橙将",
        old=[record(index, is_new=False) for index in range(35)],
        new=[record(index + 35, is_new=True) for index in range(15)],
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(404)) if offline else None
    async with httpx.AsyncClient(transport=transport) as client:
        renderer = B50ImageService(client, background_path=root / "assets" / "b50-background.png")
        output.write_bytes(await renderer.render(best50))
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="用占位封面快速生成预览")
    asyncio.run(main(offline=parser.parse_args().offline))
