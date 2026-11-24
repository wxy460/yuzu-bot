"""Retrieve the game's named progression plates from a pinned public artwork catalogue."""
from __future__ import annotations

import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from PIL import Image

REVISION = "0ebcd319585af286bce5c31cc416fcf245e28428"
BASE = f"https://raw.githubusercontent.com/basilomorireal/maimai-asset-browser/{REVISION}"
DESTINATION = Path(__file__).resolve().parents[1] / "assets" / "plates"
PLATE = re.compile(r"[真超檄橙暁桃櫻紫菫白雪輝舞熊華爽煌宙星祭祝双宴鏡彩](?:舞舞|極|神|将)|覇者")


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, follow_redirects=True) as http:
        response = http.get(f"{BASE}/manifest.json")
        response.raise_for_status()
        catalogue = [
            (item["name"], int(item["id"]))
            for item in response.json()["assets"]
            if item["category"] == "plate" and PLATE.fullmatch(item["name"])
        ]
        if len(catalogue) != 100 or len(dict(catalogue)) != 100 or len({n for _, n in catalogue}) != 100:
            raise ValueError("Unexpected plate catalogue at the pinned revision")
        catalogue.insert(0, ("デフォルト", 1))  # National catalogue's starter plate.

        with TemporaryDirectory(dir=DESTINATION) as temporary:
            staging = Path(temporary)

            def retrieve(entry):
                name, number = entry
                # Use the national starter plate and preserve the existing Orange Shogun artwork.
                url = (f"https://assets2.lxns.net/maimai/plate/{number}.png" if number in (1, 6113)
                       else f"{BASE}/assets/plates/UI_Plate_{number:06d}.png")
                res = http.get(url)
                res.raise_for_status()
                with Image.open(io.BytesIO(res.content)) as img:
                    if img.format != "PNG" or img.size != (720, 116):
                        raise ValueError(f"Invalid plate artwork for {name} ({number})")
                    img.verify()
                (staging / f"{number}.png").write_bytes(res.content)

            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(retrieve, catalogue))
            for _, number in catalogue:
                path = staging / f"{number}.png"
                path.replace(DESTINATION / path.name)
            index = staging / "index.json"
            index.write_text(json.dumps(dict(catalogue), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            index.replace(DESTINATION / index.name)
    print(f"Verified and installed {len(catalogue)} nameplates.")


if __name__ == "__main__":
    main()
