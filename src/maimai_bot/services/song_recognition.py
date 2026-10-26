from __future__ import annotations

import asyncio
import io
import logging
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

from .lxns_assets import LxnsAssetError, download_lxns_png
from .song_catalog import Song, SongCatalogService

logger = logging.getLogger(__name__)
MAX_BYTES = 12_000_000
MAX_PIXELS = 20_000_000


class RecognitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    song: Song | None
    candidates: tuple[Song, ...] = ()
    method: str = ""
    indexing: bool = False


def normalize_title(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def text_candidates(lines: list[str], songs: tuple[Song, ...]) -> list[tuple[float, Song]]:
    """Match titles, not arbitrary numbers (achievement/RA are not song IDs)."""
    normalized = [normalize_title(line) for line in lines if line.strip()]
    scored = []
    for song in songs:
        title = normalize_title(song.title)
        if not title:
            continue
        best = 0.0
        for line in normalized:
            if line == title:
                score = 1.0
            elif len(title) >= 4 and title in line:
                score = 0.97
            elif min(len(title), len(line)) >= 4:
                score = SequenceMatcher(None, title, line).ratio() * 0.94
            else:
                score = 0.0
            best = max(best, score)
        if best >= 0.72:
            scored.append((best, song))
    return sorted(scored, key=lambda item: (-item[0], item[1].id))


def attachment_url_allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        valid_port = parts.port in (None, 80, 443)
    except ValueError:
        return False
    return (
        parts.scheme in ("https", "http")
        and valid_port
        and parts.username is None
        and parts.password is None
        and any(host == domain or host.endswith("." + domain) for domain in (
            "qq.com", "qq.com.cn", "qpic.cn", "gtimg.com", "qlogo.cn",
        ))
    )


class SongRecognitionService:
    """CPU OCR plus geometrically verified SIFT jacket matching, with no LLM API.

    Only public jackets are cached. Uploaded screenshots stay in memory.
    The visual index is warmed in the background and reconstructed from JPEGs
    after restarts; no pickle/deserialization of executable objects is used.
    """

    def __init__(self, http: httpx.AsyncClient, catalog: SongCatalogService, cache_dir: Path):
        self._http = http
        self._catalog = catalog
        self._cache_dir = cache_dir
        self._lock = asyncio.Lock()
        self._ocr = None
        self._visual = None
        self._indexed_ids: frozenset[int] = frozenset()
        self._warm_task: asyncio.Task | None = None
        self._last_warm = 0.0
        self._indexing = False

    def start_warming(self) -> None:
        if self._warm_task is None or (
            self._warm_task.done() and time.monotonic() - self._last_warm > 3600
        ):
            self._warm_task = asyncio.create_task(self.warm(), name="song-recognition-index")

    async def close(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)

    async def warm(self) -> None:
        self._indexing = True
        self._last_warm = time.monotonic()
        try:
            songs = await self._catalog.songs()
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            # Four bounded workers and a small pause avoid hammering the asset CDN.
            queue = asyncio.Queue()
            for song in songs:
                if not (self._cache_dir / f"{song.id}.jpg").exists():
                    queue.put_nowait(song.id)

            async def worker():
                while not queue.empty():
                    song_id = queue.get_nowait()
                    try:
                        async with asyncio.timeout(18):
                            raw = await download_lxns_png(self._http, "jacket", song_id)
                        await asyncio.to_thread(self._save_jacket, song_id, raw)
                    except (LxnsAssetError, TimeoutError, OSError, ValueError):
                        pass  # Failures are not cached; retry on the next warm.
                    await asyncio.sleep(0.1)

            # Cached jackets can be used before the rest of the library downloads.
            async with self._lock:
                await asyncio.to_thread(self._build_visual)
            await asyncio.gather(*(worker() for _ in range(4)))
            async with self._lock:
                await asyncio.to_thread(self._build_visual)
            logger.info("识图曲绘库已就绪：%s/%s 首", len(self._indexed_ids), len(songs))
        except Exception:
            logger.exception("初始化识图曲绘库失败，文字识图仍可独立使用")
        finally:
            self._indexing = False

    def _save_jacket(self, song_id: int, raw: bytes) -> None:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.fit(image.convert("RGB"), (256, 256))
            path = self._cache_dir / f"{song_id}.jpg"
            temporary = path.with_suffix(".tmp")
            image.save(temporary, format="JPEG", quality=90)
            temporary.replace(path)

    async def _download(self, url: str) -> bytes:
        for _ in range(4):
            if not attachment_url_allowed(url):
                raise RecognitionError("图片地址不是受支持的 QQ 附件地址，请直接发送图片。")
            async with self._http.stream("GET", url, follow_redirects=False, timeout=20) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise RecognitionError("图片超过 12 MB，请压缩后重新发送。")
                return bytes(data)
        raise RecognitionError("图片地址重定向过多，请重新发送原图。")

    async def recognize(self, url: str) -> RecognitionResult:
        self.start_warming()
        try:
            raw = await self._download(url)
            songs = await self._catalog.songs()
            async with self._lock:
                return await asyncio.to_thread(self._recognize, raw, songs)
        except RecognitionError:
            raise
        except httpx.HTTPError as exc:
            logger.warning("识图附件下载失败：%s", type(exc).__name__)
            raise RecognitionError("图片下载失败或已过期，请重新发送原图。") from exc
        except Exception as exc:
            logger.exception("成绩识图失败")
            raise RecognitionError("图片识别暂时失败，请重试或使用 /song 曲名 查歌。") from exc

    def _recognize(self, raw: bytes, songs: tuple[Song, ...]) -> RecognitionResult:
        import cv2
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR

        try:
            with Image.open(io.BytesIO(raw)) as original:
                if original.width * original.height > MAX_PIXELS:
                    raise RecognitionError("图片像素过大，请裁剪为单张成绩图再发送。")
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((1800, 1800))
                pixels = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise RecognitionError("这不是可读取的图片，请发送 JPG、PNG 或 WebP 原图。") from exc
        if self._ocr is None:
            self._ocr = RapidOCR()
        result, _ = self._ocr(pixels)
        lines = [str(row[1]) for row in (result or []) if float(row[2]) >= 0.45]
        ranked = text_candidates(lines, songs)
        confident_text = (
            ranked and ranked[0][0] >= 0.90
            and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.06)
        )
        visual_id = self._match_visual(pixels)
        visual_song = next((song for song in songs if song.id == visual_id), None)
        if confident_text:
            if visual_song and visual_song.id != ranked[0][1].id:
                return RecognitionResult(None, (ranked[0][1], visual_song), "文字与曲绘不一致")
            return RecognitionResult(ranked[0][1], method="曲名与曲绘" if visual_song else "曲名")
        if visual_song:
            return RecognitionResult(visual_song, method="曲绘")
        return RecognitionResult(
            None, tuple(song for _, song in ranked[:5]), indexing=self._indexing
        )

    def _build_visual(self) -> None:
        import cv2
        import numpy as np

        cv2.setNumThreads(2)
        sift = cv2.SIFT_create(nfeatures=180)
        descriptors, points, owners = [], [], []
        indexed = set()
        for path in sorted(self._cache_dir.glob("*.jpg")):
            if not path.stem.isdecimal():
                continue
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            keys, desc = sift.detectAndCompute(image, None)
            if desc is None or len(keys) < 8:
                continue
            descriptors.append(desc)
            points.extend(key.pt for key in keys)
            owners.extend([int(path.stem)] * len(keys))
            indexed.add(int(path.stem))
        if not descriptors:
            return
        matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 96})
        matcher.add([np.concatenate(descriptors)])
        matcher.train()
        self._visual = (matcher, np.array(points, dtype=np.float32), np.array(owners))
        self._indexed_ids = frozenset(indexed)

    def _match_visual(self, pixels) -> int | None:
        import cv2
        import numpy as np

        if self._visual is None:
            return None
        gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
        keys, desc = cv2.SIFT_create(nfeatures=3500).detectAndCompute(gray, None)
        if desc is None or len(desc) < 8:
            return None
        matcher, points, owners = self._visual
        votes: dict[int, list] = {}
        for pair in matcher.knnMatch(desc, k=2):
            if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.70:
                match = pair[0]
                votes.setdefault(int(owners[match.trainIdx]), []).append(match)
        verified = []
        for song_id, matches in sorted(votes.items(), key=lambda item: -len(item[1]))[:6]:
            # Distinct reference points prevent repeated wallpaper features from voting twice.
            unique = {match.trainIdx: match for match in matches}
            if len(unique) < 8:
                continue
            matches = list(unique.values())
            source = np.float32([points[m.trainIdx] for m in matches])
            target = np.float32([keys[m.queryIdx].pt for m in matches])
            matrix, mask = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
            if matrix is None or mask is None:
                continue
            inliers = int(mask.sum())
            hull = cv2.convexHull(source[mask.ravel().astype(bool)])
            coverage = cv2.contourArea(hull) / (256 * 256)
            corners = cv2.perspectiveTransform(
                np.float32([[[0, 0], [256, 0], [256, 256], [0, 256]]]), matrix
            )[0]
            area = abs(cv2.contourArea(corners))
            if (
                inliers >= 8 and inliers / len(matches) >= 0.55 and coverage >= 0.08
                and cv2.isContourConvex(corners) and 900 <= area <= gray.size * 1.2
            ):
                verified.append((inliers, song_id))
        verified.sort(reverse=True)
        if verified and (len(verified) == 1 or verified[0][0] >= verified[1][0] * 1.5):
            return verified[0][1]
        return None
