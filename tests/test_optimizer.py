"""Tests for the optimizer: constraints must always hold."""
import json

import pytest

from wcf import config
from wcf.optimizer import optimize_squad
from wcf.projections import build_projections
from wcf.providers import lineups as lineups_provider
from wcf.providers import players as players_provider


@pytest.fixture(scope="module")
def projections():
    players = players_provider.load_players(config.SAMPLE_PLAYERS_FILE)
    matches = json.loads((config.FIXTURES_DIR / "odds_sample.json").read_text())
    lns = lineups_provider.load_lineups(
        "MD1", path=config.LINEUPS_DIR / "lineups_sample.csv")
    return build_projections(players, matches, lns)


def _assert_valid(sel, projections, budget=100.0, nation_limit=3):
    by = {p.player_id: p for p in projections}
    # Squad size + composition
    assert len(sel.squad) == config.SQUAD_SIZE
    comp = {k: 0 for k in config.POSITIONS}
    for pid in sel.squad:
        comp[by[pid].position] += 1
    assert comp == config.SQUAD_COMPOSITION
    # Budget
    assert sum(by[pid].price for pid in sel.squad) <= budget + 1e-6
    # Nation limit
    nat = {}
    for pid in sel.squad:
        nat[by[pid].nation] = nat.get(by[pid].nation, 0) + 1
    assert max(nat.values()) <= nation_limit
    # Starting XI
    assert len(sel.starters) == config.STARTING_XI
    assert set(sel.starters).issubset(set(sel.squad))
    assert len(sel.bench) == 4
    assert set(sel.bench).isdisjoint(set(sel.starters))
    # Formation ranges + exactly 1 GK
    xi_pos = {k: 0 for k in config.POSITIONS}
    for pid in sel.starters:
        xi_pos[by[pid].position] += 1
    for pos, (lo, hi) in config.FORMATION_LIMITS.items():
        assert lo <= xi_pos[pos] <= hi
    # Captain / vice are distinct starters
    assert sel.captain in sel.starters
    assert sel.vice in sel.starters
    assert sel.captain != sel.vice


def test_ilp_optimal_squad_valid(projections):
    sel = optimize_squad(projections, budget=100.0, nation_limit=3)
    _assert_valid(sel, projections)
    assert sel.expected_points > 0


def test_heuristic_squad_valid(projections):
    sel = optimize_squad(projections, budget=100.0, nation_limit=3,
                         force_heuristic=True)
    _assert_valid(sel, projections)


def test_ilp_beats_or_matches_heuristic(projections):
    ilp = optimize_squad(projections, budget=100.0, nation_limit=3)
    heur = optimize_squad(projections, budget=100.0, nation_limit=3,
                          force_heuristic=True)
    # The exact optimum must be >= a greedy solution.
    assert ilp.expected_points >= heur.expected_points - 1e-6


def test_loosening_budget_never_hurts(projections):
    base = optimize_squad(projections, budget=100.0, nation_limit=3)
    loose = optimize_squad(projections, budget=150.0, nation_limit=3)
    _assert_valid(loose, projections, budget=150.0)
    # A bigger budget is a superset of options, so EP can only rise or hold.
    assert loose.expected_points >= base.expected_points - 1e-6


def test_impossible_budget_raises(projections):
    # No valid 15-man squad can cost under $40m, so this must fail loudly.
    with pytest.raises(RuntimeError):
        optimize_squad(projections, budget=40.0, nation_limit=3)


def test_transfer_hits_applied(projections):
    base = optimize_squad(projections, budget=100.0, nation_limit=3)
    # Force transfers from a deliberately weak existing squad (cheapest 15 by id).
    cheap = sorted(projections, key=lambda p: p.price)
    existing = []
    comp = {k: 0 for k in config.POSITIONS}
    for p in cheap:
        if comp[p.position] < config.SQUAD_COMPOSITION[p.position]:
            existing.append(p.player_id)
            comp[p.position] += 1
        if len(existing) == config.SQUAD_SIZE:
            break
    sel = optimize_squad(projections, budget=100.0, nation_limit=3,
                         existing_squad=existing, free_transfers=1)
    _assert_valid(sel, projections)
    assert sel.hit_points == max(0, sel.transfers_made - 1) * config.TRANSFER_HIT_COST
