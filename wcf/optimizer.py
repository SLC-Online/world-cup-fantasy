"""Squad optimizer.

Picks the 15-man squad, starting XI and captain that maximise expected points
subject to the game's constraints, as an integer linear program (PuLP/CBC):

    squad:      2 GK, 5 DEF, 5 MID, 3 FWD, 15 total
    budget:     sum(price) <= round budget
    nation:     <= nation_limit players per country
    XI:         1 GK, 3-5 DEF, 3-5 MID, 1-3 FWD, 11 total (== legal formations)
    captain:    exactly one, must be a starter (scores double)
    transfers:  (transfer mode) each move beyond the free allowance costs 3 pts

A pure-Python heuristic fallback runs if PuLP isn't available.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from . import config
from .models import PlayerProjection, Selection

BENCH_WEIGHT = 0.10            # how much bench EP counts in the objective
DEFAULT_CAPTAIN_POSITIONS = ("MID", "FWD")   # captaincy chases ceiling, not mean


def _formation_string(starters_by_pos: Dict[str, int]) -> str:
    return f"{starters_by_pos['DEF']}-{starters_by_pos['MID']}-{starters_by_pos['FWD']}"


def optimize_squad(
    projections: Sequence[PlayerProjection],
    budget: float,
    nation_limit: int,
    existing_squad: Optional[Sequence[str]] = None,
    free_transfers: int = -1,
    bench_weight: float = BENCH_WEIGHT,
    captain_positions: Sequence[str] = DEFAULT_CAPTAIN_POSITIONS,
    force_heuristic: bool = False,
) -> Selection:
    pool = [p for p in projections if p.price > 0]
    cap_pos = set(captain_positions)
    if force_heuristic:
        return _heuristic(pool, budget, nation_limit, cap_pos)
    try:
        return _ilp(pool, budget, nation_limit, existing_squad,
                    free_transfers, bench_weight, cap_pos)
    except ImportError:
        print("[optimizer] PuLP not available — using heuristic fallback.")
        return _heuristic(pool, budget, nation_limit, cap_pos)


# --------------------------------------------------------------------------- #
# ILP
# --------------------------------------------------------------------------- #
def _ilp(pool, budget, nation_limit, existing_squad, free_transfers, bench_weight,
         captain_positions):
    import pulp

    ep = {p.player_id: p.exp_points for p in pool}
    price = {p.player_id: p.price for p in pool}
    pos = {p.player_id: p.position for p in pool}
    nation = {p.player_id: p.nation for p in pool}
    ids = list(ep)

    prob = pulp.LpProblem("wc_fantasy", pulp.LpMaximize)
    x = pulp.LpVariable.dicts("squad", ids, cat="Binary")
    s = pulp.LpVariable.dicts("start", ids, cat="Binary")
    c = pulp.LpVariable.dicts("capt", ids, cat="Binary")

    # Objective: starters + captain (doubles) + small bench credit, minus hits.
    objective = (
        pulp.lpSum(ep[i] * s[i] for i in ids)
        + pulp.lpSum(ep[i] * c[i] for i in ids)
        + bench_weight * pulp.lpSum(ep[i] * (x[i] - s[i]) for i in ids)
    )

    # Transfer hits (transfer mode only).
    transfers_made_expr = None
    if existing_squad is not None and free_transfers != config.UNLIMITED:
        owned = set(existing_squad)
        transfers_in = pulp.lpSum(x[i] for i in ids if i not in owned)
        hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += hits >= transfers_in - free_transfers
        objective -= config.TRANSFER_HIT_COST * hits
        transfers_made_expr = transfers_in

    prob += objective

    # Squad composition
    prob += pulp.lpSum(x[i] for i in ids) == config.SQUAD_SIZE
    for position, count in config.SQUAD_COMPOSITION.items():
        prob += pulp.lpSum(x[i] for i in ids if pos[i] == position) == count

    # Budget
    prob += pulp.lpSum(price[i] * x[i] for i in ids) <= budget

    # Nation limit
    for nat in set(nation.values()):
        prob += pulp.lpSum(x[i] for i in ids if nation[i] == nat) <= nation_limit

    # Starters subset of squad, 11 of them
    for i in ids:
        prob += s[i] <= x[i]
        prob += c[i] <= s[i]
        if pos[i] not in captain_positions:
            prob += c[i] == 0
    prob += pulp.lpSum(s[i] for i in ids) == config.STARTING_XI
    prob += pulp.lpSum(c[i] for i in ids) == 1

    # Formation ranges
    for position, (lo, hi) in config.FORMATION_LIMITS.items():
        cnt = pulp.lpSum(s[i] for i in ids if pos[i] == position)
        prob += cnt >= lo
        prob += cnt <= hi

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"Optimizer status: {pulp.LpStatus[status]} "
                           "(check budget / pool size / nation limit feasibility)")

    chosen = [i for i in ids if x[i].value() > 0.5]
    starters = [i for i in ids if s[i].value() > 0.5]
    captain = next(i for i in ids if c[i].value() > 0.5)

    transfers_made = 0
    if transfers_made_expr is not None:
        transfers_made = int(round(pulp.value(transfers_made_expr)))

    return _build_selection(pool, chosen, starters, captain,
                            existing_squad, free_transfers, transfers_made,
                            captain_positions)


# --------------------------------------------------------------------------- #
# Heuristic fallback (used only if PuLP is unavailable)
# --------------------------------------------------------------------------- #
def _heuristic(pool, budget, nation_limit, captain_positions):
    """Phase A: cheapest valid squad (guarantees feasibility).
    Phase B: repeatedly apply the best single same-position upgrade swap that
    stays within budget and the nation cap. Not provably optimal, but solid.
    """
    # Phase A — cheapest feasible squad.
    chosen: Dict[str, PlayerProjection] = {}
    nat_count: Dict[str, int] = {}
    for pos in config.POSITIONS:
        need = config.SQUAD_COMPOSITION[pos]
        picked = 0
        for p in sorted((p for p in pool if p.position == pos), key=lambda p: p.price):
            if picked == need:
                break
            if nat_count.get(p.nation, 0) >= nation_limit:
                continue
            chosen[p.player_id] = p
            nat_count[p.nation] = nat_count.get(p.nation, 0) + 1
            picked += 1
        if picked < need:
            raise RuntimeError(f"Pool lacks enough {pos} within the nation cap.")

    cost = sum(p.price for p in chosen.values())
    if cost > budget + 1e-6:
        raise RuntimeError(
            f"Infeasible: cheapest valid squad costs ${cost:.1f}m > ${budget:.1f}m.")

    # Phase B — local-search upgrades.
    for _ in range(2000):
        best_gain, best_swap = 1e-9, None
        for out_p in list(chosen.values()):
            for in_p in pool:
                if in_p.player_id in chosen or in_p.position != out_p.position:
                    continue
                if (in_p.nation != out_p.nation
                        and nat_count.get(in_p.nation, 0) >= nation_limit):
                    continue
                new_cost = cost - out_p.price + in_p.price
                if new_cost > budget + 1e-6:
                    continue
                gain = in_p.exp_points - out_p.exp_points
                if gain > best_gain:
                    best_gain, best_swap = gain, (out_p, in_p, new_cost)
        if not best_swap:
            break
        out_p, in_p, new_cost = best_swap
        del chosen[out_p.player_id]
        nat_count[out_p.nation] -= 1
        chosen[in_p.player_id] = in_p
        nat_count[in_p.nation] = nat_count.get(in_p.nation, 0) + 1
        cost = new_cost

    chosen_ids = list(chosen)
    starters, captain = _best_xi(list(chosen.values()), captain_positions)
    return _build_selection(pool, chosen_ids, starters, captain, None, -1, 0,
                            captain_positions)


def _best_xi(squad: List[PlayerProjection], captain_positions=DEFAULT_CAPTAIN_POSITIONS):
    formations = [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2),
                  (4, 5, 1), (5, 3, 2), (5, 4, 1)]
    by_pos = {pp: sorted([p for p in squad if p.position == pp],
                         key=lambda p: p.exp_points, reverse=True)
              for pp in config.POSITIONS}
    best, best_ep = None, -1e9
    gk = by_pos["GK"][0]
    for d, m, f in formations:
        if len(by_pos["DEF"]) < d or len(by_pos["MID"]) < m or len(by_pos["FWD"]) < f:
            continue
        xi = [gk] + by_pos["DEF"][:d] + by_pos["MID"][:m] + by_pos["FWD"][:f]
        total = sum(p.exp_points for p in xi)
        if total > best_ep:
            best_ep, best = total, xi
    starters = [p.player_id for p in best]
    cap_choices = [p for p in best if p.position in captain_positions] or best
    captain = max(cap_choices, key=lambda p: p.exp_points).player_id
    return starters, captain


# --------------------------------------------------------------------------- #
# Build the Selection result
# --------------------------------------------------------------------------- #
def _build_selection(pool, chosen, starters, captain, existing_squad,
                     free_transfers, transfers_made,
                     captain_positions=DEFAULT_CAPTAIN_POSITIONS) -> Selection:
    by_id = {p.player_id: p for p in pool}
    starter_set = set(starters)
    bench_players = [by_id[i] for i in chosen if i not in starter_set]
    # Auto-sub priority: outfielders by EP desc, reserve GK last.
    bench_out = sorted([p for p in bench_players if p.position != "GK"],
                       key=lambda p: p.exp_points, reverse=True)
    bench_gk = [p for p in bench_players if p.position == "GK"]
    bench = [p.player_id for p in bench_out] + [p.player_id for p in bench_gk]

    starters_by_pos = {pp: sum(1 for i in starters if by_id[i].position == pp)
                       for pp in config.POSITIONS}
    formation = _formation_string(starters_by_pos)

    # Vice-captain: best non-captain starter, preferring an attacking position.
    vice_pool = [by_id[i] for i in starters
                 if i != captain and by_id[i].position in captain_positions]
    if not vice_pool:
        vice_pool = [by_id[i] for i in starters if i != captain]
    vice = max(vice_pool, key=lambda p: p.exp_points).player_id

    cost = sum(by_id[i].price for i in chosen)
    starters_ep = sum(by_id[i].exp_points for i in starters)
    captain_bonus = by_id[captain].exp_points
    hit_points = 0
    if existing_squad is not None and free_transfers != config.UNLIMITED:
        hit_points = max(0, transfers_made - free_transfers) * config.TRANSFER_HIT_COST
    expected_points = starters_ep + captain_bonus - hit_points

    return Selection(
        round="", squad=chosen, starters=starters, bench=bench,  # round set by caller
        captain=captain, vice=vice, formation=formation, chip=None,
        expected_points=round(expected_points, 2), cost=round(cost, 1),
        transfers_made=transfers_made, hit_points=hit_points)


# --------------------------------------------------------------------------- #
# Multi-round (horizon) optimizer with a planned-transfer budget
# --------------------------------------------------------------------------- #
def optimize_horizon(meta, ep, round_labels, budget, nation_limit,
                     discounts=None, bench_weight=BENCH_WEIGHT,
                     captain_positions=DEFAULT_CAPTAIN_POSITIONS,
                     planned_transfers=1, top_per_pos=42, cheap_per_pos=6):
    """Plan the squad across `round_labels`, allowing a few planned transfers.

    The squad may change by up to `planned_transfers` players between consecutive
    rounds (default 1) — enough to earmark one-week punts for an upgrade, while
    deliberately *reserving* the rest of the free-transfer allowance for injuries
    and rotation. Each round it fields the best legal XI + captain.

    `planned_transfers=0` reproduces a held squad; setting it to the full free
    allowance assumes every transfer goes on upgrades (not advised).

    Returns: (md1_squad_ids, plan) where plan[r] = dict(label, squad, starters,
        captain, vice, formation, ep, bench, transfers_in, transfers_out).
    """
    import pulp

    R = len(round_labels)
    discounts = discounts or [1.0, 0.6, 0.4][:R]
    if len(discounts) < R:
        discounts = discounts + [discounts[-1]] * (R - len(discounts))

    def hv(pid):
        return sum(d * e for d, e in zip(discounts, ep.get(pid, [])))

    cand = set()
    for position in config.POSITIONS:
        ids_pos = [i for i in meta if meta[i]["position"] == position]
        cand.update(sorted(ids_pos, key=hv, reverse=True)[:top_per_pos])
        cand.update(sorted(ids_pos, key=lambda i: meta[i]["price"])[:cheap_per_pos])
    ids = list(cand)

    price = {i: meta[i]["price"] for i in ids}
    pos = {i: meta[i]["position"] for i in ids}
    nation = {i: meta[i]["nation"] for i in ids}
    epr = {i: ep[i] for i in ids}
    cap_pos = set(captain_positions)

    prob = pulp.LpProblem("wc_horizon", pulp.LpMaximize)
    x = {(i, r): pulp.LpVariable(f"x_{i}_{r}", cat="Binary")
         for i in ids for r in range(R)}
    s = {(i, r): pulp.LpVariable(f"s_{i}_{r}", cat="Binary")
         for i in ids for r in range(R)}
    c = {(i, r): pulp.LpVariable(f"c_{i}_{r}", cat="Binary")
         for i in ids for r in range(R)}
    tin = {(i, r): pulp.LpVariable(f"tin_{i}_{r}", lowBound=0, upBound=1, cat="Binary")
           for i in ids for r in range(1, R)}

    # Objective: discounted (starters + captain) per round + small MD1 bench credit.
    obj = pulp.lpSum(discounts[r] * epr[i][r] * (s[(i, r)] + c[(i, r)])
                     for i in ids for r in range(R))
    obj += bench_weight * pulp.lpSum(epr[i][0] * (x[(i, 0)] - s[(i, 0)]) for i in ids)
    prob += obj

    for r in range(R):
        # Squad composition / budget / nation, per round
        prob += pulp.lpSum(x[(i, r)] for i in ids) == config.SQUAD_SIZE
        for position, count in config.SQUAD_COMPOSITION.items():
            prob += pulp.lpSum(x[(i, r)] for i in ids if pos[i] == position) == count
        prob += pulp.lpSum(price[i] * x[(i, r)] for i in ids) <= budget
        for nat in set(nation.values()):
            prob += pulp.lpSum(x[(i, r)] for i in ids if nation[i] == nat) <= nation_limit

        # XI + captain, per round
        for i in ids:
            prob += s[(i, r)] <= x[(i, r)]
            prob += c[(i, r)] <= s[(i, r)]
            if pos[i] not in cap_pos:
                prob += c[(i, r)] == 0
        prob += pulp.lpSum(s[(i, r)] for i in ids) == config.STARTING_XI
        prob += pulp.lpSum(c[(i, r)] for i in ids) == 1
        for position, (lo, hi) in config.FORMATION_LIMITS.items():
            cnt = pulp.lpSum(s[(i, r)] for i in ids if pos[i] == position)
            prob += cnt >= lo
            prob += cnt <= hi

        # Planned-transfer budget between rounds (additions == removals as size fixed)
        if r >= 1:
            for i in ids:
                prob += tin[(i, r)] >= x[(i, r)] - x[(i, r - 1)]
            prob += pulp.lpSum(tin[(i, r)] for i in ids) <= planned_transfers

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"Horizon optimizer status: {pulp.LpStatus[status]}")

    squad_by_round = {r: [i for i in ids if x[(i, r)].value() > 0.5] for r in range(R)}
    plan = []
    for r in range(R):
        squad_r = squad_by_round[r]
        starters = [i for i in ids if s[(i, r)].value() > 0.5]
        captain = next(i for i in ids if c[(i, r)].value() > 0.5)
        vice_pool = [i for i in starters if i != captain and pos[i] in cap_pos] \
            or [i for i in starters if i != captain]
        vice = max(vice_pool, key=lambda i: epr[i][r])
        by_pos = {pp: sum(1 for i in starters if pos[i] == pp) for pp in config.POSITIONS}
        bench = [i for i in squad_r if i not in set(starters)]
        bench_out = sorted([i for i in bench if pos[i] != "GK"],
                           key=lambda i: epr[i][r], reverse=True)
        bench_gk = [i for i in bench if pos[i] == "GK"]
        if r == 0:
            t_in, t_out = [], []
        else:
            prev = set(squad_by_round[r - 1])
            cur = set(squad_r)
            t_in = sorted(cur - prev, key=lambda i: epr[i][r], reverse=True)
            t_out = sorted(prev - cur, key=lambda i: epr[i][r - 1], reverse=True)
        plan.append({
            "label": round_labels[r],
            "squad": squad_r,
            "starters": starters,
            "captain": captain,
            "vice": vice,
            "formation": _formation_string(by_pos),
            "ep": round(sum(epr[i][r] for i in starters) + epr[captain][r], 2),
            "bench": bench_out + bench_gk,
            "transfers_in": t_in,
            "transfers_out": t_out,
        })
    return squad_by_round[0], plan
