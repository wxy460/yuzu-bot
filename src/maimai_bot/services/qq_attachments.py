"""Read bounded attachments from QQ's own media servers."""

from urllib.parse import urlsplit

import httpx

MAX_ATTACHMENT_BYTES = 12_000_000
QQ_MEDIA_DOMAINS = ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.com", "qlogo.cn")


def attachment_url_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (
            parts.scheme in ("https", "http")
            and parts.port in (None, 80, 443)
            and parts.username is None
            and parts.password is None
            and any(host == domain or host.endswith("." + domain) for domain in QQ_MEDIA_DOMAINS)
        )
    except ValueError:
        return False


async def download_qq_attachment(http, url: str, *, label: str, limit: int) -> bytes:
    for _ in range(4):
        if not attachment_url_allowed(url):
            raise ValueError(f"{label}地址不是受支持的 QQ 附件地址，请直接发送文件。")
        try:
            async with http.stream("GET", url, follow_redirects=False, timeout=20) as response:
                if response.has_redirect_location:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length", "")
                if length.isdecimal() and int(length) > limit:
                    raise ValueError(f"{label}超过 12 MB。")
                data = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    if len(chunk) > limit - len(data):
                        raise ValueError(f"{label}超过 12 MB。")
                    data.extend(chunk)
                return bytes(data)
        except httpx.InvalidURL as exc:
            raise ValueError(f"{label}地址不是有效的 QQ 附件地址。") from exc
    raise ValueError(f"{label}地址重定向过多，请重新发送文件。")


async def download_qq_json(http: httpx.AsyncClient, url: str) -> bytes:
    return await download_qq_attachment(http, url, label="JSON 文件", limit=MAX_ATTACHMENT_BYTES)
