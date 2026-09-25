from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

AUTH_BASE = "https://auth.diving-fish.com"
PROBER_BASE = "https://www.diving-fish.com/api/maimaidxprober"
SCOPE = "prober.records.read"


class ScoreServiceError(RuntimeError):
    pass


class NotBound(ScoreServiceError):
    pass


@dataclass(frozen=True, slots=True)
class BindingInfo:
    url: str
    user_code: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class Best50:
    nickname: str
    rating: int
    additional_rating: int
    plate: str
    old: list[dict[str, Any]]
    new: list[dict[str, Any]]

    def as_text(self, detail: int = 5) -> str:
        rows = [f"{self.nickname} 的 DX Rating：{self.rating}", "", "B35（旧曲）前列："]
        rows.extend(_format_score(item, index) for index, item in enumerate(self.old[:detail], 1))
        rows.append("\nB15（新曲）前列：")
        rows.extend(_format_score(item, index) for index, item in enumerate(self.new[:detail], 1))
        rows.append("\n详细成绩见随消息发送的 B50 图片。")
        return "\n".join(rows)


@dataclass(frozen=True, slots=True)
class PlayerRecords:
    nickname: str
    rating: int
    additional_rating: int
    plate: str
    old: list[dict[str, Any]]
    new: list[dict[str, Any]]

    def best50(self) -> Best50:
        return Best50(
            nickname=self.nickname,
            rating=self.rating,
            additional_rating=self.additional_rating,
            plate=self.plate,
            old=sorted(self.old, key=_record_sort_key)[:35],
            new=sorted(self.new, key=_record_sort_key)[:15],
        )


def _format_score(item: dict[str, Any], index: int) -> str:
    return (
        f"{index}. {item.get('title', '未知曲目')} "
        f"[{item.get('level_label', '?')} {item.get('level', '?')}] "
        f"{float(item.get('achievements', 0)):.4f}%  ra {int(item.get('ra', 0))}"
    )


class DivingFishService:
    """Diving-Fish confidential-client device binding and on-behalf-of flow."""

    def __init__(self, http: httpx.AsyncClient, *, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _subject_ref(self, external_id: str) -> str:
        value = f"{self._client_id}:{external_id}".encode()
        return hashlib.sha256(value).hexdigest()

    def _subject(self, external_id: str) -> str:
        return "ref:" + self._subject_ref(external_id)

    async def start_binding(self, external_id: str) -> BindingInfo:
        self._require_enabled()
        label = _masked_label(external_id)
        try:
            response = await self._http.post(
                f"{AUTH_BASE}/oauth/device_authorization",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": SCOPE,
                    "subject_ref": self._subject_ref(external_id),
                    "binding_label": label,
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise ScoreServiceError(f"连接水鱼授权服务失败：{exc}") from exc
        await _raise_api_error(response, "创建绑定链接失败")
        try:
            data = response.json()
            return BindingInfo(
                url=str(data["verification_uri_complete"]),
                user_code=str(data["user_code"]),
                expires_in=int(data.get("expires_in", 600)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoreServiceError("水鱼授权服务返回了无法识别的数据") from exc

    async def best50(self, external_id: str) -> Best50:
        return (await self.player_records(external_id)).best50()

    async def player_records(self, external_id: str) -> PlayerRecords:
        """Return every score exposed by the user's Diving-Fish OAuth grant."""
        self._require_enabled()
        token = await self._access_token(external_id)
        old_data, new_data = await asyncio.gather(
            self._records(token, is_new=False),
            self._records(token, is_new=True),
        )
        return PlayerRecords(
            nickname=str(old_data.get("nickname") or new_data.get("nickname") or "玩家"),
            rating=int(old_data.get("rating") or new_data.get("rating") or 0),
            additional_rating=int(
                old_data.get("additional_rating") or new_data.get("additional_rating") or 0
            ),
            plate=str(old_data.get("plate") or new_data.get("plate") or "舞萌玩家"),
            old=list(old_data.get("records", [])),
            new=list(new_data.get("records", [])),
        )

    async def _access_token(self, external_id: str) -> str:
        cached = self._tokens.get(external_id)
        if cached and time.time() < cached[1] - 30:
            return cached[0]

        lock = self._locks.setdefault(external_id, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(external_id)
            if cached and time.time() < cached[1] - 30:
                return cached[0]
            try:
                response = await self._http.post(
                    f"{AUTH_BASE}/oauth/token",
                    data={
                        "grant_type": "urn:diving-fish:params:oauth:grant-type:on-behalf-of",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "subject": self._subject(external_id),
                        "scope": SCOPE,
                    },
                    timeout=15,
                )
            except httpx.HTTPError as exc:
                raise ScoreServiceError(f"连接水鱼授权服务失败：{exc}") from exc
            if response.status_code == 400:
                try:
                    if response.json().get("error") == "consent_required":
                        raise NotBound("尚未绑定水鱼账号")
                except ValueError:
                    pass
            await _raise_api_error(response, "获取查分授权失败")
            try:
                data = response.json()
                token = str(data["access_token"])
                expires_in = int(data.get("expires_in", 300))
            except (KeyError, TypeError, ValueError) as exc:
                raise ScoreServiceError("水鱼授权服务返回了无法识别的数据") from exc
            self._tokens[external_id] = (token, time.time() + expires_in)
            return token

    async def _records(self, token: str, *, is_new: bool) -> dict[str, Any]:
        try:
            response = await self._http.get(
                f"{PROBER_BASE}/player/records",
                headers={"Authorization": f"Bearer {token}"},
                params={"is_new": "true" if is_new else "false"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ScoreServiceError(f"连接水鱼查分器失败：{exc}") from exc
        if response.status_code == 401:
            self._tokens.clear()
        await _raise_api_error(response, "查询成绩失败")
        return response.json()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ScoreServiceError("查分功能尚未配置水鱼 OAuth 客户端。")


def _masked_label(external_id: str) -> str:
    if len(external_id) <= 8:
        return "QQ Bot 用户 ****"
    return f"QQ Bot 用户 {external_id[:4]}****{external_id[-4:]}"


def _record_sort_key(item: dict[str, Any]) -> tuple[float, float, float, int]:
    """Order B50 by RA, chart constant, achievement, then song ID."""
    try:
        rating = float(item.get("ra", 0))
    except (TypeError, ValueError):
        rating = 0
    try:
        chart_constant = float(item.get("ds", 0))
    except (TypeError, ValueError):
        chart_constant = 0
    try:
        achievement = float(item.get("achievements", 0))
    except (TypeError, ValueError):
        achievement = 0
    try:
        song_id = int(item.get("song_id", 0))
    except (TypeError, ValueError):
        song_id = 0
    return -rating, -chart_constant, -achievement, song_id


async def _raise_api_error(response: httpx.Response, prefix: str) -> None:
    if response.is_success:
        return
    try:
        data = response.json()
        detail = data.get("message") or data.get("error") or response.text[:160]
    except ValueError:
        detail = response.text[:160]
    if response.status_code == 429:
        detail = "调用频率或当日额度已达上限"
    raise ScoreServiceError(f"{prefix}（HTTP {response.status_code}）：{detail}")
