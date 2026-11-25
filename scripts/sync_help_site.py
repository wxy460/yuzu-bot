"""Build the public command catalog from the same registry used by the QQ bot."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from maimai_bot.features import build_router
from maimai_bot.help_catalog import command_examples, command_usage, group_commands, topic_for
from maimai_bot.services.lxns_scores import ScoreSources

SITE = Path(__file__).resolve().parents[1] / "bot-homepage/dist/index.html"
BLOCK = re.compile(
    r"(?<=/\* command-registry:start \*/\n).*?(?=\n    /\* command-registry:end \*/)", re.DOTALL
)


def render_site(source: str) -> str:
    # Registration only. These command dependencies are called at dispatch time.
    unused = object()
    router = build_router(
        chat=unused,
        scores=ScoreSources(unused, unused),
        b50_images=unused,
        cosmetics=unused,
        catalog=unused,
        chart_analysis=unused,
        bilibili=unused,
        recognition=unused,
        community=unused,
        cross_games=unused,
        group_life=unused,
    )
    rows = [
        [
            cmd.name,
            cmd.category,
            cmd.description,
            command_usage(cmd),
            list(cmd.aliases),
            section,
            command_examples(cmd),
            topic_for(cmd),
        ]
        for section, commands in group_commands(router.commands).items()
        for cmd in commands
    ]
    encoded = ",\n      ".join(
        json.dumps(row, ensure_ascii=False).replace("<", "\\u003c") for row in rows
    )
    updated, count = BLOCK.subn(lambda _: f"    const registry=[\n      {encoded}\n    ];", source)
    if count != 1:
        raise ValueError("帮助页缺少唯一命令清单标记")
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查命令是否已同步")
    args = parser.parse_args()
    before = SITE.read_text(encoding="utf-8")
    after = render_site(before)
    if args.check:
        if before != after:
            raise SystemExit("帮助页清单需要更新，请运行 python scripts/sync_help_site.py")
    elif before != after:
        SITE.write_text(after, encoding="utf-8")


if __name__ == "__main__":
    main()
