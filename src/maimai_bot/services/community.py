"""Public reference data and local rating history."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

CN = ZoneInfo("Asia/Shanghai")
SOURCES = {
    "areas": "https://raw.githubusercontent.com/TrueRou/maimai.py/HEAD/maimai_py/providers/local/areas_zh.json",
    "map_ids": "https://mai-find.chongxi.us/data/types/map.json",
    "map_rewards": "https://raw.githubusercontent.com/Flora-Software/maimai-resourse/HEAD/maps_data.json",
    "courses": "https://raw.githubusercontent.com/Flora-Software/maimai-resourse/HEAD/origindata/Courses.json",
    "area_icons": "https://mai-find.chongxi.us/data/types/icon.json",
    "area_plates": "https://mai-find.chongxi.us/data/types/plate.json",
    "area_frames": "https://mai-find.chongxi.us/data/types/frame.json",
    "area_titles": "https://mai-find.chongxi.us/data/types/title.json",
    "titles": "https://maimai.lxns.net/api/v0/maimai/trophy/list",
    "status": "https://mai.chongxi.us/api/status?slim=1",
    "mai_jp": "https://otoge-db.net/maimai/data/music-ex.json",
}


class PublicDataError(ValueError):
    pass


class CommunityData:
    def __init__(self, http: httpx.AsyncClient, database: Path):
        self.http = http
        self.database = database
        self.cache = {}
        self.locks = {key: asyncio.Lock() for key in SOURCES}

    async def fetch(self, key):
        async with self.locks[key]:
            updated, data = self.cache.get(key, (0, None))
            ttl = 60 if key == "status" else 21600
            if data is not None and time.monotonic() - updated < ttl:
                return data
            try:
                r = await self.http.get(SOURCES[key], timeout=12)
                r.raise_for_status()
                payload = r.json()
                if key == "titles":
                    payload = payload["trophies"]
                if not isinstance(payload, dict if key == "status" else list):
                    raise TypeError("invalid response")
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                if data is not None and key != "status":
                    return data
                raise PublicDataError("暂时读取不到公开数据，请稍后再试。") from exc
            self.cache[key] = (time.monotonic(), payload)
            return payload

    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=3)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS trends (
                subject TEXT, day TEXT, rating INTEGER, b35 INTEGER, b15 INTEGER,
                PRIMARY KEY(subject, day));
        """)
        return db

    def trend(self, user, best):
        def floor(records, size):
            return min(int(x.get("ra", 0)) for x in records) if len(records) == size else 0

        return self.snapshot(user, best.rating, floor(best.old, 35), floor(best.new, 15))

    def snapshot(self, key, rating, old_floor, new_floor):
        subject = hashlib.sha256(key.encode()).hexdigest()
        date = datetime.now(CN).date().isoformat()
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO trends VALUES(?,?,?,?,?)",
                (subject, date, rating, old_floor, new_floor),
            )
            rows = db.execute(
                "SELECT day,rating,b35,b15 FROM trends WHERE subject=? ORDER BY day DESC LIMIT 30",
                (subject,),
            ).fetchall()
        return rows[::-1]
