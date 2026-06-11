"""The FIFA player pool (price / position / nation).

Because World Cup Fantasy prices are FIXED for the whole tournament, you capture
this ONCE and reuse it for all 8 rounds. Three ways to populate it, in order of
convenience:

  1. ``players.csv`` — the canonical local file (columns below). Edit by hand or
     generate from either of the next two.
  2. ``parse_fifa_json`` — point it at a JSON response you saved from the game's
     network traffic (DevTools → Network → the players request → Copy response).
     Adjust FIELD_MAP if FIFA's keys differ from the defaults.
  3. ``fetch_players`` — if you put a working URL + auth header in .env.

CSV schema: id,name,nation,position,price,club
"""
from __future__ import annotations

import csv
import base64
import json
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .. import config
from ..models import Player

# Map FIFA position codes/labels onto our four positions. Extend as needed.
POSITION_MAP = {
    "1": "GK", "2": "DEF", "3": "MID", "4": "FWD",
    "gk": "GK", "goalkeeper": "GK", "keeper": "GK",
    "def": "DEF", "defender": "DEF", "d": "DEF",
    "mid": "MID", "midfielder": "MID", "m": "MID",
    "fwd": "FWD", "forward": "FWD", "att": "FWD", "attacker": "FWD", "f": "FWD",
}

# Default mapping from FIFA JSON keys -> our fields. Tune to your captured JSON.
FIELD_MAP = {
    "id": ["id", "playerId", "fdId", "code"],
    "name": ["name", "fullName", "displayName", "knownName", "webName"],
    "nation": ["nation", "country", "teamName", "team", "countryName"],
    "position": ["position", "positionId", "role", "skill", "positionName"],
    "price": ["price", "value", "cost", "marketValue"],
    "club": ["club", "clubName"],
}


def _first(d: dict, keys: List[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def normalize_position(raw) -> str:
    key = str(raw).strip().lower()
    if key in POSITION_MAP:
        return POSITION_MAP[key]
    up = str(raw).strip().upper()
    if up in ("GK", "DEF", "MID", "FWD"):
        return up
    raise ValueError(f"Cannot map position {raw!r}; extend POSITION_MAP.")


# --------------------------------------------------------------------------- #
# CSV load / save
# --------------------------------------------------------------------------- #
def load_players(path: Optional[Path] = None) -> List[Player]:
    """Load the player pool. Falls back to the sample pool if no real file."""
    path = Path(path) if path else config.PLAYERS_FILE
    if not path.exists():
        if config.SAMPLE_PLAYERS_FILE.exists():
            print(f"[players] {path.name} not found — using sample pool. "
                  "Replace with the real FIFA pool (see README).")
            path = config.SAMPLE_PLAYERS_FILE
        else:
            raise FileNotFoundError(
                f"No player pool at {path} and no sample available.")
    players: List[Player] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            players.append(Player(
                id=str(row["id"]).strip(),
                name=row["name"].strip(),
                nation=row["nation"].strip(),
                position=normalize_position(row["position"]),
                price=float(row["price"]),
                club=row.get("club", "").strip(),
                full_name=(row.get("full_name") or "").strip(),
                status=(row.get("status") or "playing").strip(),
                ownership=float(row["ownership"]) if row.get("ownership") else 0.0,
            ))
    _validate_pool(players)
    return players


def save_players(players: List[Player], path: Optional[Path] = None) -> Path:
    path = Path(path) if path else config.PLAYERS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "nation", "position", "price", "club",
                    "full_name", "status", "ownership"])
        for p in players:
            w.writerow([p.id, p.name, p.nation, p.position, p.price, p.club,
                        p.full_name, p.status, p.ownership])
    print(f"[players] wrote {len(players)} players -> {path}")
    return path


def _validate_pool(players: List[Player]) -> None:
    if not players:
        raise ValueError("Empty player pool.")
    ids = [p.id for p in players]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate player ids in pool.")


# --------------------------------------------------------------------------- #
# FIFA JSON parsing / fetching (best-effort, schema-tolerant)
# --------------------------------------------------------------------------- #
def parse_fifa_json(obj, price_divisor: float = 1.0) -> List[Player]:
    """Turn a saved FIFA players JSON into Player objects.

    Tolerates the response being a list, or a dict wrapping the list under common
    keys ('players', 'data', 'items', 'value'). Set price_divisor if prices come
    as integers (e.g. 105 -> 10.5 needs divisor 10).
    """
    if isinstance(obj, dict):
        for key in ("players", "data", "items", "value", "results"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
    if not isinstance(obj, list):
        raise ValueError("Could not locate a list of players in the JSON.")

    players: List[Player] = []
    for i, row in enumerate(obj):
        if not isinstance(row, dict):
            continue
        raw_price = _first(row, FIELD_MAP["price"])
        players.append(Player(
            id=str(_first(row, FIELD_MAP["id"]) or i),
            name=str(_first(row, FIELD_MAP["name"]) or f"Player {i}"),
            nation=str(_first(row, FIELD_MAP["nation"]) or "Unknown"),
            position=normalize_position(_first(row, FIELD_MAP["position"])),
            price=float(raw_price) / price_divisor if raw_price is not None else 0.0,
            club=str(_first(row, FIELD_MAP["club"]) or ""),
        ))
    _validate_pool(players)
    return players


def parse_fifa_json_file(path: Path, price_divisor: float = 1.0) -> List[Player]:
    return parse_fifa_json(json.loads(Path(path).read_text()), price_divisor)


# --------------------------------------------------------------------------- #
# HAR parsing — the easy capture path
# --------------------------------------------------------------------------- #
# A .har is a JSON export of everything your browser fetched. We scan every
# response, find the one that looks like the player list, and parse it. This
# means you don't have to identify the right network request yourself.
def _looks_like_players(lst) -> float:
    """Score how player-list-like a list is (0 = not)."""
    if not isinstance(lst, list) or len(lst) < 30:
        return 0.0
    dict_items = [x for x in lst if isinstance(x, dict)]
    if len(dict_items) < len(lst) * 0.8:
        return 0.0
    keys = set()
    for x in dict_items[:25]:
        keys |= {k.lower() for k in x.keys()}
    score = 0
    if any(k in keys for k in ("name", "fullname", "displayname", "knownname",
                               "webname", "playername", "shortname")):
        score += 1
    if any(("pos" in k) or k in ("skill", "role") for k in keys):
        score += 1
    if any(k in keys for k in ("price", "value", "cost", "marketvalue")):
        score += 1
    return len(lst) * score if score >= 2 else 0.0


def _search_player_arrays(obj, found: list) -> None:
    if isinstance(obj, list):
        s = _looks_like_players(obj)
        if s:
            found.append((s, obj))
        for x in obj:
            if isinstance(x, (list, dict)):
                _search_player_arrays(x, found)
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _search_player_arrays(v, found)


def parse_har(path: Path, price_divisor: float = 1.0) -> List[Player]:
    """Extract the player pool from a browser network export (.har)."""
    har = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    entries = har.get("log", {}).get("entries", [])
    candidates: list = []
    for e in entries:
        content = e.get("response", {}).get("content", {})
        text = content.get("text")
        if not text:
            continue
        if content.get("encoding") == "base64":
            try:
                text = base64.b64decode(text).decode("utf-8", "ignore")
            except Exception:
                continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        _search_player_arrays(data, candidates)
    if not candidates:
        raise ValueError(
            "No player-like data found in the HAR. Make sure you visited the "
            "squad-selection screen before exporting, or use --json with a "
            "copied response instead.")
    best = max(candidates, key=lambda t: t[0])[1]
    print(f"[players] HAR scan: best candidate has {len(best)} entries.")
    return parse_fifa_json(best, price_divisor)


def fetch_players() -> List[Player]:
    """Fetch the pool from a URL configured in .env (FIFA_PLAYERS_URL)."""
    url = config.get_env("FIFA_PLAYERS_URL")
    if not url:
        raise RuntimeError(
            "FIFA_PLAYERS_URL not set. Capture the players request from the game "
            "in DevTools and put its URL (+ optional auth header) in .env, or just "
            "maintain data/players/players.csv by hand.")
    headers = {"User-Agent": "Mozilla/5.0"}
    auth = config.get_env("FIFA_PLAYERS_AUTH_HEADER")
    if auth:
        # Format: "Header-Name: value"
        name, _, value = auth.partition(":")
        headers[name.strip()] = value.strip()
    resp = requests.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    return parse_fifa_json(resp.json())


# --------------------------------------------------------------------------- #
# Public FIFA feed — the easy, repeatable path
# --------------------------------------------------------------------------- #
# These are public static files (no auth). Because prices are fixed for the whole
# tournament you only need to run this once, but it's safe to re-run any time
# (it also refreshes ownership %, player status and the game's own points).
FIFA_PLAYERS_URL_PUBLIC = "https://play.fifa.com/json/fantasy/players.json"
FIFA_SQUADS_URL_PUBLIC = "https://play.fifa.com/json/fantasy/squads.json"


def fetch_public_players(save_raw: bool = True) -> List[Player]:
    """Fetch + join the public FIFA players/squads feeds into Player objects."""
    headers = {"User-Agent": "Mozilla/5.0"}
    squads = requests.get(FIFA_SQUADS_URL_PUBLIC, headers=headers, timeout=25).json()
    raw = requests.get(FIFA_PLAYERS_URL_PUBLIC, headers=headers, timeout=30).json()
    squad_by_id = {s["id"]: s for s in squads}

    players: List[Player] = []
    for r in raw:
        sq = squad_by_id.get(r.get("squadId"), {})
        legal = " ".join(
            x for x in [r.get("firstName"), r.get("lastName")] if x).strip()
        name = r.get("knownName") or legal
        players.append(Player(
            id=str(r["id"]),
            name=name or f"Player {r.get('id')}",
            nation=sq.get("name", "Unknown"),
            position=str(r["position"]).upper(),
            price=float(r["price"]),
            club=sq.get("abbr", ""),
            full_name=legal,
            status=str(r.get("status") or "playing"),
            ownership=float(r.get("percentSelected") or 0.0),
        ))

    if save_raw:
        config.ensure_dirs()
        (config.PLAYERS_DIR / "fifa_players_raw.json").write_text(
            json.dumps(raw), encoding="utf-8")
        (config.PLAYERS_DIR / "fifa_squads.json").write_text(
            json.dumps(squads), encoding="utf-8")
    _validate_pool(players)
    print(f"[players] fetched {len(players)} players across {len(squads)} squads "
          "from the public FIFA feed.")
    return players


def _name_norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


# FIFA matchStatus strings that suggest a player is in the XI (matched liberally;
# the live values are logged on matchday so we can tighten this).
_START_HINTS = ("start", "lineup", "line-up", "confirmed", "onpitch", "xi")


def _extract_round_points(stats: dict, round_id: int):
    """Best-effort per-round points from a player's FIFA `stats` block.

    Handles roundPoints as a list of numbers (indexed by round) or a list of
    {roundId/round/id, points/value} dicts. Returns None if not found.
    """
    rp = stats.get("roundPoints")
    if isinstance(rp, list) and rp:
        if all(isinstance(x, (int, float)) for x in rp):
            if len(rp) >= round_id:
                return rp[round_id - 1]
        else:
            for item in rp:
                if isinstance(item, dict):
                    rid = item.get("roundId") or item.get("round") or item.get("id")
                    val = item.get("points", item.get("value"))
                    if rid == round_id and val is not None:
                        return val
    return None


def fetch_round_points(round_id: int):
    """{player_id: points} for a round from FIFA's live players feed.

    Returns (points_by_id, used_fallback); falls back to lastRoundPoints when the
    feed doesn't break points out per round.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    raw = requests.get(FIFA_PLAYERS_URL_PUBLIC, headers=headers, timeout=30).json()
    points, last = {}, {}
    for r in raw:
        pid = str(r["id"])
        stats = r.get("stats", {}) or {}
        val = _extract_round_points(stats, round_id)
        if val is not None:
            points[pid] = float(val)
        lr = stats.get("lastRoundPoints")
        if lr is not None:
            last[pid] = float(lr)
    if points:
        return points, False
    return last, True


def live_lineup_status():
    """{player_id: 'started'|'benched'} from the feed's live ``matchStatus``.

    Only players whose game is near/under way/finished are included (matchStatus
    is null pre-match). 'started' = named in the XI; 'benched' = named substitute
    / not starting. This is the reliable who-actually-plays signal — unlike FIFA
    points, which conflate minutes with performance (a starting keeper who
    concedes can score as little as a sub).
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    raw = requests.get(FIFA_PLAYERS_URL_PUBLIC, headers=headers, timeout=30).json()
    out: Dict[str, str] = {}
    for r in raw:
        ms = r.get("matchStatus")
        if ms is None:
            continue
        started = any(h in str(ms).lower() for h in _START_HINTS)
        out[str(r["id"])] = "started" if started else "benched"
    return out


def confirmed_from_fifa_feed():
    """Read confirmed XIs from FIFA's live feed via each player's matchStatus.

    Returns (by_nation, status_counts): by_nation maps a FIFA nation to the set of
    normalised starter names. Pre-match, matchStatus is null for everyone, so this
    is empty until ~kickoff; status_counts surfaces the live values.
    """
    import collections
    headers = {"User-Agent": "Mozilla/5.0"}
    squads = requests.get(FIFA_SQUADS_URL_PUBLIC, headers=headers, timeout=25).json()
    raw = requests.get(FIFA_PLAYERS_URL_PUBLIC, headers=headers, timeout=30).json()
    squad_name = {s["id"]: s["name"] for s in squads}

    by_nation: Dict[str, set] = {}
    counts: collections.Counter = collections.Counter()
    for r in raw:
        ms = r.get("matchStatus")
        counts[ms] += 1
        if ms is None:
            continue
        if any(h in str(ms).lower() for h in _START_HINTS):
            nation = squad_name.get(r.get("squadId"))
            name = r.get("knownName") or " ".join(
                x for x in [r.get("firstName"), r.get("lastName")] if x).strip()
            by_nation.setdefault(nation, set()).add(_name_norm(name))
    return by_nation, dict(counts)
