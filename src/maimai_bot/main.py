from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import httpx

from .config import ConfigError, Settings
from .cross_game import CrossGameService
from .features import build_router
from .group_life import GroupLife
from .lxns_commands import start_callback
from .qq_adapter import QQOfficialAdapter
from .services.b50_image import B50ImageService
from .services.bilibili import BilibiliChartService
from .services.chart_analysis import ChartAnalysisService
from .services.chat import ChatService
from .services.community import CommunityData
from .services.cosmetics import CosmeticService
from .services.divingfish import DivingFishService
from .services.lxns_scores import LxnsScores, ScoreSources
from .services.song_catalog import SongCatalogService
from .services.song_recognition import SongRecognitionService
from .services.web_search import WebSearchService


async def run() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "启动舞萌 QQ Bot（对话=%s，查分=%s）",
        "已启用" if settings.chat_enabled else "未配置",
        "已启用" if settings.scores_enabled else "未配置",
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    project_root = Path(__file__).resolve().parents[2]
    cosmetics_path = (project_root / "data" / "cosmetics.json").resolve()
    private_dir = cosmetics_path.parent
    async with httpx.AsyncClient(follow_redirects=True) as http:
        web_search = WebSearchService(http)
        chat = ChatService(
            http,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            system_prompt=settings.chat_system_prompt,
            history_turns=settings.chat_history_turns,
            memory_path=(project_root / "data" / "chat_memory.json").resolve(),
            web_search=web_search,
        )
        water = DivingFishService(
            http,
            client_id=settings.divingfish_client_id,
            client_secret=settings.divingfish_client_secret,
            binding_state=private_dir / "water-bindings.sqlite3",
        )
        b50_images = B50ImageService(
            http,
            background_path=project_root / "assets" / "b50-background.png",
        )
        cosmetics = CosmeticService(http, state_path=cosmetics_path)
        catalog = SongCatalogService(http)
        lxns = LxnsScores(
            http, catalog, private_dir / "lxns.sqlite3",
            client_id=settings.lxns_client_id, client_secret=settings.lxns_client_secret,
            redirect_uri=settings.lxns_redirect_uri,
        )
        scores = ScoreSources(water, lxns)
        recognition = SongRecognitionService(
            http, catalog,
            cache_dir=private_dir / "song_recognition",
        )
        recognition.start_warming()
        chart_analysis = ChartAnalysisService(http)
        bilibili = BilibiliChartService(
            http, index_path=project_root / "data" / "bilibili_chart_videos.json"
        )
        cross_games = CrossGameService(
            http, client_id=settings.divingfish_client_id, client_secret=settings.divingfish_client_secret,
            lxns=lxns, binding_state=lxns.path.with_name("chuni-water-bindings.sqlite3"),
        )
        community = CommunityData(http, private_dir / "community.sqlite3")
        group_life = GroupLife(community.database.with_name("group-life.sqlite3"), chat)
        router = build_router(
            chat, scores, b50_images, cosmetics, catalog, chart_analysis, bilibili, recognition,
            community, cross_games, group_life,
        )
        adapter = QQOfficialAdapter(
            app_id=settings.qq_app_id,
            app_secret=settings.qq_app_secret,
            http=http,
            handler=router.dispatch,
            panel_commands=router.commands,
            unmentioned_group_filter=router.recognizes_command,
            unmentioned_context_filter=router.accepts_context,
            ambient_handler=group_life.respond,
        )
        callback = await start_callback(lxns, settings.lxns_callback_port)
        try:
            await adapter.run(stop_event)
        finally:
            if callback is not None:
                await callback.cleanup()
            await recognition.close()
            cross_games.grant.close()
            water.close()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        raise SystemExit(f"配置错误：{exc}") from exc


if __name__ == "__main__":
    main()
