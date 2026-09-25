from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .divingfish import PlayerRecords
from .rating import rating_for
from .song_catalog import Chart, Song


@dataclass(frozen=True, slots=True)
class RecommendationItem:
    chart: Chart
    current_achievement: float | None
    current_rating: int
    target_rating: int
    target_achievement: float
    rating_gain: int
    pool: str
    in_best: bool
    tier: str
    pattern: str = ""

    @property
    def gain(self) -> int:
        """Estimated increase to the player's total DX Rating."""
        return self.rating_gain


@dataclass(frozen=True, slots=True)
class DailyRecommendations:
    day: date
    player_name: str
    player_rating: int
    target_constant: float
    items: tuple[RecommendationItem, ...]

    def with_patterns(self, patterns: list[str]) -> DailyRecommendations:
        return replace(
            self,
            items=tuple(
                replace(item, pattern=pattern)
                for item, pattern in zip(self.items, patterns, strict=True)
            ),
        )


def build_daily_recommendations(
    player: PlayerRecords,
    songs: tuple[Song, ...],
    version_name: Callable[[int], str],
    user_id: str,
    day: date | None = None,
    *,
    limit: int = 10,
) -> DailyRecommendations:
    today = day or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    best = player.best50()
    best_entries = [*best.old, *best.new]
    best_count = max(1, len(best_entries))
    average_ra = sum(int(item.get("ra", 0) or 0) for item in best_entries) / best_count
    # 100% uses coefficient 21.6. Bias the recommendation 0.25 below the
    # current B50-equivalent constant so practice remains attainable.
    target_constant = max(3.0, min(15.0, average_ra / 21.6 - 0.25))

    old_records = _records_by_key(player.old)
    new_records = _records_by_key(player.new)
    old_best_keys = {_record_key(item) for item in best.old[:35]}
    new_best_keys = {_record_key(item) for item in best.new[:15]}
    old_bottom = _bottom_rating(best.old[:35], 35)
    new_bottom = _bottom_rating(best.new[:15], 15)

    newest_version = max((song.version for song in songs), default=0)
    newest_name = version_name(newest_version)
    candidates: list[tuple[float, Chart, dict | None, int, int, str, bool, float]] = []
    for song in songs:
        for chart in song.charts:
            if chart.difficulty < 2 or chart.constant <= 0:
                continue
            key = (chart.song_id, chart.chart_type, chart.difficulty)
            catalog_is_new = version_name(chart.version) == newest_name
            if key in new_records:
                is_new = True
                record = new_records[key]
            elif key in old_records:
                is_new = False
                record = old_records[key]
            else:
                is_new = catalog_is_new
                record = None
            achievement = (
                float(record.get("achievements", 0) or 0) if record is not None else None
            )
            current_ra = int(record.get("ra", 0) or 0) if record is not None else 0
            pool = "B15" if is_new else "B35"
            best_keys = new_best_keys if is_new else old_best_keys
            bottom_ra = new_bottom if is_new else old_bottom
            in_best = key in best_keys
            fitted = chart.fit_constant if chart.fit_constant and chart.fit_constant > 0 else chart.constant
            delta = fitted - target_constant
            tier = "简单" if delta <= -0.15 else "困难" if delta >= 0.18 else "中等"
            target_achievement = 100.0 if tier == "困难" else 100.5
            # A hard chart already above SSS is only a sensible SSS+ target
            # when the remaining gap is small and it can still add rating.
            if (
                tier == "困难"
                and achievement is not None
                and 100.0 <= achievement < 100.5
                and achievement >= 100.3
            ):
                target_achievement = 100.5
            if achievement is not None and achievement >= target_achievement:
                continue
            target_ra = rating_for(chart.constant, target_achievement)
            rating_gain = max(0, target_ra - (current_ra if in_best else bottom_ra))
            distance = abs(chart.constant - target_constant)
            upper_penalty = max(0.0, fitted - target_constant) * 0.35
            if achievement is None:
                played_penalty = 0.16
            elif achievement >= 97.0:
                played_penalty = 0.0
            elif achievement >= 94.0:
                played_penalty = 0.08
            else:
                played_penalty = 0.22
            near_target_bonus = (
                max(0.0, min(1.5, achievement - 99.0)) * 0.16
                if achievement is not None
                else 0.0
            )
            gain_bonus = min(20, rating_gain) * 0.055
            fit_ease_bonus = max(-1.0, min(1.0, chart.constant - fitted)) * 0.6
            jitter = _stable_jitter(today, user_id, chart) * 0.08
            score = (
                distance
                + upper_penalty
                + played_penalty
                - near_target_bonus
                - gain_bonus
                - fit_ease_bonus
                + jitter
            )
            candidates.append(
                (
                    score,
                    chart,
                    record,
                    current_ra,
                    rating_gain,
                    pool,
                    in_best,
                    target_achievement,
                )
            )

    candidates.sort(key=lambda item: (item[0], item[1].constant, item[1].song_id))
    selected: list[RecommendationItem] = []
    used_songs: set[int] = set()
    quotas = {"简单": 3, "中等": 4, "困难": 3}

    def append_candidate(
        candidate: tuple[float, Chart, dict | None, int, int, str, bool, float], tier: str
    ) -> None:
        _, chart, record, current_ra, rating_gain, pool, in_best, target_achievement = candidate
        achievement = float(record.get("achievements", 0) or 0) if record else None
        selected.append(
            RecommendationItem(
                chart=chart,
                current_achievement=achievement,
                current_rating=current_ra,
                target_rating=rating_for(chart.constant, target_achievement),
                target_achievement=target_achievement,
                rating_gain=rating_gain,
                pool=pool,
                in_best=in_best,
                tier=tier,
            )
        )
        used_songs.add(chart.song_id)

    tiered: dict[
        str, list[tuple[float, Chart, dict | None, int, int, str, bool, float]]
    ] = {
        "简单": [],
        "中等": [],
        "困难": [],
    }
    for candidate in candidates:
        fitted = candidate[1].fit_constant or candidate[1].constant
        delta = fitted - target_constant
        tier = "简单" if delta <= -0.15 else "困难" if delta >= 0.18 else "中等"
        tiered[tier].append(candidate)
    for tier, quota in quotas.items():
        for candidate in tiered[tier]:
            if candidate[1].song_id in used_songs:
                continue
            append_candidate(candidate, tier)
            if sum(item.tier == tier for item in selected) >= quota:
                break
    if len(selected) < limit:
        for candidate in candidates:
            chart = candidate[1]
            if chart.song_id in used_songs:
                continue
            fitted = chart.fit_constant or chart.constant
            delta = fitted - target_constant
            tier = "简单" if delta <= -0.15 else "困难" if delta >= 0.18 else "中等"
            append_candidate(candidate, tier)
            if len(selected) >= limit:
                break

    return DailyRecommendations(
        day=today,
        player_name=player.nickname,
        player_rating=player.rating,
        target_constant=round(target_constant, 1),
        items=tuple(selected),
    )


def format_daily_recommendations(plan: DailyRecommendations) -> str:
    rows = [
        f"{plan.player_name} 的每日推歌｜{plan.day:%Y-%m-%d}",
        f"当前 DX Rating：{plan.player_rating}；能力拟合中心：{plan.target_constant:.1f}",
        "困难谱以 100.0000% 为主要目标，较容易谱面以 100.5000% 为目标；当天结果固定。",
    ]
    for index, item in enumerate(plan.items, 1):
        chart = item.chart
        current = (
            "未游玩"
            if item.current_achievement is None
            else f"当前 {item.current_achievement:.4f}% / RA {item.current_rating}"
        )
        position = "已在榜" if item.in_best else "按当前底分替换"
        fitted = (
            f"，水鱼拟合 {chart.fit_constant:.2f}"
            if chart.fit_constant is not None
            else ""
        )
        rows.append(
            f"\n{index}. [{item.tier}] {chart.title}｜{chart.type_label} "
            f"{chart.difficulty_name} {chart.level}（官定 {chart.constant:.1f}{fitted}）"
        )
        rows.append(
            f"{current}；{item.target_achievement:.4f}% 时单谱 RA {item.target_rating}；"
            f"预计进入 {item.pool} 后总 Rating +{item.gain}（{position}）"
        )
        rows.append(f"配置/难点：{item.pattern or '以公开物量画像为准'}")
    rows.append("\n配置标签来自 LXNS 完整 Simai Note 序列；缺谱时只采用公开物量，不猜手法。")
    return "\n".join(rows)


def _record_key(record: dict) -> tuple[int, str, int] | None:
    try:
        song_id = int(record.get("song_id", 0) or 0) % 10_000
        difficulty = int(record.get("level_index", 0) or 0)
    except (TypeError, ValueError):
        return None
    raw_type = str(record.get("type") or "standard").casefold()
    chart_type = "dx" if raw_type == "dx" else "standard"
    return song_id, chart_type, difficulty


def _records_by_key(records: list[dict]) -> dict[tuple[int, str, int], dict]:
    result: dict[tuple[int, str, int], dict] = {}
    for record in records:
        key = _record_key(record)
        if key is None:
            continue
        previous = result.get(key)
        if previous is None or float(record.get("achievements", 0) or 0) > float(
            previous.get("achievements", 0) or 0
        ):
            result[key] = record
    return result


def _bottom_rating(records: list[dict], capacity: int) -> int:
    if len(records) < capacity:
        return 0
    return min(int(item.get("ra", 0) or 0) for item in records)


def _stable_jitter(day: date, user_id: str, chart: Chart) -> float:
    payload = (
        f"{day.isoformat()}:{user_id}:{chart.song_id}:{chart.chart_type}:{chart.difficulty}"
    ).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big") / 2**32
