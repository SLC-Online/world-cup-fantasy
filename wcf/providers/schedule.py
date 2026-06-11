"""Fixture schedule + kickoff (lock) times from FIFA's public rounds feed.

Used to support the game's rolling lockouts: your squad/transfers lock at the
matchday's first kickoff, but each player's XI/captain status locks only at their
own match. This tells you when each of your players locks, so you know the window
to apply confirmed line-ups.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import requests

from . import players as players_provider  # noqa: F401  (kept for symmetry)
from .. import config, nations

ROUNDS_URL = "https://play.fifa.com/json/fantasy/rounds.json"
CACHE = config.FIXTURES_DIR / "fifa_rounds.json"


def fetch_rounds(force: bool = False) -> list:
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text())
    data = requests.get(ROUNDS_URL, headers={"User-Agent": "Mozilla/5.0"},
                        timeout=25).json()
    config.ensure_dirs()
    CACHE.write_text(json.dumps(data))
    return data


def _round_index(round_key: str) -> int:
    keys = [r.key for r in config.ROUNDS]
    return keys.index(round_key.upper())


def fixtures(round_key: str, fifa_names: Optional[list] = None) -> List[dict]:
    """Return this round's fixtures: home/away (FIFA spelling), kickoff, status."""
    rounds = sorted(fetch_rounds(), key=lambda r: r.get("startDate", ""))
    idx = _round_index(round_key)
    if idx >= len(rounds):
        return []
    out = []
    for t in rounds[idx].get("tournaments", []):
        home = nations.to_fifa(t.get("homeSquadName", ""), fifa_names)
        away = nations.to_fifa(t.get("awaySquadName", ""), fifa_names)
        out.append({
            "match_id": str(t.get("id", f"{home}-{away}")),
            "home": home, "away": away,
            "kickoff": t.get("date", ""),
            "venue": t.get("venueCity", ""),
            "status": t.get("status", ""),
        })
    out.sort(key=lambda m: m["kickoff"])
    return out


def round_deadline(round_key: str) -> str:
    """The squad/transfer deadline = the round's first kickoff."""
    rounds = sorted(fetch_rounds(), key=lambda r: r.get("startDate", ""))
    idx = _round_index(round_key)
    return rounds[idx].get("startDate", "") if idx < len(rounds) else ""


def round_id_for(round_key: str):
    """FIFA's internal round id for one of our round keys (for results lookup)."""
    rounds = sorted(fetch_rounds(), key=lambda r: r.get("startDate", ""))
    keys = [r.key for r in config.ROUNDS]
    idx = keys.index(round_key.upper())
    return rounds[idx].get("id") if idx < len(rounds) else None


def nation_kickoffs(round_key: str, fifa_names: Optional[list] = None) -> Dict[str, dict]:
    """nation -> its fixture (opponent, kickoff) for this round."""
    out = {}
    for m in fixtures(round_key, fifa_names):
        out[m["home"]] = {"opponent": m["away"], "kickoff": m["kickoff"],
                          "home": True, "match_id": m["match_id"]}
        out[m["away"]] = {"opponent": m["home"], "kickoff": m["kickoff"],
                          "home": False, "match_id": m["match_id"]}
    return out
