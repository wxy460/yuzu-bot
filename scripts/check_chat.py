from __future__ import annotations

import argparse
import asyncio
import logging
import time

import httpx

from maimai_bot.config import Settings
from maimai_bot.services.chat import ChatService
from maimai_bot.services.web_search import WebSearchService


async def main(prompt: str, context: str, model: str | None) -> None:
    settings = Settings.from_env()
    started = time.monotonic()
    async with httpx.AsyncClient(follow_redirects=True) as http:
        web_search = WebSearchService(http)
        chat = ChatService(
            http,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=model or settings.llm_model,
            system_prompt=settings.chat_system_prompt,
            web_search=web_search,
        )
        answer = await chat.reply("diagnostic", prompt, user_context=context)
    elapsed = time.monotonic() - started
    print(f"模型：{chat.effective_model}")
    print(f"耗时：{elapsed:.1f} 秒")
    print(f"响应：{answer}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default="你好啊")
    parser.add_argument("--context", default="")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(args.prompt, args.context, args.model))
