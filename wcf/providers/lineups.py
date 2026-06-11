"""Predicted line-ups -> per-player start probability and expected minutes.

This is the one input you may want to tweak by hand each round (it's where the
human edge lives: late team news, rotation once a group is won, heat, etc.).

CSV schema (data/lineups/lineups_<round>.csv), all columns optional except id:
    player_id, status, p_start, exp_minutes
where status is one of: start | bench | doubt | out. If p_start / exp_minutes are
given they win; otherwise they're derived from status. Players absent from the
file get neutral defaults so they're still considered (the goal-odds signal will
mostly sort out who actually plays).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Optional

from .. import config

# status -> (p_start, exp_minutes)
STATUS_DEFAULTS = {
    "start": (0.92, 82.0),
    "bench": (0.15, 18.0),
    "doubt": (0.50, 45.0),
    "out": (0.0, 0.0),
}
# For players with no line-up info at all.
NEUTRAL = (0.70, 68.0)


class Lineups:
    def __init__(self, data: Dict[str, Dict[str, float]]):
        self._data = data

    def for_player(self, player_id: str) -> Dict[str, float]:
        if player_id in self._data:
            return self._data[player_id]
        return {"p_start": NEUTRAL[0], "exp_minutes": NEUTRAL[1]}

    def __len__(self) -> int:
        return len(self._data)


def lineups_path(round_key: str) -> Path:
    return config.LINEUPS_DIR / f"lineups_{round_key}.csv"


# Nominal number of nailed starters per position, used to tier each squad.
_NOMINAL_STARTERS = {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}


def from_pool(players, momentum=None, trend_available=False, appearances=None) -> Lineups:
    """Derive start probabilities from FIFA data when no predicted XI is given.

    The backbone is FIFA price (role/quality). For *ownership* we follow the
    direction of travel, not the level: once enough history exists
    (`trend_available`), rising ownership lifts a player (crowd moving in — e.g.
    fitness confirmed) and falling ownership penalises them (likely injury/benching
    — this auto-deweights doubts without a manual override). Only at cold start
    (no history) do we fall back to the raw ownership level. Unavailable players
    get 0; a lineups file overrides everything.

    `appearances` ({player_id: 'started'|'sub'|'benched'}) is the strongest prior
    short of a confirmed XI: it's who ACTUALLY played in completed games, so it
    overrides the name-based estimate (this is what stops a backup veteran being
    rated a nailed starter once the real line-up is known).
    """
    momentum = momentum or {}
    appearances = appearances or {}
    by_squad_pos: Dict[tuple, list] = {}
    for p in players:
        by_squad_pos.setdefault((p.nation, p.position), []).append(p)

    data: Dict[str, Dict[str, float]] = {}
    for (nation, pos), group in by_squad_pos.items():
        for p in group:
            if not p.available:
                data[p.id] = {"p_start": 0.0, "exp_minutes": 0.0}
        avail = [p for p in group if p.available]
        if not avail:
            continue
        max_price = max(p.price for p in avail) or 1.0
        max_own = max((p.ownership for p in avail), default=0.0)

        def score(p):
            s = p.price / max_price
            if not trend_available and max_own > 0:
                s += p.ownership / max_own         # cold start only: ownership level
            return s

        avail.sort(key=score, reverse=True)
        k = _NOMINAL_STARTERS[pos]
        for rank, p in enumerate(avail):
            if rank < k:
                p_start = 0.90 - 0.03 * rank          # nailed starters
            elif rank == k:
                p_start = 0.40                         # first rotation option
            else:
                p_start = 0.12                         # fringe
            if trend_available:                        # ownership = direction of travel
                rel = momentum.get(p.id, {}).get("rel", 0.0)
                if rel >= 0.50 and p_start < 0.55:
                    p_start = 0.55                     # rising: crowd moving in
                elif rel <= -0.30:
                    p_start = min(p_start, 0.20)       # falling fast: likely out/benched
            ap = appearances.get(p.id)                 # who ACTUALLY played last time
            if ap == "started":
                p_start = max(p_start, 0.90)            # confirmed real starter (nailed level)
            elif ap == "sub":
                p_start = 0.40                         # came off the bench
            elif ap == "benched":
                p_start = 0.15                         # team played, they didn't
            data[p.id] = {"p_start": p_start,
                          "exp_minutes": round(8 + p_start * 80, 1)}
    mode = "trend-direction" if trend_available else "cold-start level"
    print(f"[lineups] derived start probabilities for {len(data)} players "
          f"(price + ownership {mode}; line-ups override).")
    return Lineups(data)


def _ln_norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def merge_confirmed(prior: Lineups, confirmed_by_nation, players) -> Lineups:
    """Override the prior for teams whose XI is confirmed.

    `confirmed_by_nation`: {fifa_nation: {normalised starter names}}. A player on
    a confirmed team starts (p_start≈0.96) if their name matches, else they're
    treated as benched/out (≈0.05). Players on teams without a confirmed XI keep
    the prior probability.
    """
    data = dict(prior._data)
    for p in players:
        names = confirmed_by_nation.get(p.nation)
        if not names:
            continue
        full = _ln_norm(p.name)
        surname = full.split()[-1] if full else ""
        starting = full in names or any(surname and surname == n.split()[-1]
                                        for n in names)
        if starting:
            data[p.id] = {"p_start": 0.96, "exp_minutes": 86.0}
        else:
            data[p.id] = {"p_start": 0.05, "exp_minutes": 6.0}
    return Lineups(data)


# Override start probs from real appearances. 'started' is set to the SAME level
# a top nailed starter gets from the prior (not higher) — the override's job is to
# fix wrong priors (deweight a benched player, promote an under-rated starter), NOT
# to grant a confirmed starter a bonus that re-ranks them above equally-nailed
# players in better fixtures (which is how Mexico's keeper wrongly topped the list).
_APPEARANCE_PSTART = {"started": (0.90, 80.0), "sub": (0.40, 40.0),
                      "benched": (0.15, 20.0)}


def apply_appearances(prior: Lineups, appearances) -> Lineups:
    """Override start probabilities with who ACTUALLY played in completed games.

    This is the most authoritative signal short of a live confirmed XI, so it
    wins over the name/price prior AND over a stale saved line-ups file — which
    is the whole point: once a backup keeper is seen on the bench, he's deweighted
    everywhere (dashboard, optimiser, planner), not just in one code path.
    """
    if not appearances:
        return prior
    data = dict(prior._data)
    for pid, status in appearances.items():
        ps_min = _APPEARANCE_PSTART.get(status)
        if ps_min:
            data[str(pid)] = {"p_start": ps_min[0], "exp_minutes": ps_min[1]}
    return Lineups(data)


def save_lineups(round_key: str, lineups: Lineups) -> Path:
    config.ensure_dirs()
    path = lineups_path(round_key)
    import csv as _csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["player_id", "status", "p_start", "exp_minutes"])
        for pid, d in lineups._data.items():
            w.writerow([pid, "", round(d["p_start"], 3), round(d["exp_minutes"], 1)])
    return path


def load_lineups(round_key: str, path: Optional[Path] = None) -> Lineups:
    path = Path(path) if path else lineups_path(round_key)
    data: Dict[str, Dict[str, float]] = {}
    if not path.exists():
        sample = config.LINEUPS_DIR / "lineups_sample.csv"
        if sample.exists():
            path = sample
            print(f"[lineups] no file for {round_key} — using sample lineups.")
        else:
            print(f"[lineups] no file for {round_key} — using neutral defaults.")
            return Lineups(data)

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = str(row.get("player_id", "")).strip()
            if not pid:
                continue
            status = (row.get("status") or "").strip().lower()
            p_start = row.get("p_start")
            exp_min = row.get("exp_minutes")
            if status in STATUS_DEFAULTS:
                d_ps, d_min = STATUS_DEFAULTS[status]
            else:
                d_ps, d_min = NEUTRAL
            data[pid] = {
                "p_start": float(p_start) if p_start not in (None, "") else d_ps,
                "exp_minutes": float(exp_min) if exp_min not in (None, "") else d_min,
            }
    print(f"[lineups] loaded {len(data)} player entries for {round_key}.")
    return Lineups(data)
