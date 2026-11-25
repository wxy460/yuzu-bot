import json
import re
import runpy
from pathlib import Path

from maimai_bot.help_catalog import SECTION_ORDER

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "bot-homepage/dist/index.html"


def site_rows():
    source = SITE.read_text(encoding="utf-8")
    return json.loads(re.search(r"const registry=(\[.*?\]);", source, re.DOTALL)[1])


def test_published_page_source_matches_the_command_registry():
    source = SITE.read_text(encoding="utf-8")
    generator = runpy.run_path(str(ROOT / "scripts/sync_help_site.py"))
    assert generator["render_site"](source) == source, "运行 python scripts/sync_help_site.py"
    rows = site_rows()
    names = [row[0] for row in rows]
    assert len(names) == len(set(names))
    assert list(dict.fromkeys(row[5] for row in rows)) == list(SECTION_ORDER)
    reachable = set(names) | {alias for row in rows for alias in row[4]}
    assert {
        "识图", "舞萌数据源", "中二数据源", "绑定账号", "表情包", "群记忆", "礼物图鉴", "签到",
        "jrys", "合成状态", "塔罗牌3", "点歌", "我的画像", "群聊设置", "群戳频次", "更新日志",
    } <= reachable


def test_each_command_exposes_concrete_copyable_examples_and_qq_topics():
    commands = {row[0]: row for row in site_rows()}
    for name, original, description, usage, aliases, section, examples, topic in commands.values():
        assert name and original and description and usage and section and examples
        assert isinstance(aliases, list) and isinstance(topic, str)
        for sample, purpose in examples:
            assert sample.strip() and purpose.strip()
            assert not any(char in sample for char in "[]|→"), (name, sample)
    for name, group, topic in (
        ("pc50", "舞萌", "成绩"),
        ("plate", "舞萌", "牌子"),
        ("cosmetic", "舞萌", "账号与外观"),
        ("合成", "礼物工坊", "合成"),
        ("群聊设置", "管理", "群设置"),
    ):
        assert (commands[name][5], commands[name][7]) == (group, topic)
    samples = {sample for sample, _ in commands["pc50"][6]}
    assert {"/pc50", "/pc50 导入", "/pc50 取消", "/pc50 清除"} <= samples
    assert any("彩将" in sample for sample, _ in commands["plate"][6])
    assert any("镜将" in sample or "鏡将" in sample for sample, _ in commands["plate"][6])
