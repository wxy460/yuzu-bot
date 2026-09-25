from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .divingfish import PlayerRecords
from .song_catalog import PLATE_VERSION_ALIASES, Chart, Song, SongCatalogService

DIFFICULTY_MARKS = {"绿": 0, "黄": 1, "红": 2, "紫": 3, "白": 4}
FC_ORDER = {"": 0, "fc": 1, "fcp": 2, "ap": 3, "app": 4}
FS_ORDER = {"": 0, "sync": 1, "fs": 2, "fsp": 3, "fsd": 4, "fsdp": 5, "fsdpx": 5}


@dataclass(frozen=True, slots=True)
class PlateEntry:
    chart: Chart
    record: dict[str, Any] | None

    @property
    def achievement(self) -> float:
        return float((self.record or {}).get("achievements", 0) or 0)


@dataclass(frozen=True, slots=True)
class PlateProgress:
    plate: str
    version: str
    goal: str
    entries: tuple[PlateEntry, ...]
    completed: tuple[PlateEntry, ...]
    unfinished: tuple[PlateEntry, ...]

    @property
    def percent(self) -> float:
        return len(self.completed) / len(self.entries) * 100 if self.entries else 0

    @property
    def remaining_by_difficulty(self) -> dict[int, int]:
        return {
            difficulty: sum(entry.chart.difficulty == difficulty for entry in self.unfinished)
            for difficulty in range(5)
        }


def all_records(records: PlayerRecords) -> list[dict[str, Any]]:
    return [*records.old, *records.new]


def record_line(item: dict[str, Any], index: int | None = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    return (
        f"{prefix}{item.get('title', '未知')} "
        f"[{item.get('type', '?')} {item.get('level_label', '?')} {item.get('level', '?')}] "
        f"{float(item.get('achievements', 0) or 0):.4f}% "
        f"RA {int(item.get('ra', 0) or 0)} "
        f"{str(item.get('rate') or '').upper()} "
        f"{str(item.get('fc') or '-').upper()} {str(item.get('fs') or '-').upper()}"
    )


def ap50_text(records: PlayerRecords) -> str:
    selected = [
        item
        for item in all_records(records)
        if str(item.get("fc") or "").casefold() in {"ap", "app"}
    ]
    selected.sort(key=_record_sort_key)
    rows = [f"{records.nickname} 的 AP50（共 {len(selected)} 张 AP/AP+ 谱面）"]
    rows.extend(record_line(item, index) for index, item in enumerate(selected[:50], 1))
    if not selected:
        rows.append("水鱼记录中还没有 AP 或 AP+ 谱面。")
    return "\n".join(rows)


def unavailable_history(kind: str, records: PlayerRecords) -> str:
    keys = {key for item in all_records(records) for key in item}
    needed = "游玩时间" if kind == "r50" else "游玩次数"
    return (
        f"当前水鱼 OAuth 成绩记录不包含{needed}字段，因此无法可靠生成 {kind.upper()}。\n"
        f"已读取到的字段：{'、'.join(sorted(keys))}\n"
        "这个命令已保留；以后数据源提供对应字段时可直接启用。不会要求绑定落雪，也不会伪造数据。"
    )


def personal_song_text(records: PlayerRecords, song: Song) -> str:
    matched = [item for item in all_records(records) if _record_matches_song(item, song)]
    if not matched:
        return f"水鱼中没有找到你在《{song.title}》上的成绩。"
    matched.sort(key=lambda item: (int(item.get("level_index", 0)), str(item.get("type", ""))))
    rows = [f"{records.nickname}｜{song.title}（ID {song.id}）"]
    rows.extend(record_line(item) for item in matched)
    return "\n".join(rows)


def conditional_scores(records: PlayerRecords, condition: str) -> str:
    words = condition.casefold().split()
    if not words:
        return "用法：/scores 14+，也可追加 紫、DX、SSS+、FC、AP 等条件。"
    selected = all_records(records)
    understood = False
    for word in words:
        if re.fullmatch(r"\d{1,2}\+?", word):
            selected = [item for item in selected if str(item.get("level") or "") == word]
            understood = True
        elif word in {"dx", "sd", "standard", "标准"}:
            target = "dx" if word == "dx" else "standard"
            selected = [item for item in selected if _chart_type(item) == target]
            understood = True
        elif word in DIFFICULTY_MARKS:
            target = DIFFICULTY_MARKS[word]
            selected = [item for item in selected if int(item.get("level_index", -1)) == target]
            understood = True
        elif word in {"fc", "fc+", "fcp", "ap", "ap+", "app"}:
            target = word.replace("+", "p")
            selected = [
                item
                for item in selected
                if FC_ORDER.get(str(item.get("fc") or "").casefold(), 0) >= FC_ORDER[target]
            ]
            understood = True
        elif word in {"s", "s+", "ss", "ss+", "sss", "sss+"}:
            threshold = {"s": 97, "s+": 98, "ss": 99, "ss+": 99.5, "sss": 100, "sss+": 100.5}[word]
            selected = [
                item for item in selected if float(item.get("achievements", 0) or 0) >= threshold
            ]
            understood = True
    if not understood:
        return "没有识别出筛选条件。示例：成绩 14+ 紫 AP"
    selected.sort(key=_record_sort_key)
    rows = [f"成绩筛选「{condition}」：{len(selected)} 张"]
    rows.extend(record_line(item, index) for index, item in enumerate(selected[:30], 1))
    if len(selected) > 30:
        rows.append(f"还有 {len(selected) - 30} 条未显示，请增加筛选条件。")
    return "\n".join(rows)


def level_progress_text(
    records: PlayerRecords,
    level: str,
    goal: str,
    charts: list[Chart] | None = None,
) -> str:
    selected = [item for item in all_records(records) if str(item.get("level") or "") == level]
    goal = goal.casefold().replace("完成表", "") or "完成表"
    if charts is None:
        qualified = [item for item in selected if _qualified(item, goal)]
        unfinished: list[dict[str, Any] | Chart] = [
            item for item in selected if item not in qualified
        ]
        total = len(selected)
    else:
        eligible = [chart for chart in charts if chart.level == level]
        record_map = {_record_key(item): item for item in selected}
        qualified = []
        unfinished = []
        for chart in eligible:
            item = record_map.get(_chart_key(chart))
            if item and _qualified(item, goal):
                qualified.append(item)
            else:
                unfinished.append(chart)
        total = len(eligible)
    label = goal.upper() if goal != "完成表" else "游玩"
    rows = [f"{records.nickname}｜Lv.{level} {label} 完成表：{len(qualified)}/{total}"]
    if unfinished:
        rows.append("未完成（最多显示 25 张）：")
        if charts is None:
            unfinished.sort(
                key=lambda item: -float(item.get("achievements", 0) or 0)  # type: ignore[union-attr]
            )
            rows.extend(
                record_line(item, index)  # type: ignore[arg-type]
                for index, item in enumerate(unfinished[:25], 1)
            )
        else:
            rows.extend(
                f"{index}. {chart.song_id} {chart.title} [{chart.type_label} {chart.difficulty_name}]"
                for index, chart in enumerate(unfinished[:25], 1)  # type: ignore[union-attr]
            )
    else:
        rows.append("公共曲库中的这个等级谱面已经全部达成。")
    return "\n".join(rows)


async def plate_progress_text(
    records: PlayerRecords,
    catalog: SongCatalogService,
    plate: str,
) -> str:
    progress = await build_plate_progress(records, catalog, plate)
    rows = [
        f"{records.nickname}｜{progress.plate}进度：{len(progress.completed)}/{len(progress.entries)}"
    ]
    if progress.unfinished:
        rows.append("尚未达成（最多显示 25 张）：")
        rows.extend(
            f"{entry.chart.song_id} {entry.chart.title} "
            f"[{entry.chart.type_label} {entry.chart.difficulty_name} {entry.chart.level}] "
            f"{entry.achievement:.4f}%"
            for entry in progress.unfinished[:25]
        )
    if not progress.entries:
        rows.append("公共曲库中未匹配到这个版本；请稍后重试或检查版本字。")
    return "\n".join(rows)


async def build_plate_progress(
    records: PlayerRecords,
    catalog: SongCatalogService,
    plate: str,
) -> PlateProgress:
    normalized = (
        plate.replace("晓", "暁")
        .replace("樱", "櫻")
        .replace("堇", "菫")
        .replace("辉", "輝")
        .replace("华", "華")
    )
    match = re.fullmatch(
        r"([真超檄橙暁桃櫻紫菫白雪輝舞霸熊華爽煌宙星祭祝双宴镜鏡彩])([極极将神舞者]舞?)", normalized
    )
    if not match:
        raise ValueError("牌子格式不正确，例如：橙将、紫极、舞舞、霸者。")
    version, goal = match.groups()
    goal = "极" if goal == "極" else goal
    songs = await catalog.songs()
    if version in {"舞", "霸"}:
        eligible_charts = [
            chart
            for song in songs
            for chart in song.charts
            if chart.chart_type == "standard"
            and chart.difficulty in ({3, 4} if version == "舞" else {0, 1, 2, 3, 4})
        ]
    else:
        version_titles = PLATE_VERSION_ALIASES.get(version, ())
        eligible_charts = [
            chart
            for song in songs
            for chart in song.charts
            if version_titles
            and catalog.version_name(chart.version) in version_titles
            and chart.difficulty <= 3
        ]
    record_map = {_record_key(item): item for item in all_records(records)}
    completed: list[PlateEntry] = []
    missing: list[PlateEntry] = []
    entries: list[PlateEntry] = []
    target = "者" if version == "霸" and goal == "者" else goal
    for chart in eligible_charts:
        item = record_map.get(_chart_key(chart))
        entry = PlateEntry(chart, item)
        entries.append(entry)
        (completed if item and _qualified(item, target) else missing).append(entry)
    missing.sort(
        key=lambda entry: (
            -entry.chart.constant,
            -entry.chart.difficulty,
            -entry.achievement,
            entry.chart.song_id,
        )
    )
    return PlateProgress(
        plate=f"{version}{goal}",
        version=version,
        goal=goal,
        entries=tuple(entries),
        completed=tuple(completed),
        unfinished=tuple(missing),
    )


def plate_conditions() -> str:
    return (
        "舞萌牌子条件（按对应版本要求谱面）：\n"
        "极／極：FC 或以上\n将：达成率 100.0000%（SSS）或以上\n"
        "神：AP 或 AP+\n舞舞：FDX（Full Sync DX）或以上\n"
        "霸者：初代至 FiNALE 的全部指定谱面达到 80.0000% 或以上\n"
        "版本字：真超檄橙晓桃樱紫堇白雪辉／熊华爽煌宙星祭祝双宴镜彩。"
    )


def scoreline(chart: Chart, target: float) -> str:
    total = (
        chart.tap * 500
        + chart.hold * 1000
        + chart.slide * 1500
        + chart.touch * 500
        + chart.break_count * 2500
    )
    if not total or not 0 < 101 - target < 101:
        return "该谱面缺少物量数据，或目标达成率不在有效范围内。"
    reduce = 101 - target
    tap_great = total * reduce / 10000
    rows = [
        f"{chart.title} [{chart.difficulty_name}] 分数线 {target:.4f}%",
        f"最多允许约 {tap_great:.2f} 个 TAP GREAT（每个 -{10000 / total:.4f}%）。",
    ]
    if chart.break_count:
        break_50 = total * (0.01 / chart.break_count) / 4
        rows.append(
            f"BREAK 50落（共 {chart.break_count} 个）约等于 {break_50 / 100:.3f} 个 TAP GREAT，"
            f"单次 -{break_50 / total * 100:.4f}%。"
        )
    rows.append(
        "等价损失：HOLD GREAT=2，SLIDE GREAT=3，BREAK GREAT=5 个 TAP GREAT；GOOD 为 GREAT 的 2.5 倍。"
    )
    return "\n".join(rows)


def _qualified(item: dict[str, Any], goal: str) -> bool:
    goal = goal.casefold().replace("+", "p")
    achievement = float(item.get("achievements", 0) or 0)
    fc = str(item.get("fc") or "").casefold()
    fs = str(item.get("fs") or "").casefold()
    if goal in {"完成表", "游玩"}:
        return achievement > 0
    if goal in {"者"}:
        return achievement >= 80
    if goal in {"将", "sss"}:
        return achievement >= 100
    if goal in {"sssp"}:
        return achievement >= 100.5
    if goal in {"fc", "fcp", "ap", "app", "极"}:
        target = "fc" if goal == "极" else goal
        return FC_ORDER.get(fc, 0) >= FC_ORDER[target]
    if goal in {"神"}:
        return FC_ORDER.get(fc, 0) >= FC_ORDER["ap"]
    if goal in {"舞", "舞舞", "fdx", "fdxp"}:
        return FS_ORDER.get(fs, 0) >= FS_ORDER["fsd"]
    return False


def _record_matches_song(item: dict[str, Any], song: Song) -> bool:
    return (
        str(item.get("title") or "").casefold() == song.title.casefold()
        or _song_id(item) == song.id
    )


def _song_id(item: dict[str, Any]) -> int:
    try:
        value = int(item.get("song_id", item.get("id", -1)))
    except (TypeError, ValueError):
        return -1
    return value - 10000 if value >= 10000 else value


def _record_key(item: dict[str, Any]) -> tuple[int, str, int]:
    return _song_id(item), _chart_type(item), int(item.get("level_index", -1))


def _chart_key(chart: Chart) -> tuple[int, str, int]:
    return chart.song_id, chart.chart_type, chart.difficulty


def _chart_type(item: dict[str, Any]) -> str:
    value = str(item.get("type") or "").casefold()
    return "standard" if value in {"sd", "standard", "标准"} else value


def _record_sort_key(item: dict[str, Any]) -> tuple[float, float, float, int]:
    return (
        -float(item.get("ra", 0) or 0),
        -float(item.get("ds", 0) or 0),
        -float(item.get("achievements", 0) or 0),
        _song_id(item),
    )
