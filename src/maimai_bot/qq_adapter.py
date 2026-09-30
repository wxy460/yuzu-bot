from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

from .domain import BotReply, MessageContext, QuickAction
from .qq_command_panel import sync_group_command_panel
from .qq_shortcuts import QQShortcutKeyboard
from .router import CommandInfo

logger = logging.getLogger(__name__)
MessageHandler = Callable[[MessageContext], Awaitable[str | BotReply]]
MessageFilter = Callable[[str], bool]

_FULL_GROUP_MESSAGE_EVENT = "GROUP_MESSAGE_CREATE"


class QQOfficialAdapter:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        http: httpx.AsyncClient,
        handler: MessageHandler,
        panel_commands: tuple[CommandInfo, ...] = (),
        unmentioned_group_filter: MessageFilter | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._http = http
        self._handler = handler
        self._panel_commands = panel_commands
        self._unmentioned_group_filter = unmentioned_group_filter
        self._ws = None

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            from qqbot_agent_sdk import (
                MEDIA_TYPE_IMAGE,
                EventParser,
                MediaInfo,
                MediaUploader,
                MessageToCreate,
                QQApiClient,
                QQMessageType,
                QQWebSocket,
                WSCallbacks,
            )
            from qqbot_agent_sdk import websocket as qq_websocket_module
        except ImportError as exc:
            raise RuntimeError("缺少 qqbot-agent-sdk，请先安装项目依赖") from exc

        api = QQApiClient(app_id=self._app_id, client_secret=self._app_secret)
        api.setup(self._http)
        media_uploader = MediaUploader(api_client=api, http_client=self._http, log_tag="MaimaiBot")
        parser = EventParser()

        # qqbot-agent-sdk 1.2.x only whitelists GROUP_AT_MESSAGE_CREATE even
        # though QQ's official "receive all group messages" switch emits
        # GROUP_MESSAGE_CREATE. Extend the in-memory whitelist until upstream
        # exposes the event itself. No installed package files are modified.
        if _FULL_GROUP_MESSAGE_EVENT not in qq_websocket_module.MESSAGE_EVENT_TYPES:
            qq_websocket_module.MESSAGE_EVENT_TYPES = frozenset(
                (*qq_websocket_module.MESSAGE_EVENT_TYPES, _FULL_GROUP_MESSAGE_EVENT)
            )
            logger.info("已启用 QQ 群全量消息事件兼容：%s", _FULL_GROUP_MESSAGE_EVENT)
        session_id: str | None = None
        sequence: int | None = None

        def get_session() -> tuple[str | None, int | None]:
            return session_id, sequence

        def set_session(new_session_id: str | None, new_sequence: int | None) -> None:
            nonlocal session_id, sequence
            session_id, sequence = new_session_id, new_sequence

        async def on_message(event_type: str, raw: dict) -> None:
            parse_type = (
                "GROUP_AT_MESSAGE_CREATE"
                if event_type == _FULL_GROUP_MESSAGE_EVENT
                else event_type
            )
            event = parser.parse(parse_type, raw)
            if event is None:
                logger.warning("无法解析 QQ 消息事件：type=%s", event_type)
                return
            if (
                event_type == _FULL_GROUP_MESSAGE_EVENT
                and self._unmentioned_group_filter is not None
                and not self._unmentioned_group_filter(event.content)
            ):
                logger.debug("忽略未命中指令的 QQ 群全量消息")
                return
            logger.info(
                "收到 QQ 消息：type=%s scope=%s command=%s",
                event_type,
                event.chat_scope,
                _command_label(event.content),
            )
            context = MessageContext(
                user_id=event.user_id,
                chat_id=event.chat_id,
                chat_scope=event.chat_scope,
                content=event.content or ("/help" if event.chat_scope == "group" else ""),
                message_id=event.message_id,
                avatar_url=_qq_avatar_url(raw)
                or _qq_openid_avatar_url(self._app_id, event.user_id),
            )
            try:
                answer = await self._handler(context)
                if isinstance(answer, BotReply) and answer.image_png:
                    if event.chat_scope not in ("c2c", "group"):
                        await api.send_text(
                            event.chat_scope,
                            event.chat_id,
                            answer.text or "当前会话暂不支持图片上传。",
                            reply_to=event.message_id,
                            markdown=False,
                        )
                    else:
                        await _send_png(
                            api,
                            media_uploader,
                            event.chat_scope,
                            event.chat_id,
                            event.message_id,
                            answer.image_png,
                            MEDIA_TYPE_IMAGE,
                            MediaInfo,
                            MessageToCreate,
                            QQMessageType,
                        )
                        if answer.followup_text:
                            await _send_text_reply(
                                api,
                                event.chat_scope,
                                event.chat_id,
                                event.message_id,
                                answer.followup_text,
                                answer.quick_actions,
                            )
                else:
                    text = answer.text if isinstance(answer, BotReply) else answer
                    quick_actions = answer.quick_actions if isinstance(answer, BotReply) else ()
                    if quick_actions:
                        await _send_text_reply(
                            api,
                            event.chat_scope,
                            event.chat_id,
                            event.message_id,
                            text,
                            quick_actions,
                        )
                    else:
                        await api.send_text(
                            event.chat_scope,
                            event.chat_id,
                            text,
                            reply_to=event.message_id,
                            markdown=False,
                        )
            except Exception:
                logger.exception("处理 QQ 消息失败")
                try:
                    await api.send_text(
                        event.chat_scope,
                        event.chat_id,
                        "处理消息时发生错误，请稍后重试。",
                        reply_to=event.message_id,
                        markdown=False,
                    )
                except Exception:
                    logger.exception("发送错误提示失败")

        callbacks = WSCallbacks(
            on_message_event=on_message,
            on_connected=lambda: logger.info("已连接 QQ 官方网关"),
            on_disconnected=lambda: logger.warning("QQ 官方网关连接已断开，SDK 将尝试重连"),
            on_fatal_error=lambda code, message: logger.error(
                "QQ 官方网关发生致命错误 [%s]：%s", code, message
            ),
            get_token=api.ensure_token_sync,
            get_session=get_session,
            set_session=set_session,
            set_heartbeat_interval=lambda seconds: logger.debug("QQ 心跳间隔：%ss", seconds),
            get_gateway_url=api.get_gateway_url_sync,
            clear_token=api.clear_token,
            fail_pending=lambda reason: logger.warning("QQ 待发送请求失败：%s", reason),
        )
        self._ws = QQWebSocket(callbacks=callbacks, log_tag="MaimaiBot")
        await api.ensure_token()
        if self._panel_commands:
            for attempt in range(1, 4):
                try:
                    await sync_group_command_panel(api, self._panel_commands)
                    break
                except Exception:
                    # A panel configuration problem must not prevent message handling.
                    if attempt == 3:
                        logger.exception("同步 QQ 群指令面板失败，机器人将继续启动")
                        break
                    logger.warning("同步 QQ 群指令面板失败，将进行第 %s 次重试", attempt + 1)
                    await asyncio.sleep(attempt * 2)
        gateway_url = await api.get_gateway_url()
        self._ws.start(gateway_url, asyncio.get_running_loop())
        try:
            await stop_event.wait()
        finally:
            await self._ws.async_stop()
            self._ws = None


def _command_label(content: str) -> str:
    """Return a log-safe command label without recording message arguments."""
    text = content.strip().lstrip("/")
    if not text:
        return "<empty>"
    head = re.split(r"\s+", text, maxsplit=1)[0]
    return head[:32]


def _qq_avatar_url(raw: dict) -> str | None:
    """Read the avatar URL carried by an official QQ message event."""
    author = raw.get("author")
    value = author.get("avatar") if isinstance(author, dict) else None
    if not value:
        member = raw.get("member")
        user = member.get("user") if isinstance(member, dict) else None
        value = user.get("avatar") if isinstance(user, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith("https://") else None


def _qq_openid_avatar_url(app_id: str, user_openid: str) -> str | None:
    """Build Tencent's QQ avatar CDN URL for official-bot OpenIDs."""
    if not app_id.strip() or not user_openid.strip():
        return None
    safe_app_id = quote(app_id.strip(), safe="")
    safe_openid = quote(user_openid.strip(), safe="")
    return f"https://thirdqq.qlogo.cn/qqapp/{safe_app_id}/{safe_openid}/640"


async def _send_png(
    api: object,
    uploader: object,
    chat_scope: str,
    chat_id: str,
    message_id: str,
    png: bytes,
    media_type_image: int,
    media_info_type: type,
    message_type: type,
    qq_message_type: type,
) -> None:
    path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="maimai-b50-", suffix=".png", delete=False) as file:
            file.write(png)
            path = file.name
        file_info = await uploader.upload(
            chat_scope,
            chat_id,
            path,
            media_type_image,
            file_name="maimai-b50.png",
        )
        message = message_type(
            msg_type=qq_message_type.RICH_MEDIA,
            msg_id=message_id,
            msg_seq=api.next_msg_seq(),
            media=media_info_type(file_info=file_info),
        )
        if chat_scope == "c2c":
            await api.post_c2c_message(chat_id, message)
        else:
            await api.post_group_message(chat_id, message)
    finally:
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


async def _send_text_reply(
    api: object,
    chat_scope: str,
    chat_id: str,
    message_id: str,
    text: str,
    quick_actions: tuple[QuickAction, ...],
) -> None:
    """Send a passive text reply with an official QQ command keyboard.

    Custom keyboards are a gated QQ capability. If the application has not
    been granted it yet, keep /help usable by retrying the same passive reply
    without the keyboard.
    """
    message = api.build_text_body(
        text,
        reply_to=message_id,
        markdown=True,
    )
    keyboard = QQShortcutKeyboard(quick_actions)
    try:
        if chat_scope == "c2c":
            await api.post_c2c_message(chat_id, message, keyboard=keyboard)
        elif chat_scope == "group":
            await api.post_group_message(chat_id, message, keyboard=keyboard)
        else:
            await api.send_text(
                chat_scope,
                chat_id,
                text,
                reply_to=message_id,
                markdown=False,
            )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.warning("QQ 官方快捷按钮发送失败，回退为普通帮助消息：%s", exc)
        await api.send_text(
            chat_scope,
            chat_id,
            text,
            reply_to=message_id,
            markdown=False,
        )
