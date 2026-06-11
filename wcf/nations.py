"""Reconcile nation names between data sources.

The Odds API and the FIFA feed spell some countries differently. We canonicalise
to the FIFA squad names (what `players.csv` uses), so odds matches join cleanly
to players.
"""
from __future__ import annotations

import unicodedata
from typing import Dict, Iterable, Optional


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum())


# Odds-API spelling -> FIFA spelling (only the ones that differ).
_ALIASES = {
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Cape Verde": "Cabo Verde",
    "Czech Republic": "Czechia",
    "DR Congo": "Congo DR",
    "Democratic Republic of the Congo": "Congo DR",
    "Iran": "IR Iran",
    "Ivory Coast": "Côte d'Ivoire",
    "South Korea": "Korea Republic",
    "Republic of Korea": "Korea Republic",
    "Turkey": "Türkiye",
    "United States": "USA",
    "United States of America": "USA",
    "Curacao": "Curaçao",
}
_ALIAS_NORM = {_norm(k): v for k, v in _ALIASES.items()}


def to_fifa(name: str, fifa_names: Optional[Iterable[str]] = None) -> str:
    """Map an external nation name to its FIFA spelling.

    Resolution order: explicit alias -> exact match in the provided FIFA name set
    -> normalised match against the FIFA set -> the original name unchanged.
    """
    key = _norm(name)
    if key in _ALIAS_NORM:
        return _ALIAS_NORM[key]
    if fifa_names:
        names = list(fifa_names)
        for n in names:
            if n == name:
                return n
        norm_map = {_norm(n): n for n in names}
        if key in norm_map:
            return norm_map[key]
    return name
