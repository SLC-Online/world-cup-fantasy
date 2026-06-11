"""Expected-points engine.

For each player in a round:
    EP = appearance + goals + assists + clean sheet + goals-conceded
         + (GK saves | MID tackles/chances | FWD shots) + cards

Attacking returns (goals, assists) are driven by the betting market and are
*unconditional* (the market already prices how likely/long a player features).
Everything else is conditioned on our line-up model (start prob + expected
minutes). Each component is recorded so a projection is fully auditable.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import odds_math
from .models import MatchGoals, Player, PlayerProjection
from .providers.lineups import Lineups
from .scoring import DEFAULT_RULES, ScoringRules


# --------------------------------------------------------------------------- #
# Tunable model parameters (priors). Override per-player via a stats file.
# --------------------------------------------------------------------------- #
@dataclass
class ProjectionConfig:
    assisted_fraction: float = 0.75       # share of a team's goals that are assisted
    sub_appearance_rate: float = 0.30     # P(a benched player appears as a sub)
    starter_completes_60: float = 0.85    # P(a starter reaches 60 mins)
    saves_per_mu_against: float = 2.5     # expected GK saves per unit of opp xG

    # Residual goal share for players with no goalscorer price, by position.
    residual_goal_weight: Dict[str, float] = field(default_factory=lambda: {
        "GK": 0.0, "DEF": 0.15, "MID": 0.5, "FWD": 1.0})
    # Assist propensity by position (before blending with goal threat).
    assist_weight: Dict[str, float] = field(default_factory=lambda: {
        "GK": 0.0, "DEF": 0.4, "MID": 1.0, "FWD": 0.7})
    assist_threat_blend: float = 0.3      # how much a player's goal threat adds to assists

    # Per-90 priors for bonus categories (used when no stats file is supplied).
    tackles_p90: Dict[str, float] = field(default_factory=lambda: {
        "GK": 0.0, "DEF": 2.2, "MID": 1.8, "FWD": 0.4})
    chances_p90: Dict[str, float] = field(default_factory=lambda: {
        "GK": 0.0, "DEF": 0.4, "MID": 1.2, "FWD": 0.8})
    shots_on_target_p90: Dict[str, float] = field(default_factory=lambda: {
        "GK": 0.0, "DEF": 0.15, "MID": 0.6, "FWD": 1.1})
    yellow_p90: Dict[str, float] = field(default_factory=lambda: {
        "GK": 0.05, "DEF": 0.18, "MID": 0.16, "FWD": 0.10})

    # Penalty / set-piece taker modelling
    penalty_rate: float = 0.13          # penalties a team is awarded per match (avg)
    penalty_conversion: float = 0.76    # penalty conversion rate
    avg_team_mu: float = 1.35           # average team xG, to scale by dominance
    setpiece_assist_rate: float = 0.12  # extra expected assists for the set-piece taker
    setpiece_fk_goal_rate: float = 0.03  # extra direct-FK goals for the taker


DEFAULT_PCONFIG = ProjectionConfig()


# --------------------------------------------------------------------------- #
# Name matching helpers
# --------------------------------------------------------------------------- #
_TRANSLIT = str.maketrans({
    "ß": "ss", "ø": "o", "đ": "d", "ð": "d", "ł": "l", "þ": "th",
    "æ": "ae", "œ": "oe", "ı": "i", "ŋ": "ng", "'": " ", "’": " "})


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().translate(_TRANSLIT)
    return " ".join(s.replace(".", " ").replace("-", " ").split())


def _surname(name: str) -> str:
    parts = _norm(name).split()
    return parts[-1] if parts else ""


def _name_forms(text: str):
    """(token_set, collapsed, surname) for a name string."""
    norm = _norm(text)
    toks = norm.split()
    return (set(t for t in toks if len(t) >= 2),
            norm.replace(" ", ""),
            toks[-1] if toks else "")


def _player_forms(player):
    """Distinct name forms for a player: legal (firstName+lastName) + known."""
    forms = []
    seen = set()
    for nm in (getattr(player, "full_name", ""), player.name):
        if not nm:
            continue
        f = _name_forms(nm)
        if f[0] and f[1] not in seen:
            seen.add(f[1])
            forms.append(f)
    return forms


def _match_score(b, forms) -> float:
    """Best similarity (0..1) between a bookmaker name `b`=(tokens,collapsed,
    surname) and any of a player's name forms. Order-independent, so reversed
    names (e.g. 'Magalhaes Gabriel') and legal-vs-known names both match."""
    bt, bc, bs = b
    best = 0.0
    for pt, pc, ps in forms:
        if not pt:
            continue
        if bt == pt:                       # same token set
            return 1.0
        if bc and pc and bc == pc:         # same once spaces/hyphens removed
            best = max(best, 0.95)
            continue
        if bt <= pt or pt <= bt:           # one is a subset (short vs legal name)
            best = max(best, 0.85 + 0.01 * len(bt & pt))
            continue
        inter = bt & pt
        if inter and bs and bs == ps and len(bs) >= 4:   # shared surname
            best = max(best, 0.72)
        elif inter and max((len(t) for t in inter), default=0) >= 5:
            best = max(best, 0.60 + 0.15 * (len(inter) / len(bt | pt)))
    return best


def _match_pairs(match: dict, roster, aliases=None) -> list:
    """Assign bookmaker names to players. Returns a list of dicts with keys
    pid, bname, price, score, via ('alias'|'strong'|'weak'). Authoritative
    aliases are assigned first, then confident fuzzy matches, then weaker ones
    only when unambiguous."""
    anytime = match.get("anytime", {})
    if not anytime or not roster:
        return []
    aliases = aliases or {}
    roster_ids = {p.id for p in roster}
    alias_to_pid = {}
    for p in roster:
        for al in aliases.get(p.id, ()):      # already-normalised strings
            alias_to_pid[al] = p.id
    # Anytime-scorer markets never price goalkeepers, so excluding them removes a
    # whole class of surname-collision false matches (e.g. a "…Martinez" scorer
    # name latching onto a keeper). Keepers can still be linked via an explicit
    # alias if ever needed.
    cand = [p for p in roster if p.position != "GK"]
    forms = {p.id: _player_forms(p) for p in cand}
    results, used_b, used_p = [], set(), set()

    # Pass 0: authoritative aliases (ground truth, never overridden).
    for bname, price in anytime.items():
        pid = alias_to_pid.get(_norm(bname))
        if pid and pid in roster_ids and pid not in used_p:
            used_b.add(bname); used_p.add(pid)
            results.append({"pid": pid, "bname": bname, "price": price,
                            "score": 1.0, "via": "alias"})

    scored = []
    for bname, price in anytime.items():
        if bname in used_b:
            continue
        b = _name_forms(bname)
        if not b[0]:
            continue
        for p in cand:
            if p.id in used_p:
                continue
            s = _match_score(b, forms[p.id])
            if s > 0:
                scored.append((s, bname, p.id, price))
    scored.sort(key=lambda x: -x[0])

    # Pass 1: confident fuzzy matches, greedy unique.
    for s, bname, pid, price in scored:
        if s < 0.85:
            break
        if bname in used_b or pid in used_p:
            continue
        used_b.add(bname); used_p.add(pid)
        results.append({"pid": pid, "bname": bname, "price": price,
                        "score": s, "via": "strong"})

    # Pass 2: weaker matches only when the remaining candidate is unambiguous.
    rem: Dict[str, list] = {}
    for s, bname, pid, price in scored:
        if 0.60 <= s < 0.85 and bname not in used_b and pid not in used_p:
            rem.setdefault(bname, []).append((s, pid, price))
    for bname, cands in rem.items():
        cands = [c for c in cands if c[1] not in used_p]
        cands.sort(key=lambda x: -x[0])
        if cands and (len(cands) == 1 or cands[0][0] - cands[1][0] >= 0.10):
            s, pid, price = cands[0]
            used_p.add(pid)
            results.append({"pid": pid, "bname": bname, "price": price,
                            "score": s, "via": "weak"})
    return results


def match_market(match: dict, roster, aliases=None) -> Dict[str, float]:
    """Map player_id -> anytime-scorer price by robustly matching the bookmaker
    names in `match` against `roster`, consulting the authoritative alias table
    first (if given) and then fuzzy-matching the rest."""
    return {d["pid"]: d["price"] for d in _match_pairs(match, roster, aliases)}


def _load_aliases():
    try:
        from .providers import aliases as _store
        return _store.load()
    except Exception:
        return {}


def _priced_by_match(players_by_nation, nation_match, aliases=None
                     ) -> Dict[str, Dict[str, float]]:
    """match_id -> {player_id: anytime_price}, matching both teams together and
    honouring the persisted alias table."""
    if aliases is None:
        aliases = _load_aliases()
    by_match: Dict[str, list] = {}
    for nation, roster in players_by_nation.items():
        m = nation_match.get(nation)
        if not m:
            continue
        entry = by_match.setdefault(m["match_id"], [m, []])
        entry[1].extend(roster)
    return {mid: match_market(m, roster, aliases)
            for mid, (m, roster) in by_match.items()}


def audit_market(players, matches, aliases=None) -> dict:
    """Data-integrity check on odds<->player linking. Returns match rate plus
    the names that should worry us: popular players with no linked goal price,
    bookmaker names that didn't link to anyone, and low-confidence fuzzy matches
    worth promoting to verified aliases."""
    from collections import defaultdict
    if aliases is None:
        aliases = _load_aliases()
    by_nation = defaultdict(list)
    for p in players:
        if getattr(p, "available", True):
            by_nation[p.nation].append(p)
    nation_match = _nation_next_match(matches)
    by_match: Dict[str, list] = {}
    for nation, roster in by_nation.items():
        m = nation_match.get(nation)
        if m:
            by_match.setdefault(m["match_id"], [m, []])[1].extend(roster)

    total = matched = 0
    matched_pids, unmatched_names, low_conf = set(), [], []
    for _mid, (m, roster) in by_match.items():
        anytime = m.get("anytime", {})
        total += len(anytime)
        pairs = _match_pairs(m, roster, aliases)
        matched += len(pairs)
        assigned_b = set()
        forms = {p.id: _player_forms(p) for p in roster}
        for d in pairs:
            matched_pids.add(d["pid"])
            assigned_b.add(d["bname"])
            if d["via"] == "weak":
                low_conf.append((d["bname"], d["pid"], round(d["score"], 2)))
        for bname in anytime:
            if bname in assigned_b:
                continue
            b = _name_forms(bname)
            best = max((_match_score(b, forms[p.id]) for p in roster), default=0.0)
            if best < 0.60:
                unmatched_names.append(bname)

    popular = [p for p in players
               if getattr(p, "available", True) and p.position in ("MID", "FWD")
               and (p.ownership >= 4.0 or p.price >= 7.5)
               and p.nation in nation_match and p.id not in matched_pids]
    return {
        "total": total,
        "matched": matched,
        "rate": round(matched / total, 3) if total else 1.0,
        "unmatched_names": unmatched_names,
        "popular_unlinked": [(p.name, p.nation, p.position, p.price, p.ownership)
                             for p in popular],
        "low_confidence": low_conf,
    }


def market_priced_ids(players_by_nation, nation_match) -> set:
    """IDs of players who already have an anytime-scorer price in their next
    match. Used to gate the penalty/FK-goal boost (the market already prices a
    designated taker's penalty + direct-FK goals into their scorer quote)."""
    out: set = set()
    for pr in _priced_by_match(players_by_nation, nation_match).values():
        out |= set(pr.keys())
    return out


# --------------------------------------------------------------------------- #
# Match goals from odds
# --------------------------------------------------------------------------- #
def compute_match_goals(matches: List[dict]) -> Dict[str, MatchGoals]:
    """Map match_id -> MatchGoals using the 1X2 (+ totals) markets."""
    out: Dict[str, MatchGoals] = {}
    for m in matches:
        h2h = m.get("h2h", {})
        if not (h2h.get("home") and h2h.get("draw") and h2h.get("away")):
            continue
        totals = m.get("totals", {})
        mu_home, mu_away = odds_math.match_goals_from_odds(
            h2h["home"], h2h["draw"], h2h["away"],
            over_odds=totals.get("over"), under_odds=totals.get("under"),
            total_line=totals.get("line") or 2.5,
        )
        out[m["match_id"]] = MatchGoals(
            match_id=m["match_id"], home=m["home"], away=m["away"],
            mu_home=mu_home, mu_away=mu_away)
    return out


def _nation_next_match(matches: List[dict]) -> Dict[str, dict]:
    """nation -> the (earliest) match dict it features in within this snapshot."""
    chosen: Dict[str, dict] = {}
    for m in matches:
        for nation in (m["home"], m["away"]):
            prev = chosen.get(nation)
            if prev is None or (m.get("commence_time", "") < prev.get("commence_time", "")):
                chosen[nation] = m
    return chosen


# --------------------------------------------------------------------------- #
# Per-player expected-points components (shared by single- and multi-round)
# --------------------------------------------------------------------------- #
def compute_components(position, mu_against, lam_goal, exp_assist,
                       p_start, exp_minutes, rules=DEFAULT_RULES,
                       pconfig=DEFAULT_PCONFIG, stats=None,
                       team_mu=0.0, roles=None, has_market_goals=False):
    """Expected-points components for one player in one fixture.

    Attacking returns (goals/assists) are passed in already computed; everything
    else is derived from the opponent's expected goals and the minutes model.
    `roles` ({'pen','sp'}) + `team_mu` add penalty/set-piece value.

    `has_market_goals`: True when the player already has an anytime-scorer price.
    The market prices a designated taker's penalty and direct-free-kick *goals*
    into that quote, so for priced players we must NOT add those goals again
    (double-count). We still credit set-piece *assists* (not in the scorer
    market) and the +1 direct-free-kick method bonus.
    """
    stats = stats or {}
    roles = roles or set()
    minutes_frac = exp_minutes / 90.0
    p_play = p_start + (1 - p_start) * pconfig.sub_appearance_rate
    p_play_60 = p_start * pconfig.starter_completes_60
    comp = {}

    comp["appearance"] = (p_play * rules.appearance_any
                          + p_play_60 * rules.appearance_60)
    # Anytime-scorer odds are settled "void if the player doesn't play", so they
    # price P(scores | plays). Expected goals must therefore be gated by P(plays):
    #   E[goals] = P(plays) * P(scores | plays).  Same logic for assists.
    if lam_goal:
        comp["goals"] = lam_goal * p_play * rules.goal_points[position]
    if exp_assist:
        comp["assists"] = exp_assist * p_play * rules.assist_points

    if rules.clean_sheet_points.get(position):
        comp["clean_sheet"] = (p_play_60 * odds_math.prob_clean_sheet(mu_against)
                               * rules.clean_sheet_points[position])
    if position in ("GK", "DEF"):
        comp["goals_conceded"] = (
            minutes_frac * odds_math.expected_conceded_penalty_units(mu_against)
            * rules.goals_conceded_penalty)

    if position == "GK":
        exp_saves = stats.get("saves_p90", pconfig.saves_per_mu_against * mu_against)
        exp_saves *= minutes_frac if "saves_p90" in stats else 1.0
        comp["saves"] = (exp_saves / rules.save_unit) * rules.save_unit_points
    if position == "MID":
        tk = stats.get("tackles_p90", pconfig.tackles_p90[position]) * minutes_frac
        ch = stats.get("chances_p90", pconfig.chances_p90[position]) * minutes_frac
        comp["tackles"] = (tk / rules.tackle_unit) * rules.tackle_unit_points
        comp["chances"] = (ch / rules.chance_unit) * rules.chance_unit_points
    if position == "FWD":
        sot = stats.get("shots_on_target_p90",
                        pconfig.shots_on_target_p90[position]) * minutes_frac
        comp["shots_on_target"] = (
            sot / rules.shot_on_target_unit) * rules.shot_on_target_unit_points

    yc = stats.get("yellow_p90", pconfig.yellow_p90[position]) * minutes_frac
    comp["cards"] = yc * rules.yellow_card_points

    # Penalty / set-piece taker value (scaled by the team's attacking dominance).
    # For market-priced players the scorer odds already include their penalty
    # and direct-FK goals, so those are gated out to avoid double-counting.
    if team_mu:
        scale = team_mu / pconfig.avg_team_mu
        if "pen" in roles and not has_market_goals:
            pen_goals = pconfig.penalty_rate * scale * pconfig.penalty_conversion
            comp["penalties"] = pen_goals * p_play * rules.goal_points[position]
        if "sp" in roles:
            # Set-piece assists are NOT in the scorer market -> always credited.
            sp = pconfig.setpiece_assist_rate * scale * p_play * rules.assist_points
            if has_market_goals:
                # Base FK goal already priced; keep only the +1 method bonus.
                sp += (pconfig.setpiece_fk_goal_rate * scale * p_play
                       * rules.direct_free_kick_bonus)
            else:
                sp += (pconfig.setpiece_fk_goal_rate * scale * p_play
                       * (rules.goal_points[position] + rules.direct_free_kick_bonus))
            comp["set_pieces"] = sp
    return comp


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def build_projections(
    players: List[Player],
    matches: List[dict],
    lineups: Lineups,
    rules: ScoringRules = DEFAULT_RULES,
    pconfig: ProjectionConfig = DEFAULT_PCONFIG,
    player_stats: Optional[Dict[str, Dict[str, float]]] = None,
    set_piece_roles: Optional[Dict[str, set]] = None,
) -> List[PlayerProjection]:
    player_stats = player_stats or {}
    set_piece_roles = set_piece_roles or {}
    match_goals = compute_match_goals(matches)
    nation_match = _nation_next_match(matches)
    by_match = {m["match_id"]: m for m in matches}

    # Group players by nation for per-team goal allocation.
    players_by_nation: Dict[str, List[Player]] = {}
    for p in players:
        players_by_nation.setdefault(p.nation, []).append(p)

    # Pre-compute each player's expected goals (lambda) using the scorer market.
    lambda_goal: Dict[str, float] = _allocate_team_goals(
        players_by_nation, nation_match, match_goals, lineups, pconfig)
    priced = market_priced_ids(players_by_nation, nation_match)

    projections: List[PlayerProjection] = []
    for p in players:
        match = nation_match.get(p.nation)
        if not match or match["match_id"] not in match_goals:
            # No priced fixture this round -> not selectable in practice.
            projections.append(PlayerProjection(
                player_id=p.id, name=p.name, nation=p.nation, position=p.position,
                price=p.price, match_id="", opponent="", p_start=0.0,
                exp_minutes=0.0, exp_points=0.0, components={}))
            continue

        mg = match_goals[match["match_id"]]
        mu_against = mg.mu_against(p.nation)
        opponent = mg.opponent_of(p.nation)

        ln = lineups.for_player(p.id)
        p_start = ln["p_start"]
        exp_min = ln["exp_minutes"]
        exp_assist = _expected_assists(p, lambda_goal, players_by_nation,
                                       match_goals, nation_match, pconfig)
        comp = compute_components(
            position=p.position, mu_against=mu_against,
            lam_goal=lambda_goal.get(p.id, 0.0), exp_assist=exp_assist,
            p_start=p_start, exp_minutes=exp_min,
            rules=rules, pconfig=pconfig, stats=player_stats.get(p.id, {}),
            team_mu=mg.mu_for(p.nation), roles=set_piece_roles.get(p.id),
            has_market_goals=p.id in priced)
        exp_points = float(sum(comp.values()))
        projections.append(PlayerProjection(
            player_id=p.id, name=p.name, nation=p.nation, position=p.position,
            price=p.price, match_id=match["match_id"], opponent=opponent,
            p_start=p_start, exp_minutes=exp_min, exp_points=exp_points,
            components={k: round(v, 3) for k, v in comp.items()}))

    return projections


# --------------------------------------------------------------------------- #
# Goal / assist allocation
# --------------------------------------------------------------------------- #
def _allocate_team_goals(players_by_nation, nation_match, match_goals,
                         lineups: Lineups, pconfig: ProjectionConfig
                         ) -> Dict[str, float]:
    """Per-player expected goals, consistent with each team's match mu.

    Market-priced players keep their implied rate; any residual team goals are
    spread over un-priced players by position weight x expected minutes.
    """
    lam: Dict[str, float] = {}
    priced_by_match = _priced_by_match(players_by_nation, nation_match)
    for nation, roster in players_by_nation.items():
        match = nation_match.get(nation)
        if not match or match["match_id"] not in match_goals:
            continue
        team_mu = match_goals[match["match_id"]].mu_for(nation)
        priced = priced_by_match.get(match["match_id"], {})

        matched_sum = 0.0
        unmatched: List[Player] = []
        for p in roster:
            price = priced.get(p.id)
            if price:
                l = odds_math.lambda_from_anytime(odds_math.implied_prob(price))
                lam[p.id] = l
                matched_sum += l
            else:
                unmatched.append(p)

        residual = max(0.0, team_mu - matched_sum)
        if matched_sum > team_mu and matched_sum > 0:
            # Market attributes >= all goals to priced players: scale them down.
            scale = team_mu / matched_sum
            for p in roster:
                if p.id in lam:
                    lam[p.id] *= scale
            residual = 0.0

        if residual > 0 and unmatched:
            weights = {}
            for p in unmatched:
                mins = lineups.for_player(p.id)["exp_minutes"] / 90.0
                weights[p.id] = pconfig.residual_goal_weight[p.position] * mins
            wsum = sum(weights.values())
            if wsum > 0:
                for pid, w in weights.items():
                    lam[pid] = residual * w / wsum
    return lam


def _expected_assists(player, lambda_goal, players_by_nation, match_goals,
                      nation_match, pconfig: ProjectionConfig) -> float:
    match = nation_match.get(player.nation)
    if not match or match["match_id"] not in match_goals:
        return 0.0
    team_mu = match_goals[match["match_id"]].mu_for(player.nation)
    team_assists = team_mu * pconfig.assisted_fraction
    roster = players_by_nation[player.nation]
    weights = {}
    for p in roster:
        w = (pconfig.assist_weight[p.position]
             + pconfig.assist_threat_blend * lambda_goal.get(p.id, 0.0))
        weights[p.id] = max(0.0, w)
    wsum = sum(weights.values())
    if wsum <= 0:
        return 0.0
    return team_assists * weights[player.id] / wsum
