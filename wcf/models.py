"""Core data structures shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Player:
    """A FIFA Fantasy player. Price/position/nation are fixed all tournament."""
    id: str
    name: str
    nation: str
    position: str          # GK | DEF | MID | FWD
    price: float           # $m, fixed for the whole tournament
    club: str = ""         # optional, informational only
    full_name: str = ""    # legal name (firstName + lastName) for odds matching
    status: str = "playing"   # FIFA availability: playing | transferred | ...
    ownership: float = 0.0    # FIFA percentSelected — crowd signal of a nailed starter

    def __post_init__(self) -> None:
        if self.position not in ("GK", "DEF", "MID", "FWD"):
            raise ValueError(f"{self.name}: bad position {self.position!r}")

    @property
    def available(self) -> bool:
        return self.status == "playing"


@dataclass
class Match:
    """A single fixture within a round."""
    id: str
    round: str
    home: str              # nation name (must match Player.nation + odds team names)
    away: str
    kickoff: str = ""      # ISO timestamp, optional


@dataclass
class MatchGoals:
    """Model output: expected goals for each side of a match."""
    match_id: str
    home: str
    away: str
    mu_home: float
    mu_away: float

    def mu_for(self, nation: str) -> float:
        if nation == self.home:
            return self.mu_home
        if nation == self.away:
            return self.mu_away
        raise KeyError(f"{nation} not in match {self.match_id}")

    def mu_against(self, nation: str) -> float:
        if nation == self.home:
            return self.mu_away
        if nation == self.away:
            return self.mu_home
        raise KeyError(f"{nation} not in match {self.match_id}")

    def opponent_of(self, nation: str) -> str:
        return self.away if nation == self.home else self.home


@dataclass
class PlayerProjection:
    """Expected points for one player in one round, with a component breakdown."""
    player_id: str
    name: str
    nation: str
    position: str
    price: float
    match_id: str
    opponent: str
    p_start: float
    exp_minutes: float
    exp_points: float
    components: Dict[str, float] = field(default_factory=dict)

    @property
    def value(self) -> float:
        """Expected points per $m — a quick efficiency heuristic."""
        return self.exp_points / self.price if self.price else 0.0


@dataclass
class Selection:
    """A chosen squad for a round: 15 players, an XI, captain and vice."""
    round: str
    squad: List[str]                  # 15 player ids
    starters: List[str]               # 11 player ids (subset of squad)
    bench: List[str]                  # 4 player ids, in auto-sub priority order
    captain: str
    vice: str
    formation: str                    # e.g. "3-4-3"
    chip: Optional[str] = None        # wildcard | 12thman | maxcaptain | ...
    expected_points: float = 0.0
    cost: float = 0.0
    transfers_made: int = 0
    hit_points: int = 0               # points docked for transfers beyond free
