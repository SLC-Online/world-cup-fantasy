"""Tests for the odds -> expected-goals maths."""
import math

from wcf import odds_math as om


def test_implied_and_devig_1x2_sums_to_one():
    p = om.devig_1x2(2.0, 3.5, 4.0)
    assert abs(sum(p) - 1.0) < 1e-9
    assert all(0 < x < 1 for x in p)


def test_devig_two_way():
    # Symmetric two-way market -> 0.5
    assert abs(om.devig_two_way(1.9, 1.9) - 0.5) < 1e-9


def test_solve_mu_total_inverts_prob_over():
    for mu in (1.8, 2.6, 3.4):
        p_over = om.prob_total_over(mu, 2.5)
        over_odds = 1.0 / p_over
        under_odds = 1.0 / (1 - p_over)
        recovered = om.solve_mu_total(over_odds, under_odds, 2.5)
        assert abs(recovered - mu) < 0.05


def test_match_goals_recovers_supremacy():
    mu_h, mu_a = 2.0, 1.0
    ph, pd, pa = om.outcome_probs(mu_h, mu_a)
    p_over = om.prob_total_over(mu_h + mu_a, 2.5)
    rh, ra = om.match_goals_from_odds(
        1 / ph, 1 / pd, 1 / pa, over_odds=1 / p_over, under_odds=1 / (1 - p_over))
    assert abs(rh - mu_h) < 0.1
    assert abs(ra - mu_a) < 0.1


def test_lambda_from_anytime_roundtrip():
    for lam in (0.2, 0.5, 1.0):
        p = 1 - math.exp(-lam)
        assert abs(om.lambda_from_anytime(p) - lam) < 1e-6


def test_clean_sheet_and_conceded_penalty():
    # Higher opponent xG -> lower clean-sheet prob, larger concede penalty units
    assert om.prob_clean_sheet(0.5) > om.prob_clean_sheet(2.0)
    assert abs(om.prob_clean_sheet(0.0) - 1.0) < 1e-9
    u_low = om.expected_conceded_penalty_units(0.5)
    u_high = om.expected_conceded_penalty_units(2.0)
    assert u_high > u_low >= 0


def test_scale_player_lambdas_matches_team_mu():
    raw = {"a": 0.3, "b": 0.2, "c": 0.1}
    scaled = om.scale_player_lambdas(raw, team_mu=1.8)
    assert abs(sum(scaled.values()) - 1.8) < 1e-9
