"""Local data store: projections, chosen teams, results and running history.

Everything is plain CSV/JSON under ``data/`` so it's easy to inspect, back up
(it's in your Dropbox tree) and diff. Nothing here needs a database.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from . import config
from .models import PlayerProjection, Selection
from .scoring import DEFAULT_RULES, ScoringRules, StatLine, score_statline


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #
def save_projections(round_key: str, projections: List[PlayerProjection]) -> Path:
    config.ensure_dirs()
    path = config.PROJECTIONS_DIR / f"projections_{round_key}.csv"
    cols = ["player_id", "name", "nation", "position", "price", "opponent",
            "p_start", "exp_minutes", "exp_points", "value", "components"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in sorted(projections, key=lambda x: x.exp_points, reverse=True):
            w.writerow([p.player_id, p.name, p.nation, p.position, p.price,
                        p.opponent, round(p.p_start, 2), round(p.exp_minutes, 1),
                        round(p.exp_points, 3), round(p.value, 3),
                        json.dumps(p.components)])
    return path


def load_projections(round_key: str) -> List[PlayerProjection]:
    path = config.PROJECTIONS_DIR / f"projections_{round_key}.csv"
    if not path.exists():
        raise FileNotFoundError(f"No projections for {round_key}; run `project` first.")
    out: List[PlayerProjection] = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(PlayerProjection(
                player_id=r["player_id"], name=r["name"], nation=r["nation"],
                position=r["position"], price=float(r["price"]),
                match_id="", opponent=r["opponent"], p_start=float(r["p_start"]),
                exp_minutes=float(r["exp_minutes"]), exp_points=float(r["exp_points"]),
                components=json.loads(r["components"]) if r.get("components") else {}))
    return out


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #
def team_path(round_key: str) -> Path:
    return config.TEAMS_DIR / f"team_{round_key}.json"


def save_team(round_key: str, selection: Selection,
              projections: List[PlayerProjection]) -> Path:
    config.ensure_dirs()
    selection.round = round_key
    by_id = {p.player_id: p for p in projections}

    def describe(pid: str) -> dict:
        p = by_id.get(pid)
        return {
            "id": pid,
            "name": p.name if p else pid,
            "nation": p.nation if p else "",
            "position": p.position if p else "",
            "price": p.price if p else 0.0,
            "exp_points": round(p.exp_points, 2) if p else 0.0,
            "is_captain": pid == selection.captain,
            "is_vice": pid == selection.vice,
        }

    payload = {
        "round": round_key,
        "formation": selection.formation,
        "chip": selection.chip,
        "expected_points": selection.expected_points,
        "cost": selection.cost,
        "transfers_made": selection.transfers_made,
        "hit_points": selection.hit_points,
        "captain": selection.captain,
        "vice": selection.vice,
        "starters": [describe(i) for i in selection.starters],
        "bench": [describe(i) for i in selection.bench],
    }
    path = team_path(round_key)
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_team(round_key: str) -> dict:
    path = team_path(round_key)
    if not path.exists():
        raise FileNotFoundError(f"No saved team for {round_key}.")
    return json.loads(path.read_text())


def current_squad_ids(round_key: str) -> Optional[List[str]]:
    """The 15 ids from a saved team (for transfer planning the next round)."""
    try:
        t = load_team(round_key)
    except FileNotFoundError:
        return None
    return [p["id"] for p in t["starters"]] + [p["id"] for p in t["bench"]]


# --------------------------------------------------------------------------- #
# Results scoring (actuals)
# --------------------------------------------------------------------------- #
def load_results(round_key: str, positions: Dict[str, str],
                 rules: ScoringRules = DEFAULT_RULES) -> Dict[str, Dict[str, float]]:
    """Read results_<round>.csv -> {player_id: {points, minutes}}.

    Accepts either a ready 'points' column, or stat columns we score ourselves.
    'minutes' is used for auto-subs.
    """
    path = config.RESULTS_DIR / f"results_{round_key}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"No results file at {path}. Create it with columns "
            "player_id,minutes,points  (or full stat columns).")
    stat_fields = set(StatLine().__dict__.keys())
    out: Dict[str, Dict[str, float]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pid = str(r.get("player_id", "")).strip()
            if not pid:
                continue
            minutes = float(r.get("minutes") or 0)
            if r.get("points") not in (None, ""):
                pts = float(r["points"])
            else:
                kwargs = {k: int(float(r[k])) for k in stat_fields if r.get(k) not in (None, "")}
                kwargs["minutes"] = int(minutes)
                pts, _ = score_statline(StatLine(**kwargs),
                                        positions.get(pid, "MID"), rules)
            out[pid] = {"points": pts, "minutes": minutes}
    return out


def score_round(round_key: str) -> dict:
    """Score a saved team against actual results, applying auto-subs + captaincy."""
    team = load_team(round_key)
    positions = {p["id"]: p["position"]
                 for p in team["starters"] + team["bench"]}
    results = load_results(round_key, positions)

    def pts(pid: str) -> float:
        return results.get(pid, {}).get("points", 0.0)

    def played(pid: str) -> bool:
        return results.get(pid, {}).get("minutes", 0) > 0

    starters = [p["id"] for p in team["starters"]]
    bench = [p["id"] for p in team["bench"]]
    start_pos = {p["id"]: p["position"] for p in team["starters"]}

    # Auto-subs: replace non-playing starters with bench players keeping a legal XI.
    final_xi = [pid for pid in starters if played(pid)]
    dnp = [pid for pid in starters if not played(pid)]
    used_bench = []
    for out_pid in dnp:
        for b in bench:
            if b in used_bench or not played(b):
                continue
            candidate = final_xi + [b]
            if _formation_ok(candidate, positions):
                final_xi.append(b)
                used_bench.append(b)
                break

    captain = team["captain"]
    cap_used = captain if played(captain) else team["vice"]
    captain_extra = pts(cap_used) if played(cap_used) else 0.0

    base = sum(pts(pid) for pid in final_xi)
    hit = team.get("hit_points", 0)
    total = base + captain_extra - hit

    return {
        "round": round_key,
        "base_points": round(base, 1),
        "captain_used": cap_used,
        "captain_extra": round(captain_extra, 1),
        "auto_subs": used_bench,
        "hit_points": hit,
        "total_points": round(total, 1),
        "expected_points": team.get("expected_points", 0.0),
    }


def _formation_ok(xi_ids: List[str], positions: Dict[str, str]) -> bool:
    counts = {pp: 0 for pp in config.POSITIONS}
    for pid in xi_ids:
        counts[positions.get(pid, "MID")] += 1
    if len(xi_ids) > config.STARTING_XI:
        return False
    for pp, (lo, hi) in config.FORMATION_LIMITS.items():
        if counts[pp] > hi:
            return False
    return True


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
HISTORY_COLS = ["round", "expected_points", "total_points", "base_points",
                "captain_extra", "hit_points", "cost", "chip"]


def update_history(round_score: dict, chip: Optional[str] = None,
                   cost: float = 0.0) -> Path:
    config.ensure_dirs()
    rows = load_history()
    rows = [r for r in rows if r.get("round") != round_score["round"]]
    rows.append({
        "round": round_score["round"],
        "expected_points": round_score.get("expected_points", 0.0),
        "total_points": round_score.get("total_points", 0.0),
        "base_points": round_score.get("base_points", 0.0),
        "captain_extra": round_score.get("captain_extra", 0.0),
        "hit_points": round_score.get("hit_points", 0),
        "cost": cost,
        "chip": chip or "",
    })
    order = {r.key: i for i, r in enumerate(config.ROUNDS)}
    rows.sort(key=lambda r: order.get(r["round"], 99))
    with open(config.HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
        w.writeheader()
        w.writerows(rows)
    return config.HISTORY_FILE


def load_history() -> List[dict]:
    if not config.HISTORY_FILE.exists():
        return []
    with open(config.HISTORY_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- #
# Ownership history (for momentum: low-but-rising = the crowd moving in)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Persistent exclusions (e.g. injuries) — respected by every optimise/auto-pilot
# --------------------------------------------------------------------------- #
EXCLUDE_FILE = config.DATA_DIR / "exclude.txt"


def load_exclusions() -> List[str]:
    if not EXCLUDE_FILE.exists():
        return []
    return [ln.strip().lower() for ln in EXCLUDE_FILE.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


def set_exclusions(tokens: List[str]) -> None:
    config.ensure_dirs()
    EXCLUDE_FILE.write_text(
        "\n".join(sorted({t.strip() for t in tokens if t.strip()})) + "\n")


OWNERSHIP_FILE = config.DATA_DIR / "ownership_history.csv"


def record_ownership(players, when: Optional[str] = None) -> None:
    """Append a timestamped ownership snapshot for each player."""
    from datetime import datetime, timezone
    config.ensure_dirs()
    ts = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = not OWNERSHIP_FILE.exists()
    with open(OWNERSHIP_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "player_id", "ownership"])
        for p in players:
            w.writerow([ts, p.id, round(p.ownership, 3)])


def ownership_history_span_hours() -> float:
    """Hours between the earliest and latest ownership snapshot (0 if <2 rows)."""
    from datetime import datetime
    if not OWNERSHIP_FILE.exists():
        return 0.0
    ts = []
    with open(OWNERSHIP_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts.append(datetime.fromisoformat(r["ts"]))
            except (ValueError, KeyError):
                continue
    if len(ts) < 2:
        return 0.0
    return (max(ts) - min(ts)).total_seconds() / 3600.0


def ownership_trend(hours: float = 48.0) -> Dict[str, dict]:
    """player_id -> {now, prev, delta, rel} comparing latest ownership to the
    snapshot nearest `hours` ago. Empty/zero deltas until enough history exists.
    """
    from datetime import datetime, timezone, timedelta
    if not OWNERSHIP_FILE.exists():
        return {}
    series: Dict[str, list] = {}
    with open(OWNERSHIP_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(r["ts"])
                series.setdefault(r["player_id"], []).append((t, float(r["ownership"])))
            except (ValueError, KeyError):
                continue
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    out = {}
    for pid, pts in series.items():
        pts.sort()
        latest_t, latest = pts[-1]
        # baseline = last point at/under the cutoff, else the earliest point
        baseline = next((v for t, v in reversed(pts) if t <= cutoff), pts[0][1])
        delta = latest - baseline
        rel = (delta / baseline) if baseline > 0 else 0.0
        out[pid] = {"now": latest, "prev": baseline,
                    "delta": round(delta, 2), "rel": round(rel, 3)}
    return out


# --------------------------------------------------------------------------- #
# Appearances — learn who ACTUALLY plays from completed games. This is the
# self-correcting signal: pre-tournament line-up priors are unreliable (they
# over-trust famous veterans), but once a game is played the FIFA feed tells us
# who featured. A player who scored appearance points started/played; one whose
# team played but who scored nothing was benched. We carry that forward so the
# next round's start probabilities reflect reality, not name recognition.
# --------------------------------------------------------------------------- #
APPEARANCES_FILE = config.DATA_DIR / "appearances.csv"


def _load_appearance_rows() -> List[dict]:
    if not APPEARANCES_FILE.exists():
        return []
    with open(APPEARANCES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def record_appearances(round_key: str, status_by_id: Dict[str, str]) -> Path:
    """Store each player's line-up status ('started' | 'benched') for a round,
    taken from the live FIFA matchStatus. Replaces that round's rows."""
    config.ensure_dirs()
    rows = [r for r in _load_appearance_rows() if r.get("round") != round_key]
    for pid, status in status_by_id.items():
        rows.append({"round": round_key, "player_id": str(pid), "status": status})
    with open(APPEARANCES_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["round", "player_id", "status"])
        w.writeheader()
        w.writerows(rows)
    return APPEARANCES_FILE


def appearance_signal(players=None) -> Dict[str, str]:
    """player_id -> latest known line-up status ('started' | 'benched') across
    completed rounds (most recent wins). This is who ACTUALLY played, so it
    overrides the name-based start prior — the fix for rating a famous backup
    (e.g. a veteran keeper) as a nailed starter. `players` is accepted for call
    compatibility but not needed (matchStatus already encodes the truth).
    """
    rows = _load_appearance_rows()
    if not rows:
        return {}
    order = {rd.key: i for i, rd in enumerate(config.ROUNDS)}
    signal: Dict[str, str] = {}
    for r in sorted(rows, key=lambda x: order.get(x.get("round"), 99)):
        status = (r.get("status") or "").strip()
        if status:
            signal[str(r["player_id"])] = status   # later round overwrites
    return signal
