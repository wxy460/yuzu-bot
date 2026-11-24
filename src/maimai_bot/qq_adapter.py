from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from .domain import BotReply, MessageContext, QuickAction
from .qq_command_panel import sync_group_command_panel
from .qq_shortcuts import QQShortcutKeyboard
from .router import CommandInfo

logger = logging.getLogger(__name__)
MessageHandler = Callable[[MessageContext], Awaitable[str | BotReply]]
AmbientHandler = Callable[[MessageContext], Awaitable[str | None]]
MessageFilter = Callable[[str], bool]

_FULL_GROUP_MESSAGE_EVENT = "GROUP_MESSAGE_CREATE"
_MENTION = re.compile(r"<@!?([^<>\s]+)>")


@dataclass
class _Delivery:
    direct: bool
    ambient_sent: bool = False
    sending: asyncio.Lock = field(default_factory=asyncio.Lock)


def _self_mentions(raw, bot_ids):
    ids = set(bot_ids)
    marked = False
    for member in raw.get("mentions") or ():
        if not isinstance(member, dict) or member.get("scope") == "all":
            continue
        member_ids = {
            v for key in ("id", "member_openid") if isinstance(v := member.get(key), str) and v
        }
        if member.get("is_you") is True or member_ids & bot_ids:
            marked = True
            ids.update(member_ids)
    return ids, marked


def _plain_group_message(raw, content):
    if not content or not raw.get("id") or "@" in content or raw.get("message_type", 0) != 0:
        return False
    if any(
        raw.get(field)
        for field in (
            "attachments",
            "mentions",
            "mention_everyone",
            "ark_data",
            "msg_elements",
            "message_reference",
            "referenced_message",
        )
    ):
        return False
    scene = raw.get("message_scene") or {}
    return (
        isinstance(scene, dict)
        and scene.get("source", "default") == "default"
        and not any(
            isinstance(item, str) and item.startswith("ref_msg_idx=")
            for item in scene.get("ext") or ()
        )
    )


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
        unmentioned_context_filter: Callable[[MessageContext], bool] | None = None,
        ambient_handler: AmbientHandler | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._http = http
        self._handler = handler
        self._panel_commands = panel_commands
        self._unmentioned_group_filter = unmentioned_group_filter
        self._unmentioned_context_filter = unmentioned_context_filter
        self._ambient_handler = ambient_handler
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
        except ImportError as exc:
            raise RuntimeError("缺少 qqbot-agent-sdk，请先安装项目依赖") from exc

        api = QQApiClient(app_id=self._app_id, client_secret=self._app_secret)
        api.setup(self._http)
        media_uploader = MediaUploader(api_client=api, http_client=self._http, log_tag="MaimaiBot")
        parser = EventParser()

        class GroupSocket(QQWebSocket):
            def _handle_dispatch(self, event_type, data):
                # The SDK drops duplicates by message ID before invoking user code.
                # A full event must not swallow a later GROUP_AT event with the same ID.
                if event_type == _FULL_GROUP_MESSAGE_EVENT:
                    self._dispatch_to_main(self._cb.on_message_event(event_type, data))
                else:
                    super()._handle_dispatch(event_type, data)

        session_id: str | None = None
        sequence: int | None = None
        seen_messages: OrderedDict[tuple[str, str, str], _Delivery] = OrderedDict()
        bot_ids = frozenset({self._app_id})

        def on_ready(ready) -> None:
            nonlocal bot_ids
            if ready.user:
                bot_ids = frozenset(
                    filter(None, (self._app_id, ready.user.id, ready.user.member_openid))
                )

        def get_session() -> tuple[str | None, int | None]:
            return session_id, sequence

        def set_session(new_session_id: str | None, new_sequence: int | None) -> None:
            nonlocal session_id, sequence
            session_id, sequence = new_session_id, new_sequence

        async def on_message(event_type: str, raw: dict) -> None:
            parse_type = (
                "GROUP_AT_MESSAGE_CREATE" if event_type == _FULL_GROUP_MESSAGE_EVENT else event_type
            )
            event = parser.parse(parse_type, raw)
            if event is None or not event.user_id:
                logger.warning("无法解析 QQ 消息事件：type=%s", event_type)
                return
            content = event.content
            full = event_type == _FULL_GROUP_MESSAGE_EVENT
            addressed = not full
            role = ""
            if event.chat_scope == "group":
                author = raw.get("author") or {}
                if author.get("bot") or author.get("id") in bot_ids or event.user_id in bot_ids:
                    return
                role = author.get("member_role")
                role = role if role in ("member", "admin", "owner") else ""
                ids, marked = _self_mentions(raw, bot_ids)
                original = str(raw.get("content") or "").strip()
                addressed = (
                    addressed or marked or any(m[1] in ids for m in _MENTION.finditer(original))
                )
                # The SDK removes a leading @displayName even for unrelated full events.
                # Use QQ's original text for those events, including mentions of other people.
                text = content if addressed else original
                content = _MENTION.sub(lambda m: "" if m[1] in ids else m[0], text).strip()
            context = MessageContext(
                user_id=event.user_id,
                chat_id=event.chat_id,
                chat_scope=event.chat_scope,
                content=content
                or (
                    "/help"
                    if event.chat_scope == "group" and not event.attachments and addressed
                    else ""
                ),
                message_id=event.message_id,
                avatar_url=_qq_avatar_url(raw)
                or _qq_openid_avatar_url(self._app_id, event.user_id),
                image_urls=_image_attachment_urls(event.attachments),
                json_urls=_json_attachment_urls(event.attachments),
                member_role=role,
            )
            ambient = False
            if full and not addressed:
                accepted = (
                    self._unmentioned_context_filter(context)
                    if self._unmentioned_context_filter is not None
                    else self._unmentioned_group_filter
                    and self._unmentioned_group_filter(context.content)
                )
                if not accepted:
                    if self._ambient_handler is None or not _plain_group_message(raw, content):
                        return
                    ambient = True
            identity = (event.chat_scope, event.chat_id, event.message_id)
            delivery = seen_messages.get(identity) if event.message_id else None
            if delivery:
                if delivery.direct or ambient:
                    return
                # A genuine @ event has priority over a full event still being evaluated.
                delivery.direct = True
            else:
                delivery = _Delivery(direct=not ambient)
            if event.message_id:
                seen_messages[identity] = delivery
                seen_messages.move_to_end(identity)
                if len(seen_messages) > 1024:
                    seen_messages.popitem(last=False)
            if ambient:
                try:
                    answer = await self._ambient_handler(context)
                    async with delivery.sending:
                        if answer and not delivery.direct:
                            await api.send_text(
                                event.chat_scope,
                                event.chat_id,
                                answer,
                                reply_to=event.message_id,
                                markdown=False,
                            )
                            delivery.ambient_sent = True
                except Exception as exc:  # noqa: BLE001 - keep optional group chatter silent
                    # Exceptions may contain prompts, so omit their messages and tracebacks.
                    logger.warning("QQ 群闲聊处理失败 (%s)", type(exc).__name__)
                return
            logger.info(
                "收到 QQ 消息：type=%s scope=%s",
                event_type,
                event.chat_scope,
            )
            try:
                answer = await self._handler(context)
                # QQ can deliver the AT copy after we already sent a passive reply.
                async with delivery.sending:
                    if delivery.ambient_sent:
                        return
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
                        try:
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
                        except (OSError, RuntimeError, httpx.HTTPError) as exc:
                            logger.warning("QQ 图片发送失败，返回文字版：%s", type(exc).__name__)
                            text = answer.text or "图片暂时无法发送。"
                            if answer.followup_text and answer.followup_text not in text:
                                text += "\n" + answer.followup_text
                            await _send_text_reply(
                                api,
                                event.chat_scope,
                                event.chat_id,
                                event.message_id,
                                text,
                                answer.quick_actions,
                            )
                            return
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
            except Exception as exc:
                async with delivery.sending:
                    if delivery.ambient_sent:
                        return
                if event.chat_scope == "group":
                    logger.warning("处理 QQ 群消息失败 (%s)", type(exc).__name__)
                else:
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
            on_ready=on_ready,
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
        self._ws = GroupSocket(callbacks=callbacks, log_tag="MaimaiBot")
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


def _image_attachment_urls(attachments) -> tuple[str, ...]:
    """Only current-message image attachments; never referenced-message images."""
    urls = []
    for attachment in attachments:
        kind = attachment.content_type.casefold().split(";", 1)[0].strip()
        # Some mobile uploads use a generic MIME type but retain the filename.
        image_filename = (
            getattr(attachment, "filename", "")
            .casefold()
            .endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
        )
        if not (
            kind.startswith("image/")
            or (kind in ("", "application/octet-stream") and image_filename)
        ):
            continue
        url = _attachment_url(attachment.url)
        if url:
            urls.append(url)
    return tuple(dict.fromkeys(urls))


def _json_attachment_urls(attachments) -> tuple[str, ...]:
    urls = []
    for attachment in attachments:
        kind = attachment.content_type.partition(";")[0].strip().casefold()
        if kind == "application/json" or attachment.filename.casefold().endswith(".json"):
            url = _attachment_url(attachment.url)
            if url:
                urls.append(url)
    return tuple(dict.fromkeys(urls))


def _attachment_url(value: str) -> str:
    url = value.strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        url = "https://" + url
    return url if url.startswith(("https://", "http://")) else ""


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
    if not quick_actions:
        await api.send_text(chat_scope, chat_id, text, reply_to=message_id, markdown=False)
        return
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
