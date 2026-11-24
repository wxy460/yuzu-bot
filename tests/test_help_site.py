import json
import re
import runpy
from pathlib import Path


def test_published_page_source_matches_the_command_registry():
    root = Path(__file__).resolve().parents[1]
    source = (root / "bot-homepage/dist/index.html").read_text(encoding="utf-8")
    generator = runpy.run_path(str(root / "scripts/sync_help_site.py"))
    assert generator["render_site"](source) == source, "运行 python scripts/sync_help_site.py"
    rows = json.loads(re.search(r"const registry=(\[.*?\]);", source, re.DOTALL)[1])
    reachable = {name for name, *_ in rows} | {
        alias for *_, aliases, section in rows for alias in aliases
    }
    assert {
        "识图",
        "舞萌数据源",
        "中二数据源",
        "绑定账号",
        "表情包",
        "群记忆",
        "礼物图鉴",
        "签到",
        "jrys",
        "合成状态",
        "塔罗牌3",
        "点歌",
        "我的画像",
        "群聊设置",
        "群戳频次",
        "更新日志",
    } <= reachable
