"""Configuration, paths and the game's structural constants.

Everything here is *game structure* (budget, squad shape, per-round limits).
The *scoring* rules live in ``scoring.py``. Keeping the two separate means a
rules tweak never accidentally changes the optimizer's constraints.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

PLAYERS_DIR = DATA_DIR / "players"
FIXTURES_DIR = DATA_DIR / "fixtures"
LINEUPS_DIR = DATA_DIR / "lineups"
ODDS_DIR = DATA_DIR / "odds"
PROJECTIONS_DIR = DATA_DIR / "projections"
TEAMS_DIR = DATA_DIR / "teams"
RESULTS_DIR = DATA_DIR / "results"
HISTORY_FILE = DATA_DIR / "history.csv"

PLAYERS_FILE = PLAYERS_DIR / "players.csv"
SAMPLE_PLAYERS_FILE = PLAYERS_DIR / "players.sample.csv"

# Runtime dirs that hold generated artefacts.
_RUNTIME_DIRS = [ODDS_DIR, PROJECTIONS_DIR, TEAMS_DIR, RESULTS_DIR,
                 PLAYERS_DIR, FIXTURES_DIR, LINEUPS_DIR]


def ensure_dirs() -> None:
    for d in _RUNTIME_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# .env loading (no external dependency)
# --------------------------------------------------------------------------- #
def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ (without overriding)."""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
POSITIONS = ("GK", "DEF", "MID", "FWD")

# Squad composition: exactly these many of each across the 15-man squad.
SQUAD_COMPOSITION: Dict[str, int] = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_SIZE = sum(SQUAD_COMPOSITION.values())  # 15
STARTING_XI = 11

# Starting-XI formation ranges. The full set of legal (DEF, MID, FWD) tuples that
# satisfy these ranges with exactly 1 GK and 11 players equals the game's listed
# formations (3-4-3, 3-5-2, 4-3-3, 4-4-2, 4-5-1, 5-3-2, 5-4-1), so range
# constraints are sufficient in the optimizer.
FORMATION_LIMITS = {
    "GK": (1, 1),
    "DEF": (3, 5),
    "MID": (3, 5),
    "FWD": (1, 3),
}

# Sport key for The Odds API.
ODDS_SPORT_KEY = "soccer_fifa_world_cup"


# --------------------------------------------------------------------------- #
# Per-round structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RoundSpec:
    key: str                 # short id used on the CLI / filenames
    name: str                # human label
    stage: str               # group | knockout
    budget: float            # squad budget for this round ($m)
    nation_limit: int        # max players from one nation
    free_transfers: int      # free transfers granted before this round (-1 = unlimited)
    wildcard_allowed: bool    # may the Wildcard chip be used this round


# Source: FIFA World Cup 2026 Fantasy rules (play.fifa.com/fantasy/help/guidelines),
# cross-checked against Fantasy Football Scout and Sportsdunia write-ups (2026-06).
UNLIMITED = -1
ROUNDS: List[RoundSpec] = [
    RoundSpec("MD1", "Matchday 1", "group", 100.0, 3, UNLIMITED, False),
    RoundSpec("MD2", "Matchday 2", "group", 100.0, 3, 2, True),
    RoundSpec("MD3", "Matchday 3", "group", 100.0, 3, 2, True),
    RoundSpec("R32", "Round of 32", "knockout", 105.0, 3, UNLIMITED, False),
    RoundSpec("R16", "Round of 16", "knockout", 105.0, 4, 4, True),
    RoundSpec("QF", "Quarter-finals", "knockout", 105.0, 5, 4, True),
    RoundSpec("SF", "Semi-finals", "knockout", 105.0, 6, 5, True),
    RoundSpec("FIN", "Final", "knockout", 105.0, 8, 6, True),
]
ROUNDS_BY_KEY: Dict[str, RoundSpec] = {r.key: r for r in ROUNDS}


def get_round(key: str) -> RoundSpec:
    key = key.upper()
    if key not in ROUNDS_BY_KEY:
        raise KeyError(
            f"Unknown round '{key}'. Valid rounds: {', '.join(ROUNDS_BY_KEY)}"
        )
    return ROUNDS_BY_KEY[key]


# Hit cost for each transfer beyond the free allocation.
TRANSFER_HIT_COST = 3

# A free transfer has option value: one unused transfer rolls over (group stage),
# and churning a player you'd want back wastes future transfers. So only make a
# transfer when it improves the round by more than this many points; otherwise
# bank it. Acts as a per-transfer opportunity cost in the optimiser.
TRANSFER_VALUE = 1.5
