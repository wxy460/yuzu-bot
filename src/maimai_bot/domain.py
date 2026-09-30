from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MessageContext:
    user_id: str
    chat_id: str
    chat_scope: str
    content: str
    message_id: str
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class CommandRequest:
    context: MessageContext
    command: str
    argument: str


@dataclass(frozen=True, slots=True)
class QuickAction:
    """A transport-neutral shortcut shown below a bot reply."""

    label: str
    command: str = ""
    auto_send: bool = True
    url: str | None = None


@dataclass(frozen=True, slots=True)
class BotReply:
    """A platform-neutral reply that may contain a rendered PNG."""

    text: str = ""
    image_png: bytes | None = None
    followup_text: str = ""
    quick_actions: tuple[QuickAction, ...] = ()
