"""Persisted, authoritative odds->player name aliases.

A verified mapping (e.g. Raphinha == "Raphael Dias Belloli") is stored here so
the matcher treats it as ground truth and can never silently re-break it. The
fuzzy matcher only fills gaps this table doesn't already cover.

Stored as JSON: {player_id: [normalised_bookmaker_name, ...]}. Names are stored
already normalised (via projections._norm) so lookup is a plain set membership.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Set

from .. import config

ALIASES_FILE = config.PLAYERS_DIR / "odds_aliases.json"


def load() -> Dict[str, Set[str]]:
    if not ALIASES_FILE.exists():
        return {}
    raw = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    return {pid: set(v) for pid, v in raw.items()}


def save(aliases: Dict[str, Set[str]]) -> Path:
    ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {pid: sorted(v) for pid, v in sorted(aliases.items()) if v}
    ALIASES_FILE.write_text(
        json.dumps(serialisable, ensure_ascii=False, indent=1), encoding="utf-8")
    return ALIASES_FILE


def add(aliases: Dict[str, Set[str]], player_id: str, normalised_name: str) -> bool:
    """Add one alias; returns True if it was new."""
    bucket = aliases.setdefault(player_id, set())
    if normalised_name in bucket:
        return False
    bucket.add(normalised_name)
    return True


def seed_from_matches(players, matches, min_score: float = 0.85,
                      aliases: Optional[Dict[str, Set[str]]] = None) -> int:
    """Lock in every confident (score >= min_score) current match as an alias.

    Run after a verified snapshot so confirmed links become authoritative.
    Returns the number of newly-added aliases.
    """
    from collections import defaultdict
    from .. import projections as P  # lazy: projections imports this module lazily

    aliases = load() if aliases is None else aliases
    by_nation: Dict[str, list] = defaultdict(list)
    for p in players:
        if p.available:
            by_nation[p.nation].append(p)
    nation_match = P._nation_next_match(matches)

    by_match: Dict[str, list] = {}
    for nation, roster in by_nation.items():
        m = nation_match.get(nation)
        if m:
            by_match.setdefault(m["match_id"], [m, []])[1].extend(roster)

    added = 0
    for _mid, (m, roster) in by_match.items():
        for d in P._match_pairs(m, roster, aliases):
            if d["via"] == "alias" or d["score"] >= min_score:
                if add(aliases, d["pid"], P._norm(d["bname"])):
                    added += 1
    save(aliases)
    return added
