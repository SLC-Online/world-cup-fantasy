"""The Odds API provider.

Pulls 1X2 + totals (featured markets) and anytime-goalscorer (player props) for
the World Cup, normalises across bookmakers (median price per outcome) and writes
a timestamped snapshot. Falls back to a bundled fixture when no API key is set,
so the whole pipeline runs offline.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .. import config
from .. import nations

BASE_URL = "https://api.the-odds-api.com/v4"
FEATURED_MARKETS = ["h2h", "totals"]
PLAYER_MARKETS = ["player_goal_scorer_anytime"]
SAMPLE_ODDS_FILE = config.FIXTURES_DIR / "odds_sample.json"


class OddsAPIError(RuntimeError):
    pass


class OddsAPIClient:
    def __init__(self, api_key: str = "", regions: str = "uk,eu",
                 odds_format: str = "decimal", timeout: int = 20):
        self.api_key = api_key
        self.regions = regions
        self.odds_format = odds_format
        self.timeout = timeout

    # -- low-level ---------------------------------------------------------- #
    def _get(self, path: str, params: Dict) -> object:
        params = {**params, "apiKey": self.api_key}
        resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise OddsAPIError(f"{resp.status_code} {resp.text[:200]}")
        return resp.json()

    def featured_odds(self, sport: str = config.ODDS_SPORT_KEY) -> List[dict]:
        return self._get(f"/sports/{sport}/odds", {
            "regions": self.regions,
            "markets": ",".join(FEATURED_MARKETS),
            "oddsFormat": self.odds_format,
        })

    def event_player_props(self, event_id: str,
                           sport: str = config.ODDS_SPORT_KEY,
                           regions: Optional[str] = None) -> dict:
        return self._get(f"/sports/{sport}/events/{event_id}/odds", {
            "regions": regions or self.regions,
            "markets": ",".join(PLAYER_MARKETS),
            "oddsFormat": self.odds_format,
        })


# --------------------------------------------------------------------------- #
# Normalisation (median across bookmakers)
# --------------------------------------------------------------------------- #
def _median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v and v > 0]
    return statistics.median(vals) if vals else None


def normalize_event(event: dict, props_event: Optional[dict] = None) -> dict:
    """Collapse one event's bookmaker markets into a single median-priced view."""
    home, away = event.get("home_team", ""), event.get("away_team", "")
    h2h_home, h2h_draw, h2h_away = [], [], []
    totals_over, totals_under, totals_lines = [], [], []

    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            key = market.get("key")
            outcomes = market.get("outcomes", [])
            if key == "h2h":
                for o in outcomes:
                    if o["name"] == home:
                        h2h_home.append(o["price"])
                    elif o["name"] == away:
                        h2h_away.append(o["price"])
                    else:
                        h2h_draw.append(o["price"])
            elif key == "totals":
                # Prefer the 2.5 line; collect all then pick the modal line.
                for o in outcomes:
                    pt = o.get("point")
                    if pt is None:
                        continue
                    totals_lines.append(pt)
                    if o["name"].lower() == "over":
                        totals_over.append((pt, o["price"]))
                    elif o["name"].lower() == "under":
                        totals_under.append((pt, o["price"]))

    # Choose a consensus totals line (closest to 2.5 among those present).
    line = None
    over = under = None
    if totals_lines:
        line = min(set(totals_lines), key=lambda x: (abs(x - 2.5), x))
        over = _median([p for (pt, p) in totals_over if pt == line])
        under = _median([p for (pt, p) in totals_under if pt == line])

    out = {
        "match_id": event.get("id", f"{home}-{away}"),
        "home": home,
        "away": away,
        "commence_time": event.get("commence_time", ""),
        "h2h": {
            "home": _median(h2h_home),
            "draw": _median(h2h_draw),
            "away": _median(h2h_away),
        },
        "totals": {"line": line, "over": over, "under": under},
        "anytime": {},
    }

    if props_event:
        out["anytime"] = extract_anytime(props_event)

    return out


def extract_anytime(props_event: dict) -> Dict[str, float]:
    """Median anytime-goalscorer price per player from an event-odds response."""
    anytime_raw: Dict[str, List[float]] = {}
    for bm in props_event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "player_goal_scorer_anytime":
                continue
            for o in market.get("outcomes", []):
                # Player name is in 'description'; 'name' is usually "Yes".
                player = o.get("description") or o.get("name")
                if not player or str(player).lower() in ("yes", "no"):
                    continue
                anytime_raw.setdefault(player, []).append(o["price"])
    return {p: _median(v) for p, v in anytime_raw.items() if _median(v)}


# --------------------------------------------------------------------------- #
# Top-level fetch with snapshotting + fixture fallback
# --------------------------------------------------------------------------- #
def fetch_round_odds(round_key: str, client: Optional[OddsAPIClient] = None,
                     with_player_props: bool = True,
                     prop_regions: Optional[str] = None) -> List[dict]:
    """Fetch + normalise odds for a round and snapshot them. Returns matches.

    Featured markets (1X2/totals) are pulled for every priced fixture, but the
    (more expensive) goalscorer props are only bought for this round's fixtures —
    i.e. each nation's next game — to conserve the API quota. With no API key (or
    a network error) it loads the bundled sample so the pipeline still runs.
    """
    config.ensure_dirs()
    api_key = config.get_env("ODDS_API_KEY")

    if not api_key:
        print("[odds] No ODDS_API_KEY set — using bundled sample odds.")
        return _load_sample_odds()

    client = client or OddsAPIClient(
        api_key=api_key,
        regions=config.get_env("ODDS_REGIONS", "uk,eu"),
        odds_format=config.get_env("ODDS_FORMAT", "decimal"),
    )
    try:
        events = client.featured_odds()
    except OddsAPIError as e:
        print(f"[odds] API error ({e}); falling back to sample odds.")
        return _load_sample_odds()

    fifa_names = _load_fifa_nation_names()
    matches = []
    for event in events:
        m = normalize_event(event)
        m["home"] = nations.to_fifa(m["home"], fifa_names)
        m["away"] = nations.to_fifa(m["away"], fifa_names)
        matches.append(m)

    # This round's fixtures = each nation's earliest game in the feed.
    round_matches = _first_match_per_nation(matches)

    if with_player_props:
        pr = prop_regions or config.get_env(
            "ODDS_PROP_REGIONS", config.get_env("ODDS_REGIONS", "uk,eu"))
        got = 0
        for m in round_matches:
            try:
                props = client.event_player_props(m["match_id"], regions=pr)
                m["anytime"] = extract_anytime(props)
                got += 1 if m["anytime"] else 0
                time.sleep(0.2)   # be gentle on the API
            except OddsAPIError:
                pass
        print(f"[odds] goalscorer props attached for {got}/{len(round_matches)} "
              "round fixtures.")

    _snapshot(round_key, matches)
    return matches


def _first_match_per_nation(matches: List[dict]) -> List[dict]:
    """Earliest fixture for each nation (greedy by kickoff). ~24 for a group MD."""
    chosen, seen = [], set()
    for m in sorted(matches, key=lambda x: x.get("commence_time", "")):
        if m["home"] in seen or m["away"] in seen:
            continue
        chosen.append(m)
        seen.add(m["home"])
        seen.add(m["away"])
    return chosen


def _load_fifa_nation_names():
    import csv
    try:
        with open(config.PLAYERS_FILE, newline="", encoding="utf-8") as f:
            return sorted({row["nation"] for row in csv.DictReader(f)})
    except Exception:
        return None


def _snapshot(round_key: str, matches: List[dict]) -> Path:
    config.ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.ODDS_DIR / f"odds_{round_key}_{ts}.json"
    path.write_text(json.dumps(matches, indent=2))
    # Also write/refresh a 'latest' pointer for convenience.
    (config.ODDS_DIR / f"odds_{round_key}_latest.json").write_text(
        json.dumps(matches, indent=2))
    print(f"[odds] snapshot written: {path.name} ({len(matches)} matches)")
    return path


def load_latest_snapshot(round_key: str) -> Optional[List[dict]]:
    path = config.ODDS_DIR / f"odds_{round_key}_latest.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def load_group_snapshot() -> Optional[List[dict]]:
    """The shared group-stage snapshot (the saved odds file with the most matches —
    i.e. all 72 group fixtures). Group matchdays reuse one snapshot: round-aware
    projection picks each team's MDn fixture from it, so any group round can be
    built from committed data without a fresh fetch (works on the cloud, no key)."""
    best = None
    for p in config.ODDS_DIR.glob("odds_MD*_latest.json"):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if best is None or len(data) > len(best):
            best = data
    return best


def _load_sample_odds() -> List[dict]:
    if SAMPLE_ODDS_FILE.exists():
        return json.loads(SAMPLE_ODDS_FILE.read_text())
    return []
