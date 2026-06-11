"""Penalty / set-piece taker roles, matched to the player pool.

Penalty takers gain expected penalty goals; set-piece takers gain assist (and a
little direct-free-kick) value. Roles are matched by surname within a nation, so
the data file only needs surnames.
"""
from __future__ import annotations

import csv
import unicodedata
from typing import Dict, Set

from .. import config

SET_PIECES_FILE = config.FIXTURES_DIR / "set_pieces.csv"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def _matches(taker: str, player_name: str) -> bool:
    """True if a taker surname matches a player's name (within an already-filtered
    nation). Handles multi-word names by checking each taker token."""
    if not taker:
        return False
    pn = f" {_norm(player_name)} "
    for tok in _norm(taker).split():
        if len(tok) >= 3 and f" {tok} " in pn or pn.strip().endswith(tok):
            return True
    return False


def roles_for_players(players) -> Dict[str, Set[str]]:
    """player_id -> {'pen', 'sp'} roles, or empty if none."""
    if not SET_PIECES_FILE.exists():
        return {}
    takers: Dict[str, dict] = {}
    import io
    with open(SET_PIECES_FILE, encoding="utf-8") as f:
        body = "".join(ln for ln in f if not ln.lstrip().startswith("#"))
    for row in csv.DictReader(io.StringIO(body)):
        if row.get("nation"):
            takers[row["nation"].strip()] = row
    out: Dict[str, Set[str]] = {}
    # For each nation+role, pick the single best-matching player (most likely the
    # actual taker), to avoid tagging several namesakes.
    by_nation: Dict[str, list] = {}
    for p in players:
        by_nation.setdefault(p.nation, []).append(p)
    for nation, row in takers.items():
        for role, col in (("pen", "penalty"), ("sp", "setpiece")):
            name = (row.get(col) or "").strip()
            if not name:
                continue
            cands = [p for p in by_nation.get(nation, []) if _matches(name, p.name)]
            if not cands:
                continue
            # Prefer the highest-priced match (the recognised star, not a namesake).
            best = max(cands, key=lambda p: p.price)
            out.setdefault(best.id, set()).add(role)
    return out
