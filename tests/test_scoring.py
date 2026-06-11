"""Tests for the scoring rules (the single source of truth)."""
from wcf.scoring import DEFAULT_RULES as R
from wcf.scoring import StatLine, score_statline, apply_scouting_bonus


def pts(stat, pos):
    return score_statline(stat, pos)[0]


def test_appearance_thresholds():
    # goals_conceded=1 avoids a clean sheet confounding the appearance points.
    assert pts(StatLine(minutes=0, goals_conceded=1), "MID") == 0
    assert pts(StatLine(minutes=45, goals_conceded=1), "MID") == 1      # played, under 60
    assert pts(StatLine(minutes=60, goals_conceded=1), "MID") == 2      # 60+
    assert pts(StatLine(minutes=90, goals_conceded=1), "MID") == 2


def test_goal_values_by_position():
    # 90 mins (=2) + one goal. Use goals_conceded=1 so no clean sheet is added
    # (the first concede is free, so there's no concede penalty either).
    assert pts(StatLine(minutes=90, goals=1, goals_conceded=1), "FWD") == 2 + 5
    assert pts(StatLine(minutes=90, goals=1, goals_conceded=1), "MID") == 2 + 6
    assert pts(StatLine(minutes=90, goals=1, goals_conceded=1), "DEF") == 2 + 7
    assert pts(StatLine(minutes=90, goals=1, goals_conceded=1), "GK") == 2 + 9


def test_clean_sheet_requires_60_and_zero_conceded():
    assert pts(StatLine(minutes=90, goals_conceded=0), "DEF") == 2 + 5
    assert pts(StatLine(minutes=59, goals_conceded=0), "DEF") == 1      # under 60, no CS
    assert pts(StatLine(minutes=90, goals_conceded=1), "DEF") == 2      # conceded -> no CS, first free
    assert pts(StatLine(minutes=90, goals_conceded=0), "MID") == 2 + 1  # mid CS = +1
    assert pts(StatLine(minutes=90, goals_conceded=0), "FWD") == 2      # no CS for fwd


def test_goals_conceded_first_is_free():
    # GK plays 90, concedes 3 -> -1 for each after the first => -2, plus appearance 2
    assert pts(StatLine(minutes=90, goals_conceded=3), "GK") == 2 - 2
    assert pts(StatLine(minutes=90, goals_conceded=1), "GK") == 2        # first free
    # Outfield non GK/DEF not penalised for concedes
    assert pts(StatLine(minutes=90, goals_conceded=4), "MID") == 2


def test_gk_saves_and_pen_save():
    assert pts(StatLine(minutes=90, saves=3), "GK") == 2 + 5 + 1          # CS too (0 conceded)
    assert pts(StatLine(minutes=90, saves=5), "GK") == 2 + 5 + 1          # 5//3 = 1
    assert pts(StatLine(minutes=90, saves=6), "GK") == 2 + 5 + 2
    assert pts(StatLine(minutes=90, penalty_saves=1, goals_conceded=1), "GK") == 2 + 3


def test_mid_tackles_chances_and_fwd_shots():
    assert pts(StatLine(minutes=90, tackles=3), "MID") == 2 + 1 + 1       # 3 tackles, +CS(0 conc)? MID CS needs 0 conceded -> yes +1
    assert pts(StatLine(minutes=90, chances_created=4), "MID") == 2 + 2 + 1
    assert pts(StatLine(minutes=90, shots_on_target=5), "FWD") == 2 + 2   # 5//2 = 2


def test_discipline_and_misc():
    assert pts(StatLine(minutes=90, yellow_cards=1), "FWD") == 2 - 1
    assert pts(StatLine(minutes=90, red_cards=1), "FWD") == 2 - 2
    assert pts(StatLine(minutes=90, own_goals=1), "DEF") == 2 + 5 - 2
    assert pts(StatLine(minutes=90, penalties_won=1), "FWD") == 2 + 2
    assert pts(StatLine(minutes=90, penalties_conceded=1), "DEF") == 2 + 5 - 1


def test_direct_free_kick_bonus():
    # A direct-FK goal scores the goal value plus +1 bonus
    base = pts(StatLine(minutes=90, goals=1), "MID")
    fk = pts(StatLine(minutes=90, goals=1, direct_free_kick_goals=1), "MID")
    assert fk == base + 1


def test_scouting_bonus():
    assert apply_scouting_bonus(6, 0.03) == 8     # >4 pts and <5% owned
    assert apply_scouting_bonus(6, 0.20) == 6     # too highly owned
    assert apply_scouting_bonus(4, 0.01) == 4     # not strictly > 4
