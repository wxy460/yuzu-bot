"""Warm public jackets and/or smoke-test the real OCR + jacket matching pipeline.

Run from the repository: .venv/bin/python scripts/check_song_recognition.py
Use --warm on a production machine before starting the bot for the first time.
Only public song metadata/jackets are downloaded; no QQ messages are sent.
"""
from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from maimai_bot.services.lxns_assets import download_lxns_png
from maimai_bot.services.song_catalog import SongCatalogService
from maimai_bot.services.song_recognition import SongRecognitionService


async def run(cache_dir: Path, warm: bool) -> None:
    import cv2
    import numpy as np

    async with httpx.AsyncClient(follow_redirects=True) as http:
        catalog = SongCatalogService(http)
        service = SongRecognitionService(http, catalog, cache_dir)
        songs = await catalog.songs()
        if warm:
            await service.warm()
        cache_dir.mkdir(parents=True, exist_ok=True)
        selected = []
        for title in ("Fragrance", "PANDORA PARADOXXX", "系ぎて"):
            song = next((s for s in songs if s.title == title), None)
            if song is None:
                raise RuntimeError(f"Song missing from catalog: {title}")
            raw = await download_lxns_png(http, "jacket", song.id)
            service._save_jacket(song.id, raw)
            selected.append(song)
        await asyncio.to_thread(service._build_visual)
        for song in selected:
            jacket = cv2.imread(str(cache_dir / f"{song.id}.jpg"))
            screenshot = np.full((900, 700, 3), (210, 220, 240), dtype=np.uint8)
            screenshot[180:500, 90:410] = cv2.resize(jacket, (320, 320))
            matched = await asyncio.to_thread(service._match_visual, screenshot)
            if matched != song.id:
                raise RuntimeError(f"Visual mismatch for {song.title}: {matched}")
            image = Image.fromarray(cv2.cvtColor(screenshot, cv2.COLOR_BGR2RGB))
            ImageDraw.Draw(image).text(
                (60, 60), song.title,
                font=ImageFont.load_default(size=40), fill="black",
            )
            output = io.BytesIO()
            image.save(output, format="PNG")
            result = await asyncio.to_thread(service._recognize, output.getvalue(), songs)
            if result.song is None or result.song.id != song.id:
                raise RuntimeError(f"Recognition mismatch for {song.title}: {result}")
            print(f"PASS {song.id} {song.title}: visual + {result.method}", flush=True)
        print(f"Indexed {len(service._indexed_ids)} public jackets", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/song_recognition"))
    parser.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.cache_dir, args.warm))
