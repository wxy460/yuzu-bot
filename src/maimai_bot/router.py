from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from re import Pattern

from .domain import BotReply, CommandRequest, MessageContext
from .help_catalog import (
    command_examples,
    command_usage,
    group_commands,
    select_group,
    topic_links,
    topic_names,
)

Reply = str | BotReply
Handler = Callable[[CommandRequest], Awaitable[Reply]]
Fallback = Callable[[MessageContext], Awaitable[Reply]]
ArgumentFactory = Callable[[re.Match[str]], str]


@dataclass(frozen=True, slots=True)
class CommandInfo:
    name: str
    description: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    category: str = "其他"
    usage: str = ""


@dataclass(frozen=True, slots=True)
class _PatternRoute:
    regex: Pattern[str]
    target: str
    argument: ArgumentFactory
    when: Callable[[MessageContext], bool] | None

    def match(self, text: str, context: MessageContext | None):
        match = self.regex.fullmatch(text)
        if match and (self.when is None or (context is not None and self.when(context))):
            return match
        return None


class CommandRouter:
    """Transport-independent command registry.

    Features only depend on MessageContext, so they can be tested without QQ and
    moved to another chat platform later.
    """

    def __init__(self, fallback: Fallback) -> None:
        self._commands: dict[str, CommandInfo] = {}
        self._lookup: dict[str, CommandInfo] = {}
        self._patterns: list[_PatternRoute] = []
        self._fallback = fallback
        self._attachments: dict[str, tuple[Callable[[MessageContext], Awaitable[Reply | None]], Callable[[MessageContext], bool]]] = {}

    def add_attachment_handler(self, handler, predicate, *, kind="file") -> None:
        self._attachments[kind] = handler, predicate

    def set_image_handler(self, handler, predicate) -> None:
        self.add_attachment_handler(handler, predicate, kind="image")

    def accepts_context(self, context: MessageContext) -> bool:
        return self.recognizes_command(context.content, context) or any(
            predicate(context) for _, predicate in self._attachments.values()
        )

    def command(
        self,
        name: str,
        description: str,
        *,
        aliases: tuple[str, ...] = (),
        category: str = "其他",
        usage: str = "",
    ) -> Callable[[Handler], Handler]:
        normalized = _normalize_name(name)
        normalized_aliases = tuple(_normalize_name(alias) for alias in aliases)

        def decorator(handler: Handler) -> Handler:
            if not inspect.iscoroutinefunction(handler):
                raise TypeError("命令处理器必须是 async 函数")
            names = (normalized, *normalized_aliases)
            duplicate = next((item for item in names if item in self._lookup), None)
            if duplicate:
                raise ValueError(f"命令或别名 {duplicate} 已注册")
            info = CommandInfo(
                normalized, description, handler, normalized_aliases, category, usage
            )
            self._commands[normalized] = info
            for item in names:
                self._lookup[item] = info
            return handler

        return decorator

    def pattern(
        self,
        expression: str,
        target: str,
        argument: ArgumentFactory | None = None,
        *,
        when: Callable[[MessageContext], bool] | None = None,
    ) -> None:
        normalized = _normalize_name(target)
        if normalized not in self._commands:
            raise ValueError(f"模式目标 /{normalized} 尚未注册")
        self._patterns.append(
            _PatternRoute(
                re.compile(expression, re.IGNORECASE),
                normalized,
                argument or (lambda match: match.group(0)),
                when,
            )
        )

    async def dispatch(self, context: MessageContext) -> Reply:
        for handler, predicate in self._attachments.values():
            if predicate(context):
                answer = await handler(context)
                if answer is not None:
                    return answer
        content = context.content.strip()
        text = content.removeprefix("/")
        head, _, argument = text.partition(" ")
        command = self._lookup.get(head.casefold())
        if command:
            return await command.handler(CommandRequest(context, command.name, argument.strip()))
        for route in self._patterns:
            match = route.match(text, context)
            if match:
                command = self._commands[route.target]
                return await command.handler(
                    CommandRequest(context, command.name, route.argument(match).strip())
                )
        if content.startswith("/"):
            return "未知命令。发送 /help 查看可用功能。"
        return await self._fallback(context)

    def recognizes_command(self, content: str, context: MessageContext | None = None) -> bool:
        """Return whether plain text should be treated as a bot command.

        QQ's ``GROUP_MESSAGE_CREATE`` event contains every message in a group.
        The transport uses this check to keep no-mention commands working
        without turning ordinary group conversation into LLM prompts.
        """
        text = content.strip()
        if not text:
            return False
        if text.startswith("/"):
            return True
        head, _, _ = text.partition(" ")
        if head.casefold() in self._lookup:
            return True
        return any(route.match(text, context) for route in self._patterns)

    @property
    def commands(self) -> tuple[CommandInfo, ...]:
        """Registered commands, in registration order, for transport UI adapters."""
        return tuple(self._commands.values())

    def help_command_info(self, query: str) -> CommandInfo | None:
        return self._lookup.get(query.strip().lstrip("/").casefold())

    def help_text(self, category: str = "") -> str:
        groups = group_commands(self.commands)
        if not category:
            lines = ["柚子指令目录"]
            for name, items in groups.items():
                topics = topic_names(name, items)
                suffix = "　" + "、".join(topics) if topics else ""
                lines.append(f"/help {name}{suffix}")
            lines.append("发送 /help 文字 舞萌 成绩 查分区，或发 /help 文字 pc50 查一条指令。")
            return "\n".join(lines)
        title, items = select_group(self.commands, category)
        if not items:
            command = self.help_command_info(category)
            if command:
                lines = [f"/{command.name} — {command.description}"]
                lines.extend(f"{example} — {purpose}" for example, purpose in command_examples(command))
                return "\n".join(lines)
            available = dict.fromkeys((*groups, *group_commands(self.commands, legacy=True)))
            return "可查看的帮助分类有 " + "、".join(available) + "。也可发送 /help 指令名。"
        lines = [f"【{title}】", *topic_links(title, items)]
        for item in items:
            lines.append(f"{command_usage(item)} — {item.description}")
        lines.append("发送 /help 指令名 查看可复制的用法，例如 /help pc50。")
        return "\n".join(lines)


def _normalize_name(name: str) -> str:
    normalized = name.strip().casefold().lstrip("/")
    if not normalized or " " in normalized:
        raise ValueError("命令名不能为空或包含空格")
    return normalized
