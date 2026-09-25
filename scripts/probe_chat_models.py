from __future__ import annotations

import asyncio
import time

import httpx

from maimai_bot.config import Settings

CANDIDATES = (
    "deepseek-v4-flash-ascend",
    "deepseek-v4-flash-ascend1",
    "deepseek-flash-2",
    "deepseek-flash",
    "qwen3.8-chat",
    "qwen3.5-non-thinking",
    "claude-haiku-4-5",
    "smart/default",
)


async def probe(http: httpx.AsyncClient, settings: Settings, model: str) -> None:
    started = time.monotonic()
    try:
        response = await http.post(
            f"{settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "只回复 OK"}],
                "temperature": 0,
                "max_tokens": 8,
            },
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        )
        elapsed = time.monotonic() - started
        if response.is_success:
            answer = response.json()["choices"][0]["message"]["content"].strip()
            print(f"OK\t{elapsed:.1f}s\t{model}\t{answer[:40]}")
        else:
            print(f"HTTP {response.status_code}\t{elapsed:.1f}s\t{model}")
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        elapsed = time.monotonic() - started
        print(f"{type(exc).__name__}\t{elapsed:.1f}s\t{model}")


async def main() -> None:
    settings = Settings.from_env()
    async with httpx.AsyncClient(follow_redirects=True) as http:
        await asyncio.gather(*(probe(http, settings, model) for model in CANDIDATES))


if __name__ == "__main__":
    asyncio.run(main())
