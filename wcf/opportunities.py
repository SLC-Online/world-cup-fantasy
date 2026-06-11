"""Opportunity detection — the "is there a smart move this round?" radar.

Surfaces actionable, beyond-the-basics alerts from current state:
  * unavailable players in your squad (injury/suspension/transferred-out),
  * captain/vice not in a confirmed XI,
  * a worthwhile single transfer (EP gain over a threshold),
  * low-owned high-EP differentials you don't own,
  * a Wildcard-worth heuristic.

Each is an Alert(severity, kind, message). Pure functions over data the rest of
the system already produces, so it's easy to test and to feed notifications.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Player, PlayerProjection


@dataclass
class Alert:
    severity: str   # high | medium | low
    kind: str
    message: str


_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def detect(
    team: dict,
    projections: List[PlayerProjection],
    players: List[Player],
    confirmed_by_nation: Optional[Dict[str, set]] = None,
    momentum: Optional[Dict[str, dict]] = None,
    budget: float = 100.0,
    nation_limit: int = 3,
    differential_max_ownership: float = 8.0,
    differential_pool: int = 40,
    transfer_gain_threshold: float = 1.5,
) -> List[Alert]:
    alerts: List[Alert] = []
    momentum = momentum or {}
    owned = [p["id"] for p in team["starters"] + team["bench"]]
    owned_set = set(owned)
    starter_ids = [p["id"] for p in team["starters"]]

    status = {p.id: p.status for p in players}
    ownership = {p.id: p.ownership for p in players}
    by_id = {p.player_id: p for p in projections}
    name_by_id = {p.player_id: p.name for p in projections}

    # 1) Unavailable players in your squad.
    for pid in owned:
        if status.get(pid, "playing") != "playing":
            nm = name_by_id.get(pid, pid)
            alerts.append(Alert("high", "unavailable",
                                 f"⛔ {nm} is '{status.get(pid)}' — replace with a free transfer."))

    # 2) Captain / vice not in a confirmed XI (only when line-ups are in).
    if confirmed_by_nation:
        for role in ("captain", "vice"):
            pid = team.get(role)
            p = by_id.get(pid)
            if not p:
                continue
            names = confirmed_by_nation.get(p.nation)
            if names is not None:
                full = _norm(p.name)
                surname = full.split()[-1] if full else ""
                starting = full in names or any(surname == n.split()[-1] for n in names)
                if not starting:
                    alerts.append(Alert("high", f"{role}_benched",
                                         f"🅒 Your {role} {p.name} is NOT in the confirmed XI — "
                                         f"change {role} before kickoff."))

    # 3) Worthwhile single transfer (greedy, budget + nation feasible).
    swap = _best_transfer(team, projections, budget, nation_limit)
    if swap and swap["gain"] >= transfer_gain_threshold:
        alerts.append(Alert("medium", "transfer",
                             f"🔁 Transfer worth considering: {swap['out_name']} → "
                             f"{swap['in_name']} (+{swap['gain']:.1f} xP this round)."))

    # 4) Differentials: low-owned, high-EP players you don't own.
    ranked = sorted(projections, key=lambda p: p.exp_points, reverse=True)[:differential_pool]
    diffs = [p for p in ranked
             if p.player_id not in owned_set
             and 0 < ownership.get(p.player_id, 0) <= differential_max_ownership]
    for p in diffs[:3]:
        alerts.append(Alert("low", "differential",
                             f"💎 Market-backed differential (low-owned, strong odds): {p.name} "
                             f"({ownership.get(p.player_id, 0):.0f}% owned, "
                             f"{p.exp_points:.1f} xP vs {p.opponent})."))

    # 5) Wildcard heuristic.
    unavailable_starters = sum(1 for pid in starter_ids
                               if status.get(pid, "playing") != "playing")
    if unavailable_starters >= 3:
        alerts.append(Alert("medium", "wildcard",
                             f"🃏 {unavailable_starters} of your XI are unavailable — "
                             "a Wildcard may be worth it this round."))

    # 6) Ownership momentum: owned players sliding (possible bad news), and
    #    low-owned risers (crowd moving in — emerging picks the level alone misses).
    for pid in owned:
        mv = momentum.get(pid, {})
        if mv.get("rel", 0) <= -0.30 and mv.get("now", 0) >= 1.0:
            alerts.append(Alert("medium", "ownership_falling",
                                 f"📉 {name_by_id.get(pid, pid)} ownership sliding "
                                 f"({mv['prev']:.0f}%→{mv['now']:.0f}%) — possible bad "
                                 "news, check team news."))
    risers = sorted(
        (p for p in projections
         if p.player_id not in owned_set
         and momentum.get(p.player_id, {}).get("rel", 0) >= 0.50
         and momentum.get(p.player_id, {}).get("now", 0) <= 15
         and p.exp_points > 0),
        key=lambda p: momentum[p.player_id]["rel"], reverse=True)
    for p in risers[:3]:
        mv = momentum[p.player_id]
        alerts.append(Alert("low", "ownership_rising",
                             f"📈 {p.name} ownership surging ({mv['prev']:.0f}%→"
                             f"{mv['now']:.0f}%) — crowd moving in; emerging pick "
                             f"({p.exp_points:.1f} xP)."))

    alerts.sort(key=lambda a: _SEV_RANK.get(a.severity, 9))
    return alerts


def _best_transfer(team, projections, budget, nation_limit):
    by_id = {p.player_id: p for p in projections}
    owned = [p["id"] for p in team["starters"] + team["bench"]]
    owned_set = set(owned)
    valid = [pid for pid in owned if pid in by_id]
    if len(valid) < len(owned):           # projections missing for some -> skip
        return None
    squad_cost = sum(by_id[pid].price for pid in owned)
    nation_count: Dict[str, int] = {}
    for pid in owned:
        nation_count[by_id[pid].nation] = nation_count.get(by_id[pid].nation, 0) + 1

    # Limit candidates to the top EP per position for speed.
    by_pos_cand: Dict[str, list] = {}
    for p in sorted(projections, key=lambda x: x.exp_points, reverse=True):
        if p.player_id in owned_set:
            continue
        by_pos_cand.setdefault(p.position, [])
        if len(by_pos_cand[p.position]) < 40:
            by_pos_cand[p.position].append(p)

    best = None
    for out_pid in owned:
        out = by_id[out_pid]
        for cand in by_pos_cand.get(out.position, []):
            new_cost = squad_cost - out.price + cand.price
            if new_cost > budget + 1e-6:
                continue
            other_count = nation_count.get(cand.nation, 0) - (1 if cand.nation == out.nation else 0)
            if other_count >= nation_limit:
                continue
            gain = cand.exp_points - out.exp_points
            if best is None or gain > best["gain"]:
                best = {"gain": gain, "out_name": out.name, "in_name": cand.name}
    return best


def format_alerts(alerts: List[Alert]) -> str:
    if not alerts:
        return "No opportunities flagged."
    return "\n".join(a.message for a in alerts)
