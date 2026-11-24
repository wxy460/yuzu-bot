"""Navigation common to QQ menus and the searchable website."""

SECTION_ORDER = (
    "日常互动",
    "礼物工坊",
    "占卜与点歌",
    "舞萌",
    "中二及其他音游",
    "群友与记忆",
    "小游戏",
    "管理",
)
NAMED_COMMANDS = {
    "日常互动": {"签到", "好感度", "摸头", "状态", "互动帮助", "sticker", "vtuber"},
    "礼物工坊": {"礼物图鉴", "背包", "送礼", "合成", "一键合成", "合成状态"},
    "占卜与点歌": {"塔罗牌", "塔罗牌3", "jrys", "推歌", "点歌"},
    "舞萌": {
        "b50",
        "bind",
        "unbind",
        "source",
        "cosmetic",
        "daily",
        "guess",
        "answer",
        "hint",
        "reveal",
        "listen-guess",
    },
    "群友与记忆": {"群记忆", "我的画像", "memory", "reset", "web"},
    "管理": {
        "help",
        "about",
        "ping",
        "绑定账号",
        "lxns",
        "解绑落雪",
        "群聊设置",
        "更新日志",
        "time",
    },
    "中二及其他音游": {"search", "id"},
}
BY_NAME = {name: section for section, names in NAMED_COMMANDS.items() for name in names}
BY_CATEGORY = {
    **dict.fromkeys(("曲目", "成绩", "牌子", "随机", "工具", "等级", "舞萌工具"), "舞萌"),
    "中二节奏": "中二及其他音游",
    "其他音游": "中二及其他音游",
    "设置": "管理",
    "校园互动": "日常互动",
    "娱乐": "小游戏",
    "常用": "管理",
}
SECTION_ALIASES = {"maimaidx": "舞萌", "maimai": "舞萌", "舞萌dx": "舞萌"}


def section_for(command):
    return BY_NAME.get(command.name, BY_CATEGORY.get(command.category, command.category))


def group_commands(commands, *, legacy=False):
    groups = {} if legacy else {section: [] for section in SECTION_ORDER}
    for command in commands:
        section = command.category if legacy else section_for(command)
        groups.setdefault(section, []).append(command)
    return {name: items for name, items in groups.items() if items}


def select_group(commands, query):
    selected = query.strip().casefold()
    selected = SECTION_ALIASES.get(selected, selected)
    for legacy in (False, True):
        for title, items in group_commands(commands, legacy=legacy).items():
            if title.casefold() == selected:
                return title, items
    return "", []
