"""Turn bookmaker odds into the quantities the points model needs.

The betting market already prices in form, injuries, expected line-ups and venue,
so it is the single best low-effort signal. We extract two things:

  1. Each team's expected goals in a match (mu_home, mu_away), from the 1X2 and
     over/under markets, using an independent-Poisson match model.
  2. Each player's expected goals (lambda), from anytime-goalscorer prices,
     rescaled so a team's player goals are consistent with (1).

No SciPy: all maths is closed-form Poisson plus a couple of bisections.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

MAX_GOALS = 12          # truncate Poisson grids here; P(>12 goals) is negligible
_MIN_MU = 0.05          # floor so a team always has a sliver of goal threat


# --------------------------------------------------------------------------- #
# Basic probability helpers
# --------------------------------------------------------------------------- #
def implied_prob(decimal_odds: float) -> float:
    """1 / decimal odds. Returns 0 for non-positive input."""
    return 1.0 / decimal_odds if decimal_odds and decimal_odds > 0 else 0.0


def normalize(values: Sequence[float]) -> List[float]:
    total = float(sum(values))
    if total <= 0:
        return [0.0 for _ in values]
    return [v / total for v in values]


def devig_1x2(home_odds: float, draw_odds: float, away_odds: float) -> Tuple[float, float, float]:
    """Remove the bookmaker margin from a 1X2 market (proportional method)."""
    raw = [implied_prob(home_odds), implied_prob(draw_odds), implied_prob(away_odds)]
    p = normalize(raw)
    return p[0], p[1], p[2]


def devig_two_way(yes_odds: float, no_odds: float) -> float:
    """Return the no-vig probability of 'yes' given both sides' odds."""
    py, pn = implied_prob(yes_odds), implied_prob(no_odds)
    if py + pn <= 0:
        return 0.0
    return py / (py + pn)


# --------------------------------------------------------------------------- #
# Poisson machinery
# --------------------------------------------------------------------------- #
def poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mu) * mu ** k / math.factorial(k)


def _goal_probs(mu: float, max_goals: int = MAX_GOALS) -> List[float]:
    """P(team scores exactly g) for g in 0..max_goals (tail folded into last)."""
    probs = [poisson_pmf(g, mu) for g in range(max_goals)]
    probs.append(max(0.0, 1.0 - sum(probs)))   # P(>= max_goals)
    return probs


def outcome_probs(mu_home: float, mu_away: float,
                  max_goals: int = MAX_GOALS) -> Tuple[float, float, float]:
    """(P_home_win, P_draw, P_away_win) under independent Poisson scorelines."""
    ph = _goal_probs(mu_home, max_goals)
    pa = _goal_probs(mu_away, max_goals)
    p_home = p_draw = p_away = 0.0
    for i, pi in enumerate(ph):
        for j, pj in enumerate(pa):
            joint = pi * pj
            if i > j:
                p_home += joint
            elif i == j:
                p_draw += joint
            else:
                p_away += joint
    return p_home, p_draw, p_away


def prob_total_over(mu_total: float, line: float, max_goals: int = MAX_GOALS) -> float:
    """P(total goals > line) for an integer/half line under Poisson(mu_total)."""
    probs = _goal_probs(mu_total, max_goals * 2)
    # 'over 2.5' => 3 or more goals.
    threshold = math.floor(line) + 1
    return sum(p for n, p in enumerate(probs) if n >= threshold)


# --------------------------------------------------------------------------- #
# Solving for expected goals
# --------------------------------------------------------------------------- #
def solve_mu_total(over_odds: float, under_odds: float, line: float = 2.5) -> float:
    """Infer total expected goals from an over/under market via bisection."""
    target = devig_two_way(over_odds, under_odds)        # no-vig P(over)
    if target <= 0:
        return 2.6
    lo, hi = 0.2, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if prob_total_over(mid, line) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def solve_supremacy(mu_total: float, p_home: float) -> float:
    """Find supremacy s (= mu_home - mu_away) so the model's P(home win) matches.

    P(home win) increases monotonically with s, so we bisect on s in
    (-mu_total, +mu_total).
    """
    lo, hi = -mu_total + 1e-6, mu_total - 1e-6
    for _ in range(60):
        s = (lo + hi) / 2
        mh, ma = (mu_total + s) / 2, (mu_total - s) / 2
        ph, _, _ = outcome_probs(mh, ma)
        if ph < p_home:
            lo = s
        else:
            hi = s
    return (lo + hi) / 2


def match_goals_from_odds(
    home_odds: float,
    draw_odds: float,
    away_odds: float,
    over_odds: Optional[float] = None,
    under_odds: Optional[float] = None,
    total_line: float = 2.5,
    default_total: float = 2.6,
) -> Tuple[float, float]:
    """Return (mu_home, mu_away) from a match's 1X2 (+ optional totals) market."""
    p_home, _, _ = devig_1x2(home_odds, draw_odds, away_odds)
    if over_odds and under_odds:
        mu_total = solve_mu_total(over_odds, under_odds, total_line)
    else:
        mu_total = default_total
    s = solve_supremacy(mu_total, p_home)
    mu_home = max(_MIN_MU, (mu_total + s) / 2)
    mu_away = max(_MIN_MU, (mu_total - s) / 2)
    return mu_home, mu_away


# --------------------------------------------------------------------------- #
# Player-level goal rate
# --------------------------------------------------------------------------- #
def lambda_from_anytime(p_anytime: float) -> float:
    """Expected goals for a player from P(scores at least one).

    If goals ~ Poisson(lambda), then P(>=1) = 1 - e^-lambda, so
    lambda = -ln(1 - P(>=1)).
    """
    p = min(max(p_anytime, 1e-4), 0.95)
    return -math.log(1.0 - p)


def scale_player_lambdas(
    raw_lambda: Dict[str, float],
    team_mu: float,
) -> Dict[str, float]:
    """Rescale a team's player goal rates so they sum to the team's match mu.

    This simultaneously removes the goalscorer market's overround and ties player
    goal expectations to the match model. If we have no priced players for a team
    the dict is returned unchanged.
    """
    total = sum(raw_lambda.values())
    if total <= 0:
        return dict(raw_lambda)
    factor = team_mu / total
    return {pid: lam * factor for pid, lam in raw_lambda.items()}


def prob_clean_sheet(mu_against: float) -> float:
    """P(opponent scores 0) = e^(-mu_against)."""
    return math.exp(-max(0.0, mu_against))


def expected_conceded_penalty_units(mu_against: float) -> float:
    """E[max(0, goalsConceded - 1)] for a Poisson(mu_against) opponent.

    Equals mu - (1 - P(0)) = mu - P(>=1). Multiply by the (negative) per-goal
    penalty to get expected concede points for a GK/DEF.
    """
    p_at_least_one = 1.0 - prob_clean_sheet(mu_against)
    return max(0.0, mu_against - p_at_least_one)
