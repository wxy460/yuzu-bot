from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

AUTH_BASE = "https://auth.diving-fish.com"
AUTHORIZED_APPS = f"{AUTH_BASE}/apps"
PROBER_BASE = "https://www.diving-fish.com/api/maimaidxprober"
SCOPE = "prober.records.read"


def chart_play_count(record: dict[str, Any]) -> int | None:
    """Only an explicit count for this chart represents lifetime plays."""
    counts = set()
    for field in ("play_count", "playCount"):
        value = record.get(field)
        if value is None:
            continue
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            try:
                value = int(value)
            except ValueError:
                return None
        if type(value) is not int or value < 0:
            return None
        counts.add(value)
    return counts.pop() if len(counts) == 1 else None


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

    scope = SCOPE

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        binding_state: Path | None = None,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if binding_state is not None:
            binding_state.parent.mkdir(parents=True, exist_ok=True)
        self._bindings = sqlite3.connect(binding_state or ":memory:")
        if binding_state is not None:
            binding_state.chmod(0o600)
        self._bindings.execute("""
            CREATE TABLE IF NOT EXISTS water_binding (
                original TEXT PRIMARY KEY, subject TEXT NOT NULL, requested INTEGER NOT NULL)
        """)

    def close(self):
        self._bindings.close()

    @property
    def enabled(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _subject_ref(self, external_id: str) -> str:
        value = f"{self._client_id}:{external_id}".encode()
        return hashlib.sha256(value).hexdigest()

    def _binding(self, user: str) -> tuple[str, int]:
        # Existing installations used a deterministic ref. Preserve that ref until
        # its owner explicitly unlinks it, then permanently abandon the old grant.
        original = self._subject_ref(user)
        row = self._bindings.execute(
            "SELECT subject,requested FROM water_binding WHERE original=?", (original,)
        ).fetchone()
        # 0 = locally disconnected, 1 = eligible to request a token,
        # 2 = this grant has successfully returned a token before.
        return (row[0], row[1]) if row else (original, 1)

    def history_key(self, user: str) -> str:
        subject, _ = self._binding(user)
        return user if subject == self._subject_ref(user) else f"water:{subject}:{user}"

    def unlink(self, user: str) -> None:
        original = self._subject_ref(user)
        subject, _ = self._binding(user)
        with self._bindings:
            self._bindings.execute(
                "INSERT OR REPLACE INTO water_binding VALUES(?,?,0)",
                (original, secrets.token_hex(32)),
            )
        self._tokens.pop(subject, None)

    def has_current_token(self, user: str) -> bool:
        subject, _ = self._binding(user)
        cached = self._tokens.get(subject)
        return bool(cached and time.time() < cached[1] - 30)

    async def start_binding(self, external_id: str) -> BindingInfo:
        self._require_enabled()
        game = "舞萌 DX" if self.scope == SCOPE else "中二节奏"
        label = f"{game} · {_masked_label(external_id)}"
        subject, _ = self._binding(external_id)
        try:
            response = await self._http.post(
                f"{AUTH_BASE}/oauth/device_authorization",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": self.scope,
                    "subject_ref": subject,
                    "binding_label": label,
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise ScoreServiceError(f"连接水鱼授权服务失败：{exc}") from exc
        await _raise_api_error(response, "创建绑定链接失败")
        try:
            data = response.json()
            binding = BindingInfo(
                url=str(data["verification_uri_complete"]),
                user_code=str(data["user_code"]),
                expires_in=int(data.get("expires_in", 600)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoreServiceError("水鱼授权服务返回了无法识别的数据") from exc
        if self._binding(external_id)[0] != subject:
            raise NotBound("本次绑定已取消，请重新发送 /bind。")
        with self._bindings:
            self._bindings.execute(
                "UPDATE water_binding SET requested=1 WHERE original=? AND subject=? AND requested=0",
                (self._subject_ref(external_id), subject),
            )
        return binding

    async def best50(self, external_id: str) -> Best50:
        return (await self.player_records(external_id)).best50()

    async def player_records(self, external_id: str) -> PlayerRecords:
        """Return every score exposed by the user's Diving-Fish OAuth grant."""
        self._require_enabled()
        subject, _ = self._binding(external_id)
        token = await self._access_token(external_id)
        old_data, new_data = await asyncio.gather(
            self._records(token, subject, is_new=False),
            self._records(token, subject, is_new=True),
        )
        if self._binding(external_id)[0] != subject:
            raise NotBound("本次查询的水鱼绑定已解除。")
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
        subject, state = self._binding(external_id)
        if state == 0:
            raise NotBound("已停用此身份的水鱼舞萌绑定。")
        cached = self._tokens.get(subject)
        if cached and time.time() < cached[1] - 30:
            return cached[0]

        lock = self._locks.setdefault(external_id, asyncio.Lock())
        async with lock:
            if self._binding(external_id)[0] != subject:
                raise NotBound("本次查询的水鱼绑定已解除。")
            cached = self._tokens.get(subject)
            if cached and time.time() < cached[1] - 30:
                return cached[0]
            try:
                response = await self._http.post(
                    f"{AUTH_BASE}/oauth/token",
                    data={
                        "grant_type": "urn:diving-fish:params:oauth:grant-type:on-behalf-of",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "subject": "ref:" + subject,
                        "scope": self.scope,
                    },
                    timeout=15,
                )
            except httpx.HTTPError as exc:
                raise ScoreServiceError(f"连接水鱼授权服务失败：{exc}") from exc
            if response.status_code == 400:
                try:
                    if response.json().get("error") == "consent_required":
                        if self._binding(external_id) == (subject, 2):
                            # A previously usable grant disappeared at the provider.
                            # Quarantine prior chat/rating history just as for /unbind.
                            self.unlink(external_id)
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
            if self._binding(external_id)[0] != subject:
                raise NotBound("本次查询的水鱼绑定已解除。")
            if state != 2:
                original = self._subject_ref(external_id)
                with self._bindings:
                    self._bindings.execute(
                        "INSERT OR IGNORE INTO water_binding VALUES(?,?,2)", (original, subject)
                    )
                    self._bindings.execute(
                        "UPDATE water_binding SET requested=2 WHERE original=? AND subject=?",
                        (original, subject),
                    )
            self._tokens[subject] = (token, time.time() + expires_in)
            return token

    async def _records(self, token: str, subject: str, *, is_new: bool) -> dict[str, Any]:
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
            cached = self._tokens.get(subject)
            if cached and cached[0] == token:
                self._tokens.pop(subject, None)
        await _raise_api_error(response, "查询成绩失败")
        data = response.json()
        for record in data.get("records", ()):
            if "play_count" in record or "playCount" in record:
                count = chart_play_count(record)
                record.pop("playCount", None)
                if count is None:
                    record.pop("play_count", None)
                else:
                    record["play_count"] = count
        return data

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
