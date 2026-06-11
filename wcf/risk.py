"""Outcome distributions for risk-adjusted decisions (mainly captaincy).

Expected points is a mean. A captain is a single doubled bet, so its *ceiling*
matters more than its mean. We reconstruct each player's full points distribution
from the projection components — goals ~ Poisson, assists ~ Poisson, clean sheet
~ Bernoulli — and report mean, standard deviation and an 85th-percentile ceiling.

This answers "is captaining a nailed keeper better than Haaland?" with numbers:
the keeper's mean may be close but its ceiling is far lower.
"""
from __future__ import annotations

from typing import Dict

from .odds_math import poisson_pmf
from .scoring import DEFAULT_RULES, ScoringRules

_GOAL_MAX = 8
_ASSIST_MAX = 4


def point_distribution(position: str, components: Dict[str, float],
                       rules: ScoringRules = DEFAULT_RULES) -> Dict[str, float]:
    """Distribution stats for a player in one fixture, from their EP components.

    Goals/assists/clean-sheet are treated as random; the remaining components
    (appearance, conceded, saves, bonus, cards) are folded in at their mean,
    since they contribute little variance.
    """
    gv = rules.goal_points[position]
    cs_pts = rules.clean_sheet_points.get(position, 0)
    goals_comp = components.get("goals", 0.0)
    assists_comp = components.get("assists", 0.0)
    cs_comp = components.get("clean_sheet", 0.0)

    lam_g = goals_comp / gv if gv else 0.0
    lam_a = assists_comp / rules.assist_points if rules.assist_points else 0.0
    p_cs = min(max(cs_comp / cs_pts, 0.0), 1.0) if cs_pts else 0.0

    ep = sum(components.values())
    const = ep - goals_comp - assists_comp - cs_comp   # deterministic remainder

    cs_states = ((1, p_cs), (0, 1.0 - p_cs)) if cs_pts else ((0, 1.0),)
    outcomes = []
    for g in range(_GOAL_MAX + 1):
        pg = poisson_pmf(g, lam_g)
        if pg < 1e-9 and g > 0:
            continue
        for a in range(_ASSIST_MAX + 1):
            pa = poisson_pmf(a, lam_a)
            if pa < 1e-9 and a > 0:
                continue
            for cs, pcs in cs_states:
                pts = const + gv * g + rules.assist_points * a + cs_pts * cs
                outcomes.append((pts, pg * pa * pcs))

    total = sum(p for _, p in outcomes) or 1.0
    outcomes = [(v, p / total) for v, p in outcomes]

    mean = sum(v * p for v, p in outcomes)
    var = sum((v - mean) ** 2 * p for v, p in outcomes)
    std = var ** 0.5

    ordered = sorted(outcomes)

    def percentile(q: float) -> float:
        cum = 0.0
        for v, p in ordered:
            cum += p
            if cum >= q:
                return v
        return ordered[-1][0]

    return {
        "mean": round(mean, 2),
        "std": round(std, 2),
        "floor": round(percentile(0.15), 1),
        "ceiling": round(percentile(0.85), 1),
        "p_haul": round(sum(p for v, p in outcomes if v >= 9), 3),  # P(>=9 pts)
    }


# Risk appetite -> how much weight to put on UPSIDE when captaining.
# We deliberately reward the upper tail (ceiling - mean), NOT standard deviation:
# std is symmetric and would credit a player for downside volatility too. A
# captain is a one-way bet on the upside, so two players with the same ceiling
# should be split by their mean (the higher floor wins), and a fatter upper tail
# is rewarded. A small P(haul) term breaks ties toward genuine ceiling.
RISK_K = {"safe": 0.0, "balanced": 0.5, "upside": 1.0}


def captain_score(stats: Dict[str, float], risk: str = "balanced") -> float:
    k = RISK_K.get(risk, 0.5)
    upside = max(0.0, stats.get("ceiling", stats["mean"]) - stats["mean"])
    return stats["mean"] + k * (upside + 2.0 * stats.get("p_haul", 0.0))
