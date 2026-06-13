"""Player availability (injuries / suspensions).

The FIFA feed's ``status`` only reflects squad/transfer state, not fitness, and
there's no free real-time injury API — so known injuries (e.g. a player out for
weeks) otherwise sail through as selectable. This maintained list, seeded from
team news, is applied automatically and round-awarely: a player is treated as
unavailable for every round *before* their ``available_from`` round. Emerging /
in-tournament absences are then caught by the appearances signal (who actually
played) and the confirmed-line-up window.
"""
from __future__ import annotations

import csv
from typing import List

from .. import config

AVAILABILITY_FILE = config.FIXTURES_DIR / "availability.csv"


def _rows() -> List[dict]:
    if not AVAILABILITY_FILE.exists():
        return []
    lines = [ln for ln in AVAILABILITY_FILE.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return [r for r in csv.DictReader(lines) if r.get("name")]


def _round_index(round_key: str) -> int:
    order = {r.key: i for i, r in enumerate(config.ROUNDS)}
    return order.get((round_key or "").upper(), 0)


def out_tokens_for_round(round_key: str) -> List[str]:
    """Lower-cased name tokens of players unavailable for `round_key` (i.e. their
    return round is later than this one, or blank = out all tournament)."""
    order = {r.key: i for i, r in enumerate(config.ROUNDS)}
    ri = _round_index(round_key)
    out = []
    for r in _rows():
        af = (r.get("available_from") or "").upper().strip()
        afi = order.get(af, 99)          # blank/unknown -> out the whole tournament
        if afi > ri:
            out.append(r["name"].strip().lower())
    return out
