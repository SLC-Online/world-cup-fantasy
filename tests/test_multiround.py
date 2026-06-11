"""Tests for the multi-round (horizon) projections + optimizer."""
import json

import pytest

from wcf import config
from wcf.multiround import build_horizon_projections
from wcf.optimizer import optimize_horizon
from wcf.providers import lineups as lineups_provider
from wcf.providers import players as players_provider


@pytest.fixture(scope="module")
def horizon():
    players = players_provider.load_players(config.SAMPLE_PLAYERS_FILE)
    matches = json.loads((config.FIXTURES_DIR / "odds_sample.json").read_text())
    lns = lineups_provider.load_lineups(
        "MD1", path=config.LINEUPS_DIR / "lineups_sample.csv")
    return build_horizon_projections(players, matches, lns, horizon=2)


def test_horizon_projection_shape(horizon):
    assert horizon.round_labels == ["MD1", "MD2"]
    # every player has an EP entry per round
    assert all(len(v) == 2 for v in horizon.ep.values())


def test_horizon_optimizer_valid(horizon):
    squad, plan = optimize_horizon(
        horizon.meta, horizon.ep, horizon.round_labels,
        budget=100.0, nation_limit=3)

    # Squad composition
    assert len(squad) == config.SQUAD_SIZE
    comp = {k: 0 for k in config.POSITIONS}
    nat = {}
    cost = 0.0
    for pid in squad:
        m = horizon.meta[pid]
        comp[m["position"]] += 1
        nat[m["nation"]] = nat.get(m["nation"], 0) + 1
        cost += m["price"]
    assert comp == config.SQUAD_COMPOSITION
    assert cost <= 100.0 + 1e-6
    assert max(nat.values()) <= 3

    # One plan entry per round, each a legal XI with a captain in it
    assert len(plan) == 2
    prev_squad = None
    for pr in plan:
        assert len(pr["starters"]) == config.STARTING_XI
        assert pr["captain"] in pr["starters"]
        assert pr["vice"] in pr["starters"] and pr["vice"] != pr["captain"]
        # Each round's XI comes from THAT round's squad (transfers allowed).
        assert set(pr["starters"]).issubset(set(pr["squad"]))
        assert len(pr["squad"]) == config.SQUAD_SIZE
        if prev_squad is not None:
            # At most `planned_transfers` (default 1) changes between rounds.
            assert len(set(pr["squad"]) - prev_squad) <= 1
        prev_squad = set(pr["squad"])
        xi_pos = {k: 0 for k in config.POSITIONS}
        for pid in pr["starters"]:
            xi_pos[horizon.meta[pid]["position"]] += 1
        for pos, (lo, hi) in config.FORMATION_LIMITS.items():
            assert lo <= xi_pos[pos] <= hi
