"""Read-only LXNS scores with per-user OAuth grants and explicit provider selection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from ..domain import MessageContext
from .divingfish import (
    Best50,
    DivingFishService,
    PlayerRecords,
    ScoreServiceError,
    chart_play_count,
)
from .rating import rating_for
from .song_catalog import SongCatalogError, SongCatalogService

BASE = "https://maimai.lxns.net"
MAIMAI_REBIND = "当前选择的落雪绑定已解除，请发送 /lxns 重新关联，或明确发送 /source 水鱼 切换。"
UNCONFIGURED = "落雪授权尚未启用。管理员需登记只读 OAuth 应用，并配置应用 ID 和 HTTPS 回调地址。水鱼可继续使用 /bind。"


@dataclass
class _Pair:
    target: str
    expires: float
    owner: str = ""
    binding: str = ""


class NoPlayerProfile(ScoreServiceError):
    pass


class LxnsScores:
    def __init__(
        self,
        http: httpx.AsyncClient,
        catalog: SongCatalogService,
        path: Path,
        *,
        client_id: str = "",
        redirect_uri: str = "",
        client_secret: str = "",
    ):
        self.http, self.catalog, self.path = http, catalog, path
        self.client_id, self.redirect_uri, self.secret = client_id, redirect_uri, client_secret
        self._states: dict[str, tuple[str, str, float]] = {}
        self._pairs: dict[str, _Pair] = {}
        self._binding_generation: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()

    @property
    def enabled(self):
        try:
            uri = urlsplit(self.redirect_uri)
            _ = uri.port
        except ValueError:
            return False
        return bool(
            self.client_id
            and uri.path.startswith("/")
            and not uri.query
            and not uri.fragment
            and not uri.username
            and uri.hostname
            and (
                uri.scheme == "https"
                or uri.scheme == "http"
                and uri.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS grants(
                owner TEXT PRIMARY KEY, binding TEXT NOT NULL, name TEXT NOT NULL,
                access TEXT NOT NULL, refresh TEXT NOT NULL, expires REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS subjects(
                user TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES grants(owner) ON DELETE CASCADE,
                active INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE TABLE IF NOT EXISTS chuni_sources(user TEXT PRIMARY KEY)")
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maimai_sources'"
            ).fetchone():
                # Previous versions stored the selection on a cascading group binding.
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS maimai_sources(user TEXT PRIMARY KEY)")
                db.execute("INSERT OR IGNORE INTO maimai_sources SELECT user FROM subjects WHERE active=1")
            yield db

    def identity(self, user):
        if not self.path.exists():
            return None
        with self.db() as db:
            return db.execute(
                """SELECT g.owner,g.binding,g.name,s.active FROM grants g JOIN
                subjects s ON s.owner=g.owner WHERE s.user=?""",
                (user,),
            ).fetchone()

    def uses_maimai(self, user):
        if not self.path.exists():
            return False
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM maimai_sources WHERE user=?", (user,)).fetchone())

    def select(self, user, lxns):
        if lxns and not self.identity(user):
            raise ScoreServiceError("请先发送 /lxns 完成落雪绑定。")
        if not self.path.exists():
            return
        with self.db() as db:
            if lxns:
                db.execute("INSERT OR IGNORE INTO maimai_sources VALUES(?)", (user,))
            else:
                db.execute("DELETE FROM maimai_sources WHERE user=?", (user,))

    def uses_chuni(self, user):
        if not self.path.exists():
            return False
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM chuni_sources WHERE user=?", (user,)).fetchone())

    def select_chuni(self, user, lxns):
        if not lxns and not self.path.exists():
            return
        with self.db() as db:
            if lxns:
                if not db.execute("SELECT 1 FROM subjects WHERE user=?", (user,)).fetchone():
                    raise ScoreServiceError("请先发送 /lxns 完成自己的落雪绑定或群内关联。")
                db.execute("INSERT OR IGNORE INTO chuni_sources VALUES(?)", (user,))
            else:
                db.execute("DELETE FROM chuni_sources WHERE user=?", (user,))

    def _prune(self):
        now = time.monotonic()
        for state, (user, _, expiry) in list(self._states.items()):
            if expiry <= now:
                del self._states[state]
                if self._binding_generation.get(user) == state:
                    self._binding_generation.pop(user, None)
        self._pairs = {key: pair for key, pair in self._pairs.items() if pair.expires > now}

    def begin(self, user):
        if not self.enabled:
            raise ScoreServiceError(UNCONFIGURED)
        self._prune()
        self.cancel(user)
        if len(self._states) >= 1024:
            raise ScoreServiceError("等待落雪授权的人较多，请稍后再试。")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        self._states[state] = user, verifier, time.monotonic() + 600
        self._binding_generation[user] = state
        return (
            BASE
            + "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "scope": "read_player",
                    "client_id": self.client_id,
                    "redirect_uri": self.redirect_uri,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def cancel(self, user):
        state = self._binding_generation.pop(user, None)
        if state:
            self._states.pop(state, None)

    def unlink(self, user, *, private=True):
        self.cancel(user)
        self._pairs = {
            key: pair for key, pair in self._pairs.items() if user not in {pair.target, pair.owner}
        }
        if self.path.exists():
            with self.db() as db:
                db.execute("DELETE FROM subjects WHERE user=?", (user,))
                if private:
                    db.execute("DELETE FROM grants WHERE owner=?", (user,))

    async def _token(self, **fields):
        fields["client_id"] = self.client_id
        if self.secret:
            fields["client_secret"] = self.secret
        try:
            resp = await self.http.post(
                BASE + "/api/v0/oauth/token", json=fields, timeout=15, follow_redirects=False
            )
            if resp.status_code in (400, 401, 403):
                raise ScoreServiceError("落雪授权未通过或已过期，请私聊发送 /lxns 重新授权。")
            resp.raise_for_status()
            body = resp.json()
            if (
                not body["access_token"]
                or not body["refresh_token"]
                or int(body["expires_in"]) <= 0
            ):
                raise ValueError("empty grant")
            return (
                body["access_token"],
                body["refresh_token"],
                time.time() + int(body["expires_in"]),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ScoreServiceError("落雪授权服务暂时不可用，请稍后重试。") from None

    async def _data(self, token, route, *, game="maimai"):
        game_name = "舞萌" if game == "maimai" else "中二节奏"
        try:
            resp = await self.http.get(
                BASE + "/api/v0/user/" + game + "/player" + route,
                headers={"Authorization": "Bearer " + token},
                timeout=25,
                follow_redirects=False,
            )
            if resp.status_code in (401, 403):
                raise ScoreServiceError("落雪授权已失效或缺少成绩权限，请私聊发送 /lxns 重新授权，或主动切换查分源。")
            if not route and resp.status_code == 404:
                raise NoPlayerProfile(f"落雪账号尚无{game_name}玩家资料。")
            resp.raise_for_status()
            payload = resp.json()
            if not route and (payload.get("data") is None or not payload["data"].get("name")):
                raise NoPlayerProfile(f"落雪账号尚无{game_name}玩家资料。")
            if payload.get("success") is False or payload["data"] is None:
                raise ValueError("unavailable")
            return payload["data"]
        except (httpx.HTTPError, TypeError, KeyError, AttributeError, ValueError):
            raise ScoreServiceError(
                f"落雪未能提供{game_name}成绩，请检查落雪账号的成绩同步状态。"
            ) from None

    async def finish(self, state, code):
        self._prune()
        item = self._states.pop(state, None)
        if not item:
            raise ScoreServiceError("授权链接过期或已经使用，请重新私聊发送 /lxns。")
        user, verifier, _ = item
        try:
            if not code:
                raise ScoreServiceError("本次落雪授权已取消。")
            access, refresh, expiry = await self._token(
                grant_type="authorization_code",
                code=code,
                redirect_uri=self.redirect_uri,
                code_verifier=verifier,
            )
            try:
                profile = await self._data(access, "")
            except NoPlayerProfile:
                profile = await self._data(access, "", game="chunithm")
            if self._binding_generation.get(user) != state:
                raise ScoreServiceError("本次授权已取消，请使用最新的绑定链接。")
            with self.db() as db:
                # A rebind can select a different player. Invalidate previous links.
                db.execute("DELETE FROM grants WHERE owner=?", (user,))
                db.execute(
                    "INSERT INTO grants VALUES(?,?,?,?,?,?)",
                    (user, secrets.token_hex(12), profile["name"], access, refresh, expiry),
                )
                db.execute("INSERT OR REPLACE INTO subjects VALUES(?,?,0)", (user, user))
            return str(profile["name"])
        finally:
            if self._binding_generation.get(user) == state:
                self._binding_generation.pop(user, None)

    def new_pair(self, target):
        if not self.enabled:
            raise ScoreServiceError(UNCONFIGURED)
        self._prune()
        self._pairs = {key: value for key, value in self._pairs.items() if value.target != target}
        if len(self._pairs) >= 1024:
            raise ScoreServiceError("等待关联的人较多，请稍后重试。")
        ref = secrets.token_hex(6)
        self._pairs[ref] = _Pair(target, time.monotonic() + 600)
        return ref

    def offer(self, owner, ref):
        self._prune()
        ident = self.identity(owner)
        if not ident or ident["owner"] != owner:
            raise ScoreServiceError("请先在此私聊发送 /lxns，并在官网完成落雪授权。")
        pair = self._pairs.get(ref)
        if not pair or (pair.owner and pair.owner != owner):
            raise ScoreServiceError("群关联编号无效或已被使用，请回原群重新发送 /lxns。")
        pair.owner, pair.binding = owner, ident["binding"]
        return ident["name"]

    def confirm(self, target, name):
        self._prune()
        found = next(
            (
                (key, pair)
                for key, pair in self._pairs.items()
                if pair.target == target and pair.owner
            ),
            None,
        )
        if found:
            key, pair = found
            ident = self.identity(pair.owner)
            if ident and ident["binding"] == pair.binding and ident["name"] == name:
                with self.db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO subjects VALUES(?,?,0)", (target, pair.owner)
                    )
                del self._pairs[key]
                return
        raise ScoreServiceError("关联尚未完成或昵称不符，请按私聊中的说明回到原群确认。")

    async def access_token(self, user):
        async with self._refresh_lock:
            with self.db() as db:
                row = db.execute(
                    """SELECT g.* FROM grants g JOIN subjects s ON s.owner=g.owner
                                    WHERE s.user=?""",
                    (user,),
                ).fetchone()
            if not row:
                raise ScoreServiceError("请先发送 /lxns 完成落雪绑定。")
            if row["expires"] > time.time() + 30:
                return row["access"]
            access, refresh, expiry = await self._token(
                grant_type="refresh_token", refresh_token=row["refresh"]
            )
            with self.db() as db:
                changed = db.execute(
                    """UPDATE grants SET access=?,refresh=?,expires=?
                    WHERE owner=? AND binding=? AND refresh=?""",
                    (access, refresh, expiry, row["owner"], row["binding"], row["refresh"]),
                ).rowcount
                still_linked = db.execute(
                    "SELECT 1 FROM subjects WHERE user=? AND owner=?", (user, row["owner"])
                ).fetchone()
            if not changed or not still_linked:
                raise ScoreServiceError("落雪绑定已变更，请重新查询。")
            return access

    async def records(self, user, *, mode="scores"):
        if not self.identity(user):
            raise ScoreServiceError(MAIMAI_REBIND)
        if not self.enabled:
            raise ScoreServiceError(UNCONFIGURED)
        token = await self.access_token(user)
        try:
            profile, data, songs = await asyncio.gather(
                self._data(token, ""), self._data(token, "/" + mode), self.catalog.songs()
            )
            by_key = {(s.id, c.chart_type, c.difficulty): c for s in songs for c in s.charts}
            newest = self.catalog.version_name(max((s.version for s in songs), default=0))
            old, new = [], []
            pools = (
                [(old, data["standard"]), (new, data["dx"])] if mode == "bests" else [(None, data)]
            )
            for target, rows in pools:
                if not isinstance(rows, list):
                    raise TypeError("unexpected scores")
                for row in rows:
                    kind = row["type"]
                    if kind not in {"standard", "dx"} or row.get("achievements") is None:
                        continue
                    sid, diff = int(row["id"]), int(row["level_index"])
                    chart = by_key.get((sid, kind, diff))
                    if not chart:
                        raise ScoreServiceError("落雪曲库和玩家成绩版本不一致，请稍后重试。")
                    ach = float(row["achievements"])
                    ra = row.get("dx_rating")
                    record = {
                        "song_id": sid + (10000 if kind == "dx" else 0),
                        "title": row.get("song_name") or chart.title,
                        "level": chart.level,
                        "level_index": diff,
                        "level_label": chart.difficulty_name,
                        "type": chart.type_label,
                        "ds": chart.constant,
                        "achievements": ach,
                        "ra": int(ra) if ra is not None else rating_for(chart.constant, ach),
                        "rate": row.get("rate") or "",
                        "fc": row.get("fc") or "",
                        "fs": row.get("fs") or "",
                        "dxScore": int(row.get("dx_score") or 0),
                    }
                    if (count := chart_play_count(row)) is not None:
                        record["play_count"] = count
                    for field in ("play_time", "last_played_time"):
                        if row.get(field) is not None:
                            record[field] = row[field]
                    pool = (
                        target
                        if target is not None
                        else (new if self.catalog.version_name(chart.version) == newest else old)
                    )
                    pool.append(record)
            return PlayerRecords(str(profile["name"]), int(profile["rating"]), 0, "", old, new)
        except (SongCatalogError, KeyError, ValueError, TypeError):
            raise ScoreServiceError("落雪曲库或成绩格式暂时无法读取。") from None


def lxns_subject(context: MessageContext) -> str:
    if context.chat_scope == "c2c":
        return f"c2c:{context.user_id}"
    return f"{context.chat_scope}:{context.chat_id}:{context.user_id}"


def score_identity(provider, context: MessageContext):
    """Water binds an OpenID. LXNS additionally needs the verified group scope."""
    return context if isinstance(provider, ScoreSources) else context.user_id


class ScoreSources:
    def __init__(self, water: DivingFishService, lxns: LxnsScores):
        self.water, self.lxns = water, lxns

    @property
    def enabled(self):
        return self.water.enabled or self.lxns.enabled

    def source(self, context: MessageContext):
        return "落雪" if self.lxns.uses_maimai(lxns_subject(context)) else "水鱼"

    def history_key(self, context: MessageContext):
        user = lxns_subject(context)
        if self.lxns.uses_maimai(user):
            identity = self.lxns.identity(user)
            if not identity:
                raise ScoreServiceError(MAIMAI_REBIND)
            return f"lxns:{identity['binding']}:{user}"
        return self.water.history_key(context.user_id)

    async def start_binding(self, user):
        return await self.water.start_binding(user)

    async def player_records(self, context: MessageContext) -> PlayerRecords:
        if self.source(context) == "落雪":
            return await self.lxns.records(lxns_subject(context))
        return await self.water.player_records(context.user_id)

    async def best50(self, context: MessageContext) -> Best50:
        if self.source(context) == "落雪":
            return (await self.lxns.records(lxns_subject(context), mode="bests")).best50()
        return await self.water.best50(context.user_id)
