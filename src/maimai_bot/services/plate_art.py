"""Resolve the exact artwork for an achievement or equipped nameplate."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from pathlib import Path

import httpx
from PIL import Image

from .lxns_assets import LxnsAssetError, download_lxns_png
from .plate_names import normalize_plate_name

logger = logging.getLogger(__name__)
CATALOG_URL = "https://maimai.lxns.net/api/v0/maimai/plate/list"
CATALOG_TTL = 6 * 3600


class PlateArtwork:
    def __init__(self, http: httpx.AsyncClient, directory: Path):
        self.http = http
        self.directory = directory
        self._bundled = {"デフォルト": 1, "橙将": 6113}
        try:
            raw = json.loads((directory / "index.json").read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("expected a name/ID mapping")
            self._bundled.update(
                {
                    normalize_plate_name(name): item_id
                    for name, item_id in raw.items()
                    if isinstance(name, str) and name and type(item_id) is int and item_id > 0
                }
            )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("无法读取本地姓名框索引 (%s)", type(exc).__name__)
        self._catalog: dict[str, int] = {}
        self._next_refresh = 0.0
        self._images: dict[int, bytes] = {}
        self._catalog_lock = asyncio.Lock()
        self._loading: dict[int, asyncio.Task] = {}

    async def _current_id(self, name: str) -> int | None:
        if time.monotonic() >= self._next_refresh:
            try:
                response = await self.http.get(CATALOG_URL, timeout=12)
                response.raise_for_status()
                plates = response.json()["plates"]
                if not isinstance(plates, list) or not plates:
                    raise ValueError("empty plate catalog")
                by_name = {}
                for plate in plates:
                    if not isinstance(plate, dict) or not isinstance(plate.get("name"), str):
                        continue
                    try:
                        item_id = int(plate.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if item_id > 0:
                        by_name[normalize_plate_name(plate["name"])] = item_id
                if not by_name:
                    raise ValueError("invalid plate catalog")
                self._catalog = by_name
                self._next_refresh = time.monotonic() + CATALOG_TTL
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                # Keep earlier successful entries across a transient directory failure.
                self._next_refresh = time.monotonic() + 60
                logger.info("姓名框目录暂时不可用 (%s)", type(exc).__name__)
        return self._catalog.get(name)

    async def _image(self, item_id: int) -> bytes | None:
        if item_id in self._images:
            return self._images[item_id]
        task = self._loading.get(item_id)
        if task is None:
            task = asyncio.create_task(self._load_image(item_id))
            self._loading[item_id] = task
            task.add_done_callback(lambda _: self._loading.pop(item_id, None))
        return await asyncio.shield(task)

    async def _load_image(self, item_id: int) -> bytes | None:
        try:
            data = (self.directory / f"{item_id}.png").read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except (OSError, ValueError, SyntaxError):
            try:
                data = await download_lxns_png(self.http, "plate", item_id)
            except LxnsAssetError:
                return None
        if len(self._images) >= 128:
            self._images.pop(next(iter(self._images)))
        self._images[item_id] = data
        return data

    async def resolve(self, name: str) -> bytes | None:
        name = normalize_plate_name(name)
        if not name or name == "舞萌玩家":
            name = "デフォルト"
        bundled_id = self._bundled.get(name)
        if bundled_id:
            data = await self._image(bundled_id)
            if data is not None:
                return data
        async with self._catalog_lock:
            current_id = await self._current_id(name)
        if current_id is not None and current_id != bundled_id:
            return await self._image(current_id)
        return None
