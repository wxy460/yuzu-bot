#!/usr/bin/env python3
"""Build the bot's offline chart-video index from reviewed open metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SOURCE_URL = (
    "https://raw.githubusercontent.com/Nick-bit233/mai-gen-videob50/"
    "main/video_metadata/asset-manifest.json"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "bilibili_chart_videos.json"


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "maimai-bot-index-updater/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f"metadata download failed: HTTP {response.status}")
        return response.read()


def _canonical_url(value: str, bvid: str, pid: object) -> str:
    fallback = f"https://www.bilibili.com/video/{bvid}"
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"www.bilibili.com", "bilibili.com"}
        or parsed.path.rstrip("/") != f"/video/{bvid}"
    ):
        return fallback
    page = parse_qs(parsed.query).get("p", [str(pid or "")])[0]
    return f"{fallback}?p={int(page)}" if page.isdecimal() and int(page) > 0 else fallback


def build_index(payload: dict[str, object]) -> dict[str, object]:
    assets = payload.get("assets")
    if payload.get("game_type") != "maimai" or not isinstance(assets, list):
        raise ValueError("not a maimai asset manifest")
    videos: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    for item in assets:
        if not isinstance(item, dict):
            continue
        if (
            item.get("source_type") not in {"bilibili", "manual"}
            or item.get("review_status") != "reviewed"
            or item.get("difficulty") not in {3, 4}
            or item.get("chart_type") not in {"standard", "dx"}
        ):
            continue
        bvid = str(item.get("source_id") or "")
        song_title = str(item.get("song_title") or "").strip()
        chart_type = str(item["chart_type"])
        difficulty = int(item["difficulty"])
        title = str(item.get("source_title") or item.get("source_page_title") or "").strip()
        if not re.fullmatch(r"BV[0-9A-Za-z]{10,}", bvid) or not song_title or not title:
            continue
        key = (song_title.casefold(), chart_type, difficulty)
        if key in seen:
            continue
        seen.add(key)
        videos.append(
            {
                "song_title": song_title,
                "artist": str(item.get("artist") or "").strip(),
                "chart_type": chart_type,
                "difficulty": difficulty,
                "bvid": bvid,
                "url": _canonical_url(
                    str(item.get("source_url") or ""), bvid, item.get("source_pid")
                ),
                "title": title,
                "source": "mai-gen-videob50 审核谱面库",
            }
        )
    if len(videos) < 1000:
        raise ValueError(f"refusing incomplete index with only {len(videos)} records")
    videos.sort(
        key=lambda row: (
            str(row["song_title"]).casefold(),
            str(row["chart_type"]),
            int(row["difficulty"]),
        )
    )
    return {
        "schema_version": 2,
        "source": "Nick-bit233/mai-gen-videob50",
        "source_url": SOURCE_URL,
        "upstream_generated_at": payload.get("generated_at"),
        "upstream_version": payload.get("metadata_store_version"),
        "videos": videos,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="use an already downloaded upstream manifest")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    raw = args.input.read_bytes() if args.input else _download(SOURCE_URL)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("upstream manifest must be a JSON object")
    index = build_index(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"wrote {len(index['videos'])} reviewed chart links to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
