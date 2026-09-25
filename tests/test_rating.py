from maimai_bot.services.rating import minimum_achievement, rating_for


def test_rating_uses_official_threshold_coefficients() -> None:
    assert rating_for(14.7, 100.5) == 330
    assert rating_for(14.7, 100.0) == 317
    assert rating_for(14.7, 99.5) == 308


def test_minimum_achievement_reaches_requested_rating() -> None:
    achievement = minimum_achievement(14.7, 318)
    assert achievement is not None
    assert rating_for(14.7, achievement) >= 318
    assert rating_for(14.7, achievement - 0.0001) < 318


def test_minimum_achievement_reports_impossible_target() -> None:
    assert minimum_achievement(10.0, 999) is None
