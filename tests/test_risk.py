"""Tests for the risk-adjusted captaincy metric."""
from wcf.risk import captain_score


def test_equal_ceiling_prefers_higher_mean():
    """With the same ceiling, the higher-mean (higher-floor) captain must win —
    the old mean+0.5*std metric wrongly preferred the higher-variance player."""
    vini = {"mean": 5.6, "std": 4.0, "ceiling": 9.5, "p_haul": 0.17}
    yamal = {"mean": 5.8, "std": 3.2, "ceiling": 9.5, "p_haul": 0.22}
    assert captain_score(yamal, "balanced") > captain_score(vini, "balanced")


def test_upside_appetite_rewards_fat_tail():
    safe = {"mean": 6.0, "std": 1.0, "ceiling": 7.0, "p_haul": 0.05}
    boom = {"mean": 6.0, "std": 5.0, "ceiling": 12.0, "p_haul": 0.30}
    assert captain_score(boom, "upside") > captain_score(safe, "upside")


def test_safe_appetite_is_pure_mean():
    a = {"mean": 7.0, "std": 0.5, "ceiling": 8.0, "p_haul": 0.10}
    b = {"mean": 6.0, "std": 9.0, "ceiling": 20.0, "p_haul": 0.40}
    assert captain_score(a, "safe") > captain_score(b, "safe")
