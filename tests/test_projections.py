"""Tests for the expected-points engine using the bundled sample data."""
import json

import pytest

from wcf import config
from wcf.projections import build_projections, compute_match_goals
from wcf.providers import lineups as lineups_provider
from wcf.providers import players as players_provider


@pytest.fixture(scope="module")
def sample():
    players = players_provider.load_players(config.SAMPLE_PLAYERS_FILE)
    matches = json.loads((config.FIXTURES_DIR / "odds_sample.json").read_text())
    lns = lineups_provider.load_lineups(
        "MD1", path=config.LINEUPS_DIR / "lineups_sample.csv")
    projs = build_projections(players, matches, lns)
    return players, matches, projs


def test_projection_for_every_player(sample):
    players, _, projs = sample
    assert len(projs) == len(players)
    assert all(p.exp_points == p.exp_points for p in projs)   # not NaN


def test_match_goals_positive(sample):
    _, matches, _ = sample
    mg = compute_match_goals(matches)
    assert len(mg) == len(matches)
    for m in mg.values():
        assert m.mu_home > 0 and m.mu_away > 0
        # Stronger home side (Brazil v Japan etc.) should not be absurd
        assert m.mu_home < 5 and m.mu_away < 5


def test_team_goal_allocation_consistent(sample):
    """Raw per-player goal rates for a team sum to its match expected goals.
    (Components are now gated by P(plays); this checks the pre-gating allocation.)"""
    players, matches, projs = sample
    from wcf import projections as proj
    from wcf.providers import lineups as lineups_provider
    lns = lineups_provider.load_lineups(
        "MD1", path=config.LINEUPS_DIR / "lineups_sample.csv")
    mg = proj.compute_match_goals(matches)
    nation_match = proj._nation_next_match(matches)
    by_nation = {}
    for p in players:
        by_nation.setdefault(p.nation, []).append(p)
    lam = proj._allocate_team_goals(by_nation, nation_match, mg, lns, proj.DEFAULT_PCONFIG)

    nat_of = {p.id: p.nation for p in players}
    brazil_lam = sum(v for pid, v in lam.items() if nat_of[pid] == "Brazil")
    brazil_match = next(m for m in matches if "Brazil" in (m["home"], m["away"]))
    mu_brazil = mg[brazil_match["match_id"]].mu_for("Brazil")
    assert brazil_lam == pytest.approx(mu_brazil, abs=0.25)


def test_attackers_outscore_defenders_on_strong_teams(sample):
    _, _, projs = sample
    by = {p.player_id: p for p in projs}
    fwd_strong = max((p for p in projs if p.position == "FWD" and p.nation == "Brazil"),
                     key=lambda p: p.exp_points)
    weak_def = min((p for p in projs if p.position == "DEF"),
                   key=lambda p: p.exp_points)
    assert fwd_strong.exp_points > weak_def.exp_points
