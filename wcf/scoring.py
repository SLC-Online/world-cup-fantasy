"""The single source of truth for FIFA World Cup 2026 Fantasy scoring.

If FIFA tweak the rules, change the numbers here and nowhere else. The expected
-points engine (``projections.py``) reads these same values, so the model and
the actual-points calculation can never drift apart.

Rules captured from play.fifa.com/fantasy/help/guidelines, cross-checked against
Fantasy Football Scout and Sportsdunia (June 2026).

KNOWN AMBIGUITY (flagged, low impact):
  * GK goal value: detailed tables list +9; a "one more than FPL" reading implies
    +7. Keepers essentially never score at a World Cup, so the optimizer is
    insensitive to this. Set to 9 here; change ``goal_points["GK"]`` if needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass(frozen=True)
class ScoringRules:
    # Appearance
    appearance_any: int = 1            # played at all (up to 60 mins)
    appearance_60: int = 1             # ADDITIONAL point for 60+ mins (=> 2 total)
    minutes_threshold: int = 60

    # Goals, by position
    goal_points: Dict[str, int] = field(default_factory=lambda: {
        "GK": 9, "DEF": 7, "MID": 6, "FWD": 5,   # see KNOWN AMBIGUITY for GK
    })
    direct_free_kick_bonus: int = 1    # extra point for a goal direct from a free kick

    assist_points: int = 3

    # Clean sheet (requires 60+ mins), by position
    clean_sheet_points: Dict[str, int] = field(default_factory=lambda: {
        "GK": 5, "DEF": 5, "MID": 1, "FWD": 0,
    })
    # Goals conceded (GK/DEF only): the FIRST is free, each ADDITIONAL costs 1.
    # i.e. penalty = -1 * max(0, conceded - 1). More punitive than FPL.
    goals_conceded_free: int = 1
    goals_conceded_penalty: int = -1

    # Goalkeeping
    save_unit: int = 3                 # +1 per this many saves
    save_unit_points: int = 1
    penalty_save_points: int = 3

    # Midfielder defensive / creative rewards
    tackle_unit: int = 3               # +1 per 3 tackles
    tackle_unit_points: int = 1
    chance_unit: int = 2               # +1 per 2 chances created
    chance_unit_points: int = 1

    # Forward
    shot_on_target_unit: int = 2       # +1 per 2 shots on target
    shot_on_target_unit_points: int = 1

    # Discipline & misc (all positions)
    yellow_card_points: int = -1
    red_card_points: int = -2
    own_goal_points: int = -2
    penalty_won_points: int = 2
    penalty_conceded_points: int = -1

    # Differential reward: +2 if a player scores >4 pts AND is owned by <5% of teams
    scouting_bonus_points: int = 2
    scouting_bonus_min_points: int = 4      # strictly greater than this
    scouting_bonus_max_ownership: float = 0.05


DEFAULT_RULES = ScoringRules()


@dataclass
class StatLine:
    """A player's actual stats for a single match (used to score real results)."""
    minutes: int = 0
    goals: int = 0
    assists: int = 0
    goals_conceded: int = 0
    saves: int = 0
    penalty_saves: int = 0
    tackles: int = 0
    chances_created: int = 0
    shots_on_target: int = 0
    direct_free_kick_goals: int = 0    # subset of `goals` that were direct FKs
    yellow_cards: int = 0
    red_cards: int = 0
    own_goals: int = 0
    penalties_won: int = 0
    penalties_conceded: int = 0


def score_statline(
    stat: StatLine,
    position: str,
    rules: ScoringRules = DEFAULT_RULES,
) -> Tuple[float, Dict[str, float]]:
    """Return (total_points, breakdown) for an actual performance.

    `breakdown` maps a category label to points so results are auditable.
    """
    if position not in ("GK", "DEF", "MID", "FWD"):
        raise ValueError(f"bad position {position!r}")

    b: Dict[str, float] = {}

    # Appearance
    if stat.minutes > 0:
        appearance = rules.appearance_any
        if stat.minutes >= rules.minutes_threshold:
            appearance += rules.appearance_60
        b["appearance"] = appearance

    # Goals (+ direct free-kick bonus)
    if stat.goals:
        b["goals"] = stat.goals * rules.goal_points[position]
    if stat.direct_free_kick_goals:
        b["direct_fk_bonus"] = stat.direct_free_kick_goals * rules.direct_free_kick_bonus

    # Assists
    if stat.assists:
        b["assists"] = stat.assists * rules.assist_points

    # Clean sheet (needs 60+ mins) and goals conceded (GK/DEF)
    played_60 = stat.minutes >= rules.minutes_threshold
    if position in ("GK", "DEF", "MID"):
        if played_60 and stat.goals_conceded == 0 and rules.clean_sheet_points[position]:
            b["clean_sheet"] = rules.clean_sheet_points[position]
    if position in ("GK", "DEF") and stat.goals_conceded > rules.goals_conceded_free:
        extra = stat.goals_conceded - rules.goals_conceded_free
        b["goals_conceded"] = extra * rules.goals_conceded_penalty

    # Goalkeeping
    if position == "GK":
        if stat.saves >= rules.save_unit:
            b["saves"] = (stat.saves // rules.save_unit) * rules.save_unit_points
        if stat.penalty_saves:
            b["penalty_saves"] = stat.penalty_saves * rules.penalty_save_points

    # Midfield defensive / creative
    if position == "MID":
        if stat.tackles >= rules.tackle_unit:
            b["tackles"] = (stat.tackles // rules.tackle_unit) * rules.tackle_unit_points
        if stat.chances_created >= rules.chance_unit:
            b["chances"] = (stat.chances_created // rules.chance_unit) * rules.chance_unit_points

    # Forward shooting
    if position == "FWD" and stat.shots_on_target >= rules.shot_on_target_unit:
        b["shots_on_target"] = (
            stat.shots_on_target // rules.shot_on_target_unit
        ) * rules.shot_on_target_unit_points

    # Discipline & misc
    if stat.yellow_cards:
        b["yellow_cards"] = stat.yellow_cards * rules.yellow_card_points
    if stat.red_cards:
        b["red_cards"] = stat.red_cards * rules.red_card_points
    if stat.own_goals:
        b["own_goals"] = stat.own_goals * rules.own_goal_points
    if stat.penalties_won:
        b["penalties_won"] = stat.penalties_won * rules.penalty_won_points
    if stat.penalties_conceded:
        b["penalties_conceded"] = stat.penalties_conceded * rules.penalty_conceded_points

    total = float(sum(b.values()))
    return total, b


def apply_scouting_bonus(
    base_points: float,
    ownership_fraction: float,
    rules: ScoringRules = DEFAULT_RULES,
) -> float:
    """Add the differential (+2) bonus if eligible. ownership_fraction in [0,1]."""
    if (base_points > rules.scouting_bonus_min_points
            and ownership_fraction < rules.scouting_bonus_max_ownership):
        return base_points + rules.scouting_bonus_points
    return base_points
