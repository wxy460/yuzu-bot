from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass

import httpx

from .song_catalog import Chart

logger = logging.getLogger(__name__)

CHART_ASSET_URL = "https://assets2.lxns.net/maimai/chart/{chart_id}.txt"
CHART_TIMEOUT = httpx.Timeout(connect=8, read=15, write=8, pool=8)
_CACHE_SECONDS = 21_600
_MAX_CHART_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class ChartPatternAnalysis:
    source_url: str
    raw_available: bool
    event_count: int = 0
    max_subdivision: int = 0
    dense_events: int = 0
    chord_count: int = 0
    multi_slide_count: int = 0
    fan_count: int = 0
    fold_count: int = 0
    rotation_count: int = 0


class ChartAnalysisService:
    """Analyze the actual Simai chart file used by LXNS's public chart viewer."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._cache: dict[int, tuple[float, str | None]] = {}

    async def describe(self, chart: Chart, bpm: int) -> str:
        analysis = await self.analyze(chart)
        return format_chart_guide(chart, bpm, analysis)

    async def brief(self, chart: Chart, bpm: int) -> str:
        """Return a compact, evidence-based pattern summary for recommendation cards."""
        analysis = await self.analyze(chart)
        return format_chart_brief(chart, bpm, analysis)

    async def analyze(self, chart: Chart) -> ChartPatternAnalysis:
        chart_id = _chart_resource_id(chart)
        source_url = CHART_ASSET_URL.format(chart_id=chart_id)
        raw = await self._chart_text(chart_id)
        if not raw:
            return ChartPatternAnalysis(source_url, False)
        body = _difficulty_body(raw, chart.difficulty + 2)
        if not body:
            return ChartPatternAnalysis(source_url, False)
        return _analyze_body(body, source_url)

    async def _chart_text(self, chart_id: int) -> str | None:
        cached = self._cache.get(chart_id)
        if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        url = CHART_ASSET_URL.format(chart_id=chart_id)
        try:
            response = await self._http.get(url, timeout=CHART_TIMEOUT)
            response.raise_for_status()
            if len(response.content) > _MAX_CHART_BYTES:
                raise ValueError("谱面文件超过大小限制")
            value = response.text
        except (httpx.HTTPError, UnicodeError, ValueError) as exc:
            logger.info("完整谱面暂不可用 chart_id=%s type=%s", chart_id, type(exc).__name__)
            value = None
        self._cache[chart_id] = (time.monotonic(), value)
        return value


def format_chart_guide(chart: Chart, bpm: int, analysis: ChartPatternAnalysis) -> str:
    total = max(1, chart.note_count)
    tap_rate = chart.tap / total * 100
    slide_rate = chart.slide / total * 100
    hold_rate = chart.hold / total * 100
    break_rate = chart.break_count / total * 100
    rows = [
        f"今日指定谱面：{chart.type_label} {chart.difficulty_name} Lv.{chart.level}（定数 {chart.constant:.1f}）",
        "",
        "【经典配置】",
        (
            f"总物量 {chart.note_count}：TAP {chart.tap}（{tap_rate:.1f}%）/ "
            f"HOLD {chart.hold}（{hold_rate:.1f}%）/ SLIDE {chart.slide}（{slide_rate:.1f}%）/ "
            f"TOUCH {chart.touch} / BREAK {chart.break_count}（{break_rate:.1f}%）。"
        ),
    ]
    if analysis.raw_available:
        features = [
            f"最高 {analysis.max_subdivision} 分细分",
            f"16 分及以上有效事件约 {analysis.dense_events} 个",
            f"双押/多押语法约 {analysis.chord_count} 处",
        ]
        if analysis.multi_slide_count:
            features.append(f"多重星星 {analysis.multi_slide_count} 处")
        if analysis.fan_count:
            features.append(f"扇形 Slide {analysis.fan_count} 处")
        if analysis.fold_count:
            features.append(f"V 字折返 {analysis.fold_count} 处")
        if analysis.rotation_count:
            features.append(f"完整顺/逆时针轮转片段 {analysis.rotation_count} 处")
        rows.append("原始谱面结构：" + "；".join(features) + "。")
    else:
        rows.append("LXNS 本次未返回完整 Simai 文件，以下难点仅按公开物量判断。")

    difficulties: list[str] = []
    advice: list[str] = []
    if analysis.max_subdivision >= 24:
        difficulties.append(
            f"BPM {bpm} 下出现 {analysis.max_subdivision} 分细分，短段爆发、读谱和落键稳定性是主要压力"
        )
        advice.append("先降速确认高密度段的手序，再逐步回到原速")
    elif analysis.max_subdivision >= 16:
        difficulties.append(f"BPM {bpm} 下有较多 16 分节奏，连续交互的均匀度很重要")
        advice.append("用固定左右交替处理连续 TAP，避免前快后慢")
    if analysis.chord_count >= max(8, chart.note_count // 12):
        difficulties.append("双押/多押密度较高，容易在换手或滑键衔接处吃 Great")
        advice.append("双押段先看同侧还是跨屏，再固定每组的起手")
    if chart.slide >= max(30, chart.note_count * 0.1) or analysis.multi_slide_count:
        difficulties.append("Slide 占比较高且存在复合星星，启动时机和收尾落点需要分开确认")
        advice.append("星星先按节拍启动，手不要为了追尾判过早离开起点")
    if analysis.fan_count or analysis.fold_count:
        kinds = "、".join(
            name
            for enabled, name in ((analysis.fan_count, "扇形"), (analysis.fold_count, "V 字折返"))
            if enabled
        )
        difficulties.append(f"包含{kinds}路径，重点是提前读终点而不是临时追线")
    if chart.hold >= max(25, chart.note_count * 0.07):
        difficulties.append("HOLD 约束明显，按住期间的另一手交互容易错位")
        advice.append("把 HOLD 当作手位限制，单独记住空闲手负责的区域")
    if chart.touch:
        difficulties.append(f"包含 {chart.touch} 个 TOUCH，屏幕中央与外圈切换需要提前抬手")
    if chart.break_count >= max(20, chart.note_count * 0.04):
        difficulties.append("BREAK 数量或占比较高，关键音的准度会明显影响达成率")
        advice.append("先保 BREAK 正拍，再处理装饰性 Slide 和擦键动作")
    if not difficulties:
        difficulties.append("谱面以基础节奏和常规交互为主，难点更偏向全曲稳定率而非单一配置")
    if not advice:
        advice.append("先用谱面确认定位失分段，再按 4～8 小节拆段练习")

    rows.extend(("", "【主要难点】", "；".join(difficulties) + "。"))
    rows.extend(("", "【练习建议】", "；".join(dict.fromkeys(advice)) + "。"))
    rows.extend(
        (
            "",
            (
                "依据：LXNS 公共曲库物量 + 完整 Simai Note 序列。语法次数用于描述谱面结构，"
                "不冒充人工攻略。"
                if analysis.raw_available
                else "依据：LXNS 公共曲库物量；完整谱面缺失，未推断具体手法。"
            ),
            f"谱面确认：{analysis.source_url}",
        )
    )
    return "\n".join(rows)


def format_chart_brief(chart: Chart, bpm: int, analysis: ChartPatternAnalysis) -> str:
    """Summarise only structures supported by the public chart data."""
    features: list[str] = []
    if analysis.raw_available:
        if analysis.max_subdivision >= 24:
            features.append(f"{analysis.max_subdivision}分爆发")
        elif analysis.max_subdivision >= 16:
            features.append("16分交互")
        if analysis.chord_count >= max(8, chart.note_count // 12):
            features.append("双押/多押")
        if analysis.multi_slide_count:
            features.append("多重星星")
        if analysis.fan_count:
            features.append("扇形Slide")
        if analysis.fold_count:
            features.append("V字折返")
        if analysis.rotation_count:
            features.append("轮转")
    if chart.slide >= max(30, chart.note_count * 0.1):
        features.append("Slide复合")
    if chart.hold >= max(25, chart.note_count * 0.07):
        features.append("HOLD约束")
    if chart.touch:
        features.append("TOUCH换区")
    if chart.break_count >= max(20, chart.note_count * 0.04):
        features.append("BREAK准度")
    if not features:
        features.append("基础节奏与全曲稳定")
    prefix = f"BPM {bpm} · " if bpm else ""
    return prefix + " / ".join(dict.fromkeys(features))


def _chart_resource_id(chart: Chart) -> int:
    if chart.song_id >= 100_000:
        return chart.song_id
    base = chart.song_id % 10_000
    return base + 10_000 if chart.chart_type == "dx" else base


def _difficulty_body(raw: str, simai_difficulty: int) -> str:
    pattern = re.compile(
        rf"(?ms)^&inote_{simai_difficulty}=\s*(.*?)(?=^\s*&(?:lv|base|des|inote)_|\Z)"
    )
    match = pattern.search(raw)
    return match.group(1).strip() if match else ""


def _analyze_body(body: str, source_url: str) -> ChartPatternAnalysis:
    current_subdivision = 4
    event_count = 0
    dense_events = 0
    subdivisions: Counter[int] = Counter()
    compact = re.sub(r"\s+", "", body)
    for raw_event in body.split(","):
        markers = [int(value) for value in re.findall(r"\{(\d+)\}", raw_event)]
        if markers:
            current_subdivision = markers[-1]
        event = re.sub(r"\([^)]*\)|\{[^}]*\}|\s|E", "", raw_event)
        if not event:
            continue
        event_count += 1
        subdivisions[current_subdivision] += 1
        if current_subdivision >= 16:
            dense_events += 1
    rotations = sum(
        compact.count(pattern)
        for pattern in ("1,2,3,4,5,6,7,8", "8,7,6,5,4,3,2,1")
    )
    return ChartPatternAnalysis(
        source_url=source_url,
        raw_available=True,
        event_count=event_count,
        max_subdivision=max(subdivisions, default=0),
        dense_events=dense_events,
        chord_count=body.count("/"),
        multi_slide_count=body.count("*"),
        fan_count=len(re.findall(r"[1-8]w[1-8]", body)),
        fold_count=len(re.findall(r"[1-8]V[1-8]{2}", body)),
        rotation_count=rotations,
    )
