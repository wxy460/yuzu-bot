"""Adapt the read-only LXNS CHUNITHM catalogue and player records for the bot."""
from __future__ import annotations

import asyncio

from .divingfish import ScoreServiceError
from .lxns_scores import BASE, UNCONFIGURED, lxns_subject

SONGS = BASE + "/api/v0/chunithm/song/list"
DIFFICULTIES = ("Basic", "Advanced", "Expert", "Master", "Ultima", "World's End")


def catalog_songs(payload):
    versions = {v["version"]: v["title"] for v in payload["versions"]}
    songs = []
    for item in payload["songs"]:
        mid = int(item["id"])
        constants, levels, cids, chart_versions = ([None] * 6 for _ in range(4))
        for chart in item["difficulties"]:
            diff = int(chart["difficulty"])
            if 0 <= diff < len(DIFFICULTIES):
                # LXNS identifies a chart by (song ID, difficulty). Keep this key
                # distinct from Diving-Fish cids, including separate World's End IDs.
                cids[diff] = (mid, diff)
                levels[diff] = chart["level"]
                constants[diff] = float(chart["level_value"])
                chart_versions[diff] = versions.get(chart.get("version", item["version"]), "")
        songs.append({
            "id": mid, "title": item["title"], "cids": cids, "level": levels, "ds": constants,
            "chart_versions": chart_versions,
            "basic_info": {"from": versions.get(item["version"], ""), "artist": item["artist"],
                           "genre": item["genre"], "bpm": item["bpm"]},
        })
    return songs


def scores(rows, by_id):
    mapped = []
    for row in rows:
        mid, diff = int(row["id"]), int(row["level_index"])
        song = by_id.get(mid)
        if not 0 <= diff < len(DIFFICULTIES):
            raise ScoreServiceError("落雪返回了无法识别的中二难度，请稍后重试。")
        # A player's history can outlive a removed chart. The score API supplies
        # its own title, level and rating even when no current constant is known.
        mapped.append({
            "cid": (mid, diff), "mid": mid, "title": row.get("song_name") or (song["title"] if song else f"z{mid}"),
            "level": row.get("level") or (song["level"][diff] if song else "？"), "level_label": DIFFICULTIES[diff],
            "ds": song["ds"][diff] if song else None, "score": int(row["score"]), "ra": float(row["rating"]),
            "fc": row.get("full_combo") or "", "clear": row.get("clear") or "",
            "info": {**song["basic_info"], "from": song["chart_versions"][diff]} if song else {},
        })
    return mapped


async def read_scores(lxns, context, song_list, *, best=False):
    if not lxns.enabled:
        raise ScoreServiceError(UNCONFIGURED)
    user = lxns_subject(context)
    identity = lxns.identity(user)
    if not identity:
        raise ScoreServiceError("你的中二数据源为落雪。请发送 /lxns 完成绑定，或用 /中二数据源 水鱼 切换。")
    token = await lxns.access_token(user)
    if best:
        # The official frontend's useBests hook uses this personal endpoint for
        # both games (src/hooks/queries/queryKeys.ts in maimai-prober-frontend).
        songs, data, profile = await asyncio.gather(
            song_list(), lxns._data(token, "/bests", game="chunithm"),
            lxns._data(token, "", game="chunithm"),
        )
    else:
        songs, data = await asyncio.gather(song_list(), lxns._data(token, "/scores", game="chunithm"))
    current = lxns.identity(user)
    if not current or current["binding"] != identity["binding"] or not lxns.uses_chuni(user):
        raise ScoreServiceError("本次查询期间落雪中二授权或数据源发生变化，请重新查询。")
    by_id = {s["id"]: s for s in songs}
    if best:
        records = {"b30": scores(data["bests"], by_id)}
        if data.get("new_bests") is not None:
            records["n20"] = scores(data["new_bests"], by_id)
        return {"nickname": profile["name"], "rating": float(profile["rating"]), "records": records}
    return scores(data, by_id)
