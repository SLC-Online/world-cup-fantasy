"""Confirmed line-ups via API-Football (api-sports.io).

Official XIs publish ~1 hour before kickoff. Because the WC game uses rolling
lockouts (each player locks at their own match), you re-run `fetch-lineups`
through the matchday to pick up XIs as they're confirmed, refining your
start/captain choices. Needs a free API-Sports key in .env (API_FOOTBALL_KEY).
"""
from __future__ import annotations

import time
import unicodedata
from typing import Dict, List, Optional, Set

import requests

from .. import config, nations

BASE = "https://v3.football.api-sports.io"
WC_LEAGUE_ID = 1          # FIFA World Cup in API-Football
WC_SEASON = 2026


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


class APIFootballError(RuntimeError):
    pass


class APIFootball:
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": api_key})

    def _get(self, path: str, params: dict) -> list:
        r = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise APIFootballError(f"{r.status_code} {r.text[:200]}")
        body = r.json()
        if body.get("errors"):
            raise APIFootballError(str(body["errors"]))
        return body.get("response", [])

    def fixtures(self) -> List[dict]:
        return self._get("/fixtures", {"league": WC_LEAGUE_ID, "season": WC_SEASON})

    def lineup(self, fixture_id: int) -> list:
        return self._get("/fixtures/lineups", {"fixture": fixture_id})


def confirmed_starters_for_round(round_fixtures: List[dict], fifa_names: list,
                                 api_key: str, max_calls: int = 30
                                 ) -> Dict[str, Set[str]]:
    """Return {fifa_nation: {normalised starter names}} for fixtures with a
    confirmed XI. Fixtures without a published lineup are simply skipped.
    """
    client = APIFootball(api_key)
    af = client.fixtures()
    # Index API-Football fixtures by an unordered, FIFA-normalised team-pair key.
    by_pair = {}
    for f in af:
        h = nations.to_fifa(f["teams"]["home"]["name"], fifa_names)
        a = nations.to_fifa(f["teams"]["away"]["name"], fifa_names)
        by_pair[frozenset((h, a))] = f["fixture"]["id"]

    starters: Dict[str, Set[str]] = {}
    calls = 0
    for m in round_fixtures:
        if calls >= max_calls:
            break
        fid = by_pair.get(frozenset((m["home"], m["away"])))
        if not fid:
            continue
        try:
            lus = client.lineup(fid)
            calls += 1
            time.sleep(0.2)
        except APIFootballError:
            continue
        for lu in lus:
            team_fifa = nations.to_fifa(lu.get("team", {}).get("name", ""), fifa_names)
            names = {_norm(p["player"]["name"])
                     for p in lu.get("startXI", []) if p.get("player")}
            if names:
                starters[team_fifa] = names
    return starters
