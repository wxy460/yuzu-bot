from __future__ import annotations

import math


def rating_for(constant: float, achievement: float) -> int:
    if constant <= 0 or achievement < 0:
        raise ValueError("定数必须大于 0，达成率不能为负数")
    capped = min(achievement, 100.5)
    return math.floor(constant * capped / 100 * _coefficient(capped))


def minimum_achievement(constant: float, target_rating: int) -> float | None:
    """Return the first 4-decimal achievement that reaches target RA."""
    if constant <= 0 or target_rating < 0:
        raise ValueError("定数必须大于 0，目标 Rating 不能为负数")
    if rating_for(constant, 100.5) < target_rating:
        return None
    low, high = 0, 1_005_000  # achievement in 0.0001% units
    while low < high:
        middle = (low + high) // 2
        if rating_for(constant, middle / 10_000) >= target_rating:
            high = middle
        else:
            low = middle + 1
    return low / 10_000


def _coefficient(achievement: float) -> float:
    thresholds = (
        (100.5, 22.4),
        (100.0, 21.6),
        (99.5, 21.1),
        (99.0, 20.8),
        (98.0, 20.3),
        (97.0, 20.0),
        (94.0, 16.8),
        (90.0, 15.2),
        (80.0, 13.6),
        (75.0, 12.0),
        (70.0, 11.2),
        (60.0, 9.6),
        (50.0, 8.0),
        (0.0, 7.0),
    )
    return next(coefficient for threshold, coefficient in thresholds if achievement >= threshold)
