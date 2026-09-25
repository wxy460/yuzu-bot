from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from .b50_image import CosmeticProfile
from .lxns_assets import LxnsAssetError, download_lxns_png

logger = logging.getLogger(__name__)

LXNS_API_BASE = "https://maimai.lxns.net/api/v0/maimai"
CosmeticKind = Literal["icon", "plate"]


class CosmeticError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CosmeticSelection:
    icon_id: int | None = None
    plate_id: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionItem:
    id: int
    name: str
    description: str = ""
    genre: str = ""


class CosmeticService:
    """Per-QQ cosmetic choices backed by LXNS's public game-asset catalogue.

    The selection is deliberately independent from score-provider bindings.
    No LXNS player account or token is needed to select public assets.
    """

    def __init__(self, http: httpx.AsyncClient, *, state_path: Path) -> None:
        self._http = http
        self._state_path = state_path
        self._selections = self._load()
        self._catalogs: dict[CosmeticKind, tuple[CollectionItem, ...]] = {}
        self._asset_cache: dict[tuple[CosmeticKind, int], bytes | None] = {}
        self._state_lock = asyncio.Lock()
        self._catalog_lock = asyncio.Lock()
        self._asset_lock = asyncio.Lock()

    def selection(self, user_id: str) -> CosmeticSelection:
        return self._selections.get(user_id, CosmeticSelection())

    async def search(
        self,
        kind: CosmeticKind,
        query: str,
        *,
        limit: int = 10,
    ) -> list[CollectionItem]:
        items = await self._catalog(kind)
        needle = query.strip().casefold()
        if needle.isdecimal():
            exact = [item for item in items if item.id == int(needle)]
            if exact:
                return exact
        if not needle:
            return list(items[:limit])
        starts = [item for item in items if item.name.casefold().startswith(needle)]
        contains = [item for item in items if needle in item.name.casefold() and item not in starts]
        return (starts + contains)[:limit]

    async def choose(self, user_id: str, kind: CosmeticKind, item_id: int) -> CollectionItem:
        matches = await self.search(kind, str(item_id), limit=1)
        if not matches or matches[0].id != item_id:
            label = "头像" if kind == "icon" else "姓名框"
            raise CosmeticError(f"没有找到 ID 为 {item_id} 的{label}。")
        current = self.selection(user_id)
        updated = CosmeticSelection(
            icon_id=item_id if kind == "icon" else current.icon_id,
            plate_id=item_id if kind == "plate" else current.plate_id,
        )
        await self._save_selection(user_id, updated)
        return matches[0]

    async def reset(self, user_id: str, kind: CosmeticKind | None = None) -> None:
        current = self.selection(user_id)
        if kind is None:
            updated = CosmeticSelection()
        elif kind == "icon":
            updated = CosmeticSelection(icon_id=None, plate_id=current.plate_id)
        else:
            updated = CosmeticSelection(icon_id=current.icon_id, plate_id=None)
        await self._save_selection(user_id, updated)

    async def describe(self, user_id: str) -> tuple[CollectionItem | None, CollectionItem | None]:
        selected = self.selection(user_id)
        icon, plate = await asyncio.gather(
            self._item("icon", selected.icon_id),
            self._item("plate", selected.plate_id),
        )
        return icon, plate

    async def profile(self, user_id: str) -> CosmeticProfile:
        selected = self.selection(user_id)
        icon_png, plate_png = await asyncio.gather(
            self._asset("icon", selected.icon_id),
            self._asset("plate", selected.plate_id),
        )
        return CosmeticProfile(avatar_png=icon_png, nameplate_png=plate_png)

    async def asset(self, kind: CosmeticKind, item_id: int) -> bytes | None:
        """Return a public collection asset for list previews."""
        return await self._asset(kind, item_id)

    async def _item(self, kind: CosmeticKind, item_id: int | None) -> CollectionItem | None:
        if item_id is None:
            return None
        return next((item for item in await self._catalog(kind) if item.id == item_id), None)

    async def _catalog(self, kind: CosmeticKind) -> tuple[CollectionItem, ...]:
        cached = self._catalogs.get(kind)
        if cached is not None:
            return cached
        async with self._catalog_lock:
            cached = self._catalogs.get(kind)
            if cached is not None:
                return cached
            try:
                response = await self._http.get(f"{LXNS_API_BASE}/{kind}/list", timeout=30)
                response.raise_for_status()
                data = response.json()
                raw_items = data.get("icons" if kind == "icon" else "plates", [])
                items = tuple(
                    CollectionItem(
                        id=int(item["id"]),
                        name=str(item.get("name") or f"ID {item['id']}"),
                        description=str(item.get("description") or ""),
                        genre=str(item.get("genre") or ""),
                    )
                    for item in raw_items
                    if isinstance(item, dict) and item.get("id") is not None
                )
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                raise CosmeticError(f"获取舞萌{self._kind_label(kind)}目录失败：{exc}") from exc
            self._catalogs[kind] = items
            return items

    async def _asset(self, kind: CosmeticKind, item_id: int | None) -> bytes | None:
        if item_id is None:
            return None
        key = (kind, item_id)
        async with self._asset_lock:
            if key in self._asset_cache:
                return self._asset_cache[key]
        try:
            content = await download_lxns_png(self._http, kind, item_id)
        except LxnsAssetError as exc:
            raise CosmeticError(f"下载舞萌{self._kind_label(kind)}素材失败：{exc}") from exc
        async with self._asset_lock:
            if len(self._asset_cache) >= 256:
                self._asset_cache.pop(next(iter(self._asset_cache)))
            self._asset_cache[key] = content
        return content

    async def _save_selection(self, user_id: str, selection: CosmeticSelection) -> None:
        async with self._state_lock:
            if selection == CosmeticSelection():
                self._selections.pop(user_id, None)
            else:
                self._selections[user_id] = selection
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            payload = {
                key: {name: value for name, value in asdict(value).items() if value is not None}
                for key, value in self._selections.items()
            }
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self._state_path)

    def _load(self) -> dict[str, CosmeticSelection]:
        try:
            data: Any = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("收藏品配置根节点不是对象")
            result = {}
            for user_id, value in data.items():
                if not isinstance(value, dict):
                    continue
                result[str(user_id)] = CosmeticSelection(
                    icon_id=_optional_positive_int(value.get("icon_id")),
                    plate_id=_optional_positive_int(value.get("plate_id")),
                )
            return result
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError):
            logger.exception("读取收藏品选择配置失败，将使用默认外观")
            return {}

    @staticmethod
    def _kind_label(kind: CosmeticKind) -> str:
        return "头像" if kind == "icon" else "姓名框"


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None
