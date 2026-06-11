"""Multi-round (group-stage) projections.

The 15-man squad mostly persists through the group stage (only ~2 transfers a
round), so a strong squad is one that can field a good XI across MD1-MD3, not
just MD1. We project every player for each of their group games.

Goal rates for MD2/MD3 reuse each player's MD1 *share* of their team's goals
(a stable "who's the threat" signal from the bookmakers) applied to that
fixture's expected goals — so no extra odds calls are needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from . import projections as proj
from .models import Player
from .providers.lineups import Lineups
from .scoring import DEFAULT_RULES, ScoringRules


@dataclass
class HorizonProjections:
    round_labels: List[str]
    meta: Dict[str, dict]                       # pid -> {name,nation,position,price}
    ep: Dict[str, List[float]]                  # pid -> [ep per round]
    opponents: Dict[str, List[str]]             # pid -> [opponent per round]
    components: Dict[str, List[dict]]           # pid -> [components per round]

    def horizon_value(self, pid: str, discounts: List[float]) -> float:
        return sum(d * e for d, e in zip(discounts, self.ep.get(pid, [])))


def _nation_ordered_fixtures(matches, match_goals) -> Dict[str, list]:
    """nation -> its group fixtures (with goals) ordered by kickoff time."""
    by_nation: Dict[str, list] = {}
    for m in matches:
        if m["match_id"] not in match_goals:
            continue
        for nation in (m["home"], m["away"]):
            by_nation.setdefault(nation, []).append(m)
    for nation, ms in by_nation.items():
        ms.sort(key=lambda x: x.get("commence_time", ""))
    return by_nation


def build_horizon_projections(
    players: List[Player],
    matches: List[dict],
    lineups: Lineups,
    horizon: int = 3,
    rules: ScoringRules = DEFAULT_RULES,
    pconfig: proj.ProjectionConfig = proj.DEFAULT_PCONFIG,
    set_piece_roles=None,
) -> HorizonProjections:
    set_piece_roles = set_piece_roles or {}
    match_goals = proj.compute_match_goals(matches)
    nation_match = proj._nation_next_match(matches)          # each nation's MD1
    fixtures_by_nation = _nation_ordered_fixtures(matches, match_goals)

    players_by_nation: Dict[str, List[Player]] = {}
    for p in players:
        players_by_nation.setdefault(p.nation, []).append(p)

    # MD1 goal allocation -> per-player share of the team's expected goals.
    lam_md1 = proj._allocate_team_goals(
        players_by_nation, nation_match, match_goals, lineups, pconfig)
    priced = proj.market_priced_ids(players_by_nation, nation_match)
    share: Dict[str, float] = {}
    for p in players:
        m = nation_match.get(p.nation)
        if m and m["match_id"] in match_goals:
            team_mu = match_goals[m["match_id"]].mu_for(p.nation)
            share[p.id] = (lam_md1.get(p.id, 0.0) / team_mu) if team_mu > 0 else 0.0
        else:
            share[p.id] = 0.0

    labels_all = ["MD1", "MD2", "MD3"]
    n_rounds = horizon
    round_labels = labels_all[:n_rounds]

    meta: Dict[str, dict] = {}
    ep: Dict[str, List[float]] = {}
    opponents: Dict[str, List[str]] = {}
    components: Dict[str, List[dict]] = {}

    for p in players:
        meta[p.id] = {"name": p.name, "nation": p.nation,
                      "position": p.position, "price": p.price}
        ep[p.id] = [0.0] * n_rounds
        opponents[p.id] = [""] * n_rounds
        components[p.id] = [{} for _ in range(n_rounds)]

    ln_cache = {p.id: lineups.for_player(p.id) for p in players}

    # Per nation, per round: distribute team goals/assists then score each player.
    for nation, roster in players_by_nation.items():
        fixtures = fixtures_by_nation.get(nation, [])
        for r in range(n_rounds):
            if r >= len(fixtures):
                continue
            m = fixtures[r]
            mg = match_goals[m["match_id"]]
            mu_for = mg.mu_for(nation)
            mu_against = mg.mu_against(nation)
            opponent = mg.opponent_of(nation)

            # Assist pool for this fixture, weighted by position + goal threat.
            team_assists = mu_for * pconfig.assisted_fraction
            weights = {}
            for q in roster:
                lam_q = share[q.id] * mu_for
                weights[q.id] = max(0.0, pconfig.assist_weight[q.position]
                                    + pconfig.assist_threat_blend * lam_q)
            wsum = sum(weights.values()) or 1.0

            for q in roster:
                lam_q = share[q.id] * mu_for
                assist_q = team_assists * weights[q.id] / wsum
                comp = proj.compute_components(
                    position=q.position, mu_against=mu_against,
                    lam_goal=lam_q, exp_assist=assist_q,
                    p_start=ln_cache[q.id]["p_start"],
                    exp_minutes=ln_cache[q.id]["exp_minutes"],
                    rules=rules, pconfig=pconfig,
                    team_mu=mu_for, roles=set_piece_roles.get(q.id),
                    has_market_goals=q.id in priced)
                ep[q.id][r] = float(sum(comp.values()))
                opponents[q.id][r] = opponent
                components[q.id][r] = {k: round(v, 3) for k, v in comp.items()}

    return HorizonProjections(round_labels, meta, ep, opponents, components)
