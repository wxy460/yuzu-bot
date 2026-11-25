"""Navigation common to QQ menus and the searchable website."""

import re

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


# The primary menu stays compact. Each of these examples is a separate,
# sendable message, used by both the QQ detail cards and the web copy buttons.
EXAMPLES = {
    "b50": (("/b50", "查看当前舞萌查分源的 Best 35 与 New 15 图"),),
    "r50": (("/r50", "仅在当前查分源提供逐谱游玩时间时可用"),),
    "listen-guess": (
        ("/listen-guess", "查看试听模式的开放情况，目前公共曲库未提供可分发的试听音源"),
    ),
    "daily": (("/daily", "按个人成绩、拟合定数和可获得的 Rating 增量推荐 10 张谱面"),),
    "help": (
        ("/help 舞萌 成绩", "浏览舞萌成绩指令"),
        ("/help pc50", "查看一条指令的所有用法"),
        ("/help 文字", "查看可复制的分类目录"),
    ),
    "pc50": (
        ("/pc50", "查看个人逐谱游玩次数 Top 50"),
        ("/pc50 导入", "在同一会话中于 3 分钟内发送本人带游玩次数的 JSON 文件，成功后替换原快照"),
        ("/pc50 取消", "停止等待本次导入的文件"),
        ("/pc50 清除", "删除当前身份已导入的次数快照"),
    ),
    "plate": (
        ("/plate 橙将", "查看橙将完成情况与高难度缺谱"),
        ("镜将进度", "查看镜将进度图"),
        ("彩将进度", "查看彩将进度图"),
        ("舞进度", "查看舞系进度"),
        ("霸进度", "查看霸者进度"),
    ),
    "level": (
        ("14完成表", "查看等级 14 的游玩进度"),
        ("14ap", "看 AP 进度"),
        ("14fc", "看 FC 进度"),
        ("14sss+", "看 SSS+ 进度"),
    ),
    "scoretable": (
        ("14分表", "按达成率查看等级 14 的前 100 张谱面"),
        ("13+分表", "按等级查询"),
        ("14.7分表", "按确切定数查询"),
    ),
    "recognize": (
        ("/识图", "3 分钟内在同一会话发送成绩图，仅接收 @ 的群请附图时再次 @机器人"),
        ("/识图 取消", "结束本次识图等待"),
    ),
    "trend": (
        ("/trend", "查看已记录的舞萌 Rating 和进榜底分"),
        ("/trend 中二", "查看中二成绩趋势"),
    ),
    "source": (
        ("/舞萌数据源", "查看当前舞萌查分源"),
        ("/source 水鱼", "切换到水鱼"),
        ("/source 落雪", "切换到已授权的落雪账号"),
    ),
    "中二数据源": (
        ("/中二数据源", "查看当前中二查分源"),
        ("/中二数据源 水鱼", "切换中二到水鱼"),
        ("/中二数据源 落雪", "切换中二到已授权的落雪账号"),
    ),
    "lxns": (
        ("/lxns", "私聊打开官方授权入口，群内获取关联编号，再按返回步骤确认"),
        ("/lxns 状态", "查看自己的落雪关联状态"),
        ("/lxns 解绑", "解除落雪关联"),
    ),
    "unbind": (("/unbind", "查看解绑说明"), ("/unbind 确认", "确认停用当前身份的水鱼舞萌绑定")),
    "cosmetic": (
        ("/cosmetic", "查看当前外观与收藏品图鉴"),
        ("/cosmetic 搜索 姓名框 橙将", "按名称搜索姓名框"),
        ("/cosmetic 搜索 头像 初音", "搜索游戏头像"),
        ("/cosmetic 头像 101", "按图鉴编号选择头像"),
        ("/cosmetic 姓名框 609102", "按图鉴编号选择彩将姓名框"),
        ("/cosmetic 头像 QQ", "恢复 QQ 头像"),
        ("/cosmetic 姓名框 自动", "恢复自动选择姓名框"),
    ),
    "合成": (("/合成", "查看配方、材料编号和制作时间"), ("/合成 1+2", "各消耗一份材料制作礼物")),
    "合成状态": (("/合成状态", "查看正在制作的礼物"), ("/合成状态 2", "查看队列第二页")),
    "送礼": (("/送礼", "查看可赠送的礼物"), ("/送礼 1*2", "从背包给柚子送两份 1 号礼物")),
    "推歌": (("/推歌", "随机推荐一首歌"), ("/点歌 Fragrance", "按曲名点歌")),
    "群聊设置": (
        ("/群聊设置", "查看本群设置"),
        ("/群聊设置 开启", "群主或管理员同时开启偶尔接话和群聊记忆"),
        ("/群聊设置 关闭", "群主或管理员同时关闭偶尔接话和群聊记忆"),
    ),
    "发言频次": (
        ("/发言频次", "查看当前上限"),
        ("/发言频次 3", "群主或管理员设置每小时最多接话三次"),
        ("/发言频次 0", "停止偶尔接话"),
    ),
    "群戳频次": (
        ("/群戳频次", "查看文字戳戳概率"),
        ("/群戳频次 20%", "群主或管理员调整接话时附带文字戳戳的概率"),
    ),
    "群记忆": (
        ("/群记忆", "查看本群记忆状态"),
        ("/群记忆 开启", "群主或管理员启用群记忆"),
        ("/群记忆 关闭", "群主或管理员停用群记忆"),
        ("/群记忆 清除", "群主或管理员清除本群便签、成员画像与最近聊天片段"),
    ),
    "我的画像": (
        ("/我的画像", "查看自己的群内画像"),
        ("/我的画像 退出", "退出本群个人画像"),
        ("/我的画像 恢复", "恢复参与"),
        ("/我的画像 清除", "清除本群自己的画像记录"),
    ),
    "更新日志": (
        ("/更新日志", "Bot 主可查版本记录"),
        ("/更新日志 身份", "部署者在私聊查看自己的 OpenID"),
    ),
    "memory": (("/memory", "查看当前会话的记忆数"), ("/memory clear", "清空自己的当前会话记忆")),
    "web": (("/web 舞萌DX最近有什么更新", "联网查找并回答"),),
    "song": (
        ("/song 潘", "按别名查歌，唯一结果返回谱面图和预览链接"),
        ("/song 834", "按曲目 ID 查找"),
    ),
    "chart": (("紫潘", "查看 MASTER 谱面"), ("紫id 834", "按颜色与曲目 ID 查看")),
    "aliases": (("潘有什么别名", "列出该曲的已知别名"),),
    "which": (("11451是什么歌", "按别名反查歌曲"),),
    "achieve": (("/achieve 1000 990 8 1 1", "按总物量、PERFECT、GREAT、GOOD、MISS 估算达成率"),),
    "threshold": (("/threshold 14.7", "按当前 B35/B15 底分算出进榜线"),),
    "guess": (
        ("/guess", "开始舞萌猜歌"),
        ("谱面猜歌", "用谱面线索猜歌"),
        ("note猜歌", "按音符物量猜曲"),
        ("开字母", "逐步揭开曲名字母"),
        ("曲绘猜歌 14+", "用局部曲绘猜 14+ 的歌"),
        ("/guess 14.0-14.5 DX", "限定定数区间和谱面类型"),
    ),
    "answer": (("/answer 潘", "提交本局猜歌答案"),),
    "sjcj": (("/sjcj", "随机抽取自己的成绩"), ("/sjcj 牛逼 3", "抽三条表现较好的成绩")),
    "舞萌定数表": (
        ("/舞萌定数表 14+", "查看国服 14+ 谱面"),
        ("/舞萌定数表 日服 14+ p2", "查看日服 14+ 的第二页"),
    ),
    "id": (("/id m834", "用 ID 查舞萌乐曲"), ("/id z317", "用 ID 查中二乐曲")),
    "段位表": (("/段位表 真七段", "查看真七段四曲和 LIFE 规则"),),
    "asearch": (("/asearch 天界", "按名称找区域"),),
    "ainfo": (("/ainfo m150006", "按编号查看区域资料"),),
    "中二进度表": (
        ("/中二进度表 14+ sss", "看 14+ 等级的 SSS 进度"),
        ("/中二进度表 14+ aj", "看 AJ 进度"),
    ),
    "中二定数表": (("/中二定数表 14+", "查国服谱面"), ("/中二定数表 日服14+", "查日服谱面")),
    "chub30": (("/chub30", "查询本人中二旧曲 Best 30"),),
    "chun20": (("/chun20", "查询本人中二当前版本 New 20"),),
    "chubind": (("/chubind", "打开水鱼中二授权入口"), ("/chubind 解绑", "解除水鱼中二绑定")),
    "hardle": (("#hardle", "查看本群今日榜"), ("#hardle all", "查看本群总榜")),
    "guesssong": (("/guesssong maimai 14+", "限定舞萌 14+ 等级猜歌"),),
    "秋什么": (("/秋什么 14+", "随机推荐 14+ 的中二乐曲"),),
}


def command_usage(command):
    """Short registry synopsis; retain natural-language shortcuts and canonical keys."""
    usage = command.usage or f"/{command.name}"
    if usage.partition(" ")[0].lstrip("/").casefold() == command.name.casefold():
        return usage if usage.startswith("/") else "/" + usage
    return f"/{command.name} · {usage}"


def command_examples(command):
    if command.name in EXAMPLES:
        return EXAMPLES[command.name]
    usage = command.usage or f"/{command.name}"
    return ((usage, command.description),)


TOPICS = {
    "舞萌": {
        "成绩": {
            "b50",
            "ap50",
            "r50",
            "pc50",
            "minfo",
            "scores",
            "scoretable",
            "analyze",
            "threshold",
            "b40",
            "trend",
            "上传成绩",
            "level",
            "sjcj",
        },
        "牌子": {"plate", "plate-conditions"},
        "曲目": {"tsearch", "maijacket", "舞萌定数表"},
        "段位与区域": {"段位表", "随机段位", "asearch", "ainfo"},
        "工具": {"mai"},
        "账号与外观": {"bind", "unbind", "source", "cosmetic"},
        "推歌与猜歌": {"guess", "hint", "answer", "reveal", "listen-guess", "random", "daily"},
    },
    "中二及其他音游": {"跨游戏查曲": {"search", "id"}},
    "管理": {
        "群设置": {"群聊设置", "发言频次", "群戳频次"},
        "账号": {"绑定账号", "lxns", "解绑落雪"},
    },
    "礼物工坊": {"合成": {"合成", "一键合成", "合成状态"}, "礼物": {"礼物图鉴", "背包", "送礼"}},
}


def topic_for(command):
    section = section_for(command)
    for topic, names in TOPICS.get(section, {}).items():
        if command.name in names:
            return topic
    if section == "管理":
        return "机器人"
    return command.category if section in {"舞萌", "中二及其他音游"} else ""


def group_topics(commands):
    groups = {}
    for command in commands:
        topic = topic_for(command)
        if topic:
            groups.setdefault(topic, []).append(command)
    return groups


def select_group(commands, query):
    parts = re.split(r"[\s/／>＞·]+", query.strip().casefold().strip("/"))
    selected = SECTION_ALIASES.get(parts[0], parts[0])
    for legacy in (False, True):
        for title, items in group_commands(commands, legacy=legacy).items():
            if title.casefold() != selected:
                continue
            if len(parts) == 1:
                return title, items
            if not legacy and len(parts) == 2:
                for topic, children in group_topics(items).items():
                    if topic.casefold() == parts[1]:
                        return f"{title} · {topic}", children
    return "", []


def topic_names(title, items):
    topics = group_topics(items) if title in SECTION_ORDER else {}
    return tuple(topics) if len(topics) > 1 else ()


def topic_links(title, items):
    return [f"/help {title} {topic}" for topic in topic_names(title, items)]
