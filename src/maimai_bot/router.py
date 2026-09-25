from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from re import Pattern

from .domain import BotReply, CommandRequest, MessageContext

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
    ) -> None:
        normalized = _normalize_name(target)
        if normalized not in self._commands:
            raise ValueError(f"模式目标 /{normalized} 尚未注册")
        self._patterns.append(
            _PatternRoute(
                re.compile(expression, re.IGNORECASE),
                normalized,
                argument or (lambda match: match.group(0)),
            )
        )

    async def dispatch(self, context: MessageContext) -> Reply:
        text = context.content.strip()
        if text.startswith("/"):
            head, _, argument = text[1:].partition(" ")
            return await self._dispatch_named(context, head, argument.strip(), unknown=True)

        head, _, argument = text.partition(" ")
        if head.casefold() in self._lookup:
            return await self._dispatch_named(context, head, argument.strip(), unknown=False)
        for route in self._patterns:
            match = route.regex.fullmatch(text)
            if match:
                return await self._dispatch_named(
                    context,
                    route.target,
                    route.argument(match).strip(),
                    unknown=False,
                )
        return await self._fallback(context)

    async def _dispatch_named(
        self,
        context: MessageContext,
        name: str,
        argument: str,
        *,
        unknown: bool,
    ) -> Reply:
        info = self._lookup.get(name.casefold().strip().lstrip("/"))
        if info is None:
            if unknown:
                return "未知命令。发送 /help 查看可用功能。"
            return await self._fallback(context)
        return await info.handler(CommandRequest(context, info.name, argument))

    @property
    def commands(self) -> tuple[CommandInfo, ...]:
        """Registered commands, in registration order, for transport UI adapters."""
        return tuple(self._commands.values())

    def help_text(self, category: str = "") -> str:
        categories: dict[str, list[CommandInfo]] = {}
        for command in self._commands.values():
            categories.setdefault(command.category, []).append(command)
        requested = category.strip().casefold()
        selected = next((name for name in categories if name.casefold() == requested), None)
        if requested and selected is None:
            return "没有这个帮助分类。可用分类：" + "、".join(categories)
        names = [selected] if selected else list(categories)
        lines = ["舞萌 Bot 指令帮助（/ 可省略）："]
        for category_name in names:
            lines.append(f"\n【{category_name}】")
            for item in categories[category_name]:
                example = item.usage or f"/{item.name}"
                lines.append(f"{example} — {item.description}")
        if not selected:
            lines.append("\n可发送 /help 分类名 只看一类，例如 /help 成绩。")
        lines.append("直接 @机器人 说话可进入对话。")
        return "\n".join(lines)


def _normalize_name(name: str) -> str:
    normalized = name.strip().casefold().lstrip("/")
    if not normalized or " " in normalized:
        raise ValueError("命令名不能为空或包含空格")
    return normalized
