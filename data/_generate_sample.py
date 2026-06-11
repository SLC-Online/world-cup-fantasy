"""Generate SAMPLE fixture data so the pipeline runs with no API key / real pool.

Produces (all clearly-labelled sample data — replace with the real FIFA pool +
live odds for actual use):
    data/players/players.sample.csv     180 players over 12 nations
    data/fixtures/odds_sample.json       6 MD1 matches, normalized odds shape
    data/lineups/lineups_sample.csv      predicted XIs

The numbers are internally consistent: match odds imply each team's expected
goals, and each attacker's anytime-scorer price is derived from their share of
that, so the expected-points engine recovers sensible values.

Run:  python -m data._generate_sample      (from the project root)
"""
from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYERS_CSV = ROOT / "data" / "players" / "players.sample.csv"
ODDS_JSON = ROOT / "data" / "fixtures" / "odds_sample.json"
LINEUPS_CSV = ROOT / "data" / "lineups" / "lineups_sample.csv"

RNG = random.Random(2026)

# 12 nations with a coarse strength rating.
NATIONS = [
    ("Brazil", 90), ("Argentina", 89), ("France", 88), ("Spain", 87),
    ("Portugal", 86), ("England", 86), ("Germany", 85), ("Netherlands", 84),
    ("Croatia", 80), ("USA", 76), ("Mexico", 75), ("Japan", 74),
]
# MD1 pairings (home, away).
MATCHES = [
    ("Brazil", "Japan"), ("France", "Mexico"), ("England", "USA"),
    ("Spain", "Croatia"), ("Argentina", "Netherlands"), ("Germany", "Portugal"),
]

# Squad shape per nation (gives bench depth for the optimizer).
SHAPE = [("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)]

# Build 256 unique synthetic surnames so names never collide across teams.
_ONSET = ["Mar", "Tav", "Bre", "Cor", "Dal", "Fen", "Gro", "Hal",
          "Kov", "Lim", "Nor", "Pas", "Ros", "Sera", "Vol", "Zan"]
_CODA = ["en", "os", "ic", "ke", "ov", "al", "ander", "ino",
         "sson", "ez", "ard", "ic", "ot", "ius", "by", "ek"]
SURNAMES = []
for o in _ONSET:
    for c in _CODA:
        SURNAMES.append(o + c)
# de-dup while preserving order
SURNAMES = list(dict.fromkeys(SURNAMES))


def poisson_pmf(k, mu):
    return math.exp(-mu) * mu ** k / math.factorial(k)


def outcome_probs(mu_h, mu_a, n=10):
    ph = [poisson_pmf(i, mu_h) for i in range(n)]
    pa = [poisson_pmf(j, mu_a) for j in range(n)]
    h = d = a = 0.0
    for i in range(n):
        for j in range(n):
            p = ph[i] * pa[j]
            if i > j:
                h += p
            elif i == j:
                d += p
            else:
                a += p
    return h, d, a


def prob_over_25(mu_total, n=16):
    p_le2 = sum(poisson_pmf(k, mu_total) for k in range(3))
    return 1 - p_le2


def quoted(p, margin):
    """Fair decimal odds shortened by `margin` to create an overround."""
    p = min(max(p, 1e-3), 0.999)
    return round((1.0 / p) / (1.0 + margin), 2)


def team_mus(str_home, str_away):
    diff = (str_home - str_away) / 30.0
    mu_h = max(0.3, 1.35 + diff + 0.15)   # +0.15 home edge
    mu_a = max(0.25, 1.35 - diff)
    return mu_h, mu_a


def build_players():
    rows = []
    strength = dict(NATIONS)
    for ni, (nation, st) in enumerate(NATIONS):
        idx = 0
        slice_base = ni * 15
        for pos, count in SHAPE:
            for _ in range(count):
                surname = SURNAMES[slice_base + idx]
                idx += 1
                pid = f"{nation[:3].upper()}_{idx:02d}"
                # price: base by position + strength + role noise
                base = {"GK": 4.8, "DEF": 5.3, "MID": 6.3, "FWD": 7.3}[pos]
                price = base + (st - 78) / 8.0 + RNG.uniform(-0.8, 1.6)
                # first-choice attackers of strong teams pricier
                if pos in ("MID", "FWD") and idx % 5 <= 1:
                    price += 1.6
                price = max(4.0, min(11.5, round(price * 2) / 2))
                # latent attack rating for odds generation
                attack = {"GK": 0.02, "DEF": 0.25, "MID": 0.8, "FWD": 1.3}[pos]
                attack *= (0.6 + (st - 70) / 40.0) * RNG.uniform(0.7, 1.3)
                rows.append({
                    "id": pid, "name": surname, "nation": nation,
                    "position": pos, "price": price, "club": "",
                    "_attack": round(attack, 4),
                })
    return rows


def build_odds_and_lineups(players):
    by_nation = {}
    for p in players:
        by_nation.setdefault(p["nation"], []).append(p)
    strength = dict(NATIONS)

    matches_out = []
    lineup_rows = []
    base_time = "2026-06-11T"
    for mi, (home, away) in enumerate(MATCHES):
        mu_h, mu_a = team_mus(strength[home], strength[away])
        ph, pd, pa = outcome_probs(mu_h, mu_a)
        over = prob_over_25(mu_h + mu_a)

        anytime = {}
        for nation, mu in ((home, mu_h), (away, mu_a)):
            roster = by_nation[nation]
            atk_sum = sum(p["_attack"] for p in roster)
            # ~85% of goals come from these listed players
            for p in roster:
                share = p["_attack"] / atk_sum if atk_sum else 0
                lam = mu * 0.85 * share
                if p["position"] in ("MID", "FWD", "DEF") and lam > 0.02:
                    p_any = 1 - math.exp(-lam)
                    anytime[p["name"]] = quoted(p_any, 0.10)

        matches_out.append({
            "match_id": f"S{mi+1}",
            "home": home, "away": away,
            "commence_time": f"{base_time}{18 + mi % 3:02d}:00:00Z",
            "h2h": {"home": quoted(ph, 0.05),
                    "draw": quoted(pd, 0.05),
                    "away": quoted(pa, 0.05)},
            "totals": {"line": 2.5,
                       "over": quoted(over, 0.04),
                       "under": quoted(1 - over, 0.04)},
            "anytime": anytime,
        })

        # Predicted XI: first of each (1 GK, 4 DEF, 4 MID, 2 FWD) start.
        for nation in (home, away):
            roster = by_nation[nation]
            start_quota = {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}
            seen = {k: 0 for k in start_quota}
            for p in roster:
                pos = p["position"]
                if seen[pos] < start_quota[pos]:
                    status = "start"
                    seen[pos] += 1
                else:
                    status = "bench"
                lineup_rows.append({"player_id": p["id"], "status": status,
                                    "p_start": "", "exp_minutes": ""})
    return matches_out, lineup_rows


def main():
    PLAYERS_CSV.parent.mkdir(parents=True, exist_ok=True)
    ODDS_JSON.parent.mkdir(parents=True, exist_ok=True)
    LINEUPS_CSV.parent.mkdir(parents=True, exist_ok=True)

    players = build_players()
    matches, lineups = build_odds_and_lineups(players)

    with open(PLAYERS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "nation", "position", "price", "club"])
        for p in players:
            w.writerow([p["id"], p["name"], p["nation"], p["position"],
                        p["price"], p["club"]])

    ODDS_JSON.write_text(json.dumps(matches, indent=2))

    with open(LINEUPS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["player_id", "status", "p_start", "exp_minutes"])
        w.writeheader()
        w.writerows(lineups)

    print(f"Wrote {len(players)} players -> {PLAYERS_CSV}")
    print(f"Wrote {len(matches)} matches -> {ODDS_JSON}")
    print(f"Wrote {len(lineups)} lineup rows -> {LINEUPS_CSV}")


if __name__ == "__main__":
    main()
