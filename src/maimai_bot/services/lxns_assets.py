from __future__ import annotations

import io
import re

import httpx
from PIL import Image, UnidentifiedImageError

LXNS_ASSET_BASE = "https://assets2.lxns.net/maimai"


class LxnsAssetError(RuntimeError):
    pass


async def download_lxns_png(
    http: httpx.AsyncClient,
    kind: str,
    item_id: int,
) -> bytes:
    """Download a public LXNS asset, handling its numeric JS cookie challenge.

    Only integer literals are parsed from the challenge. No remote JavaScript is
    executed inside the bot process.
    """
    url = f"{LXNS_ASSET_BASE}/{kind}/{item_id}.png"
    try:
        response = await http.get(url, timeout=30)
        response.raise_for_status()
        if _is_cookie_challenge(response):
            cookies = _challenge_cookie_header(response.text)
            response = await http.get(url, headers={"Cookie": cookies}, timeout=30)
            response.raise_for_status()
        if len(response.content) > 10_000_000:
            raise LxnsAssetError("素材文件超过 10 MB 限制")
        return _normalize_png(response.content)
    except httpx.HTTPError as exc:
        raise LxnsAssetError(f"下载素材失败：{exc}") from exc


def _is_cookie_challenge(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "text/html" in content_type and "EO_Bot_Ssid" in response.text


def _challenge_cookie_header(script: str) -> str:
    session_match = re.search(r"EO_Bot_Ssid=.*?(\d{6,})", script, re.DOTALL)
    values_match = re.search(r"var e=\{(.*?)\},t=0", script, re.DOTALL)
    if session_match is None or values_match is None:
        raise LxnsAssetError("无法识别素材 CDN 的访问验证")
    status_values = [
        int(value)
        for value in re.findall(r":[A-Za-z_$][\w$]*|:\s*(\d+)", values_match.group(1))
        if value
    ]
    if not status_values:
        raise LxnsAssetError("素材 CDN 验证缺少状态参数")
    status = sum(status_values)
    session = session_match.group(1)
    return f"__tst_status={status}#; EO_Bot_Ssid={session}"


def _normalize_png(content: bytes) -> bytes:
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise LxnsAssetError("素材响应不是有效图片") from exc
    if image.format == "PNG":
        return content
    output = io.BytesIO()
    image.convert("RGBA").save(output, format="PNG")
    return output.getvalue()
