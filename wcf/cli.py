"""Command-line interface.

Typical round workflow (see README for the full story):

    python -m wcf.cli fetch-odds --round MD1      # pull betting markets
    python -m wcf.cli project    --round MD1      # odds + lineups -> expected pts
    python -m wcf.cli optimize   --round MD1      # best 15 / XI / captain
    # ... after the matches ...
    python -m wcf.cli record     --round MD1      # score actuals, update history
    python -m wcf.cli report                      # performance across rounds
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import config, persistence
from . import multiround
from . import risk
from . import notify
from . import opportunities
from .models import PlayerProjection, Selection
from .optimizer import optimize_squad, optimize_horizon
from .projections import build_projections
from .providers import lineups as lineups_provider
from .providers import odds_api
from .providers import players as players_provider
from .providers import schedule as schedule_provider
from .providers import apifootball
from .providers import setpieces


# --------------------------------------------------------------------------- #
# Pipeline helpers
# --------------------------------------------------------------------------- #
def _active_players():
    players = players_provider.load_players()
    active = [p for p in players if p.available]
    dropped = len(players) - len(active)
    if dropped:
        print(f"[players] excluded {dropped} unavailable (e.g. transferred); "
              f"{len(active)} active.")
    return active


def _trend_available() -> bool:
    # Need ~half a day of ownership history before trusting direction-of-travel.
    return persistence.ownership_history_span_hours() >= 12.0


def _resolve_lineups(round_key, players):
    """Manual predicted XI if present, else derive start probs from FIFA data
    (price + ownership trend + who actually played in completed games)."""
    if lineups_provider.lineups_path(round_key).exists():
        return lineups_provider.load_lineups(round_key)
    return lineups_provider.from_pool(
        players, momentum=persistence.ownership_trend(),
        trend_available=_trend_available(),
        appearances=persistence.appearance_signal(players))


def _ensure_odds(round_key: str, refetch: bool = False) -> List[dict]:
    if not refetch:
        cached = odds_api.load_latest_snapshot(round_key)
        if cached:
            print(f"[odds] using cached snapshot for {round_key} "
                  f"({len(cached)} matches).")
            return cached
    return odds_api.fetch_round_odds(round_key)


def _run_projection(round_key: str, refetch: bool = False) -> List[PlayerProjection]:
    players = _active_players()
    matches = _ensure_odds(round_key, refetch=refetch)
    lns = _resolve_lineups(round_key, players)
    projections = build_projections(players, matches, lns,
                                    set_piece_roles=setpieces.roles_for_players(players))
    persistence.save_projections(round_key, projections)
    return projections


def _ensure_projections(round_key: str) -> List[PlayerProjection]:
    try:
        return persistence.load_projections(round_key)
    except FileNotFoundError:
        print(f"[project] no projections for {round_key} yet — building them.")
        return _run_projection(round_key)


# --------------------------------------------------------------------------- #
# Pretty printing
# --------------------------------------------------------------------------- #
def _print_selection(sel: Selection, projections: List[PlayerProjection]):
    by_id = {p.player_id: p for p in projections}
    spec = config.get_round(sel.round) if sel.round else None
    budget = spec.budget if spec else 0.0

    print("\n" + "=" * 64)
    print(f"  {spec.name if spec else sel.round}  |  formation {sel.formation}"
          f"  |  cost ${sel.cost}m" + (f" / ${budget}m" if budget else ""))
    print(f"  projected points: {sel.expected_points}"
          + (f"   (after -{sel.hit_points} hit, {sel.transfers_made} transfers)"
             if sel.hit_points else ""))
    print("=" * 64)

    def line(pid: str, tag: str = ""):
        p = by_id[pid]
        mark = " (C)" if pid == sel.captain else (" (V)" if pid == sel.vice else "")
        print(f"  {p.position:<3} {p.name:<22}{mark:<4} {p.nation:<14} "
              f"${p.price:>4.1f}m  vs {p.opponent:<12} {p.exp_points:>5.2f}")

    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    print("  STARTING XI")
    for pid in sorted(sel.starters, key=lambda i: (order[by_id[i].position],
                                                   -by_id[i].exp_points)):
        line(pid)
    print("  BENCH (auto-sub order)")
    for pid in sel.bench:
        line(pid)
    print("=" * 64)
    cap = by_id[sel.captain]
    vice = by_id[sel.vice]
    print(f"  Captain: {cap.name} ({cap.exp_points:.2f} xP -> "
          f"{cap.exp_points * 2:.2f} doubled)   Vice: {vice.name}")
    print()


def _print_projection_table(projections: List[PlayerProjection], top: int = 25):
    ranked = sorted(projections, key=lambda p: p.exp_points, reverse=True)[:top]
    print(f"\n  Top {top} projected players")
    print("  " + "-" * 62)
    print(f"  {'POS':<4}{'NAME':<22}{'NATION':<14}{'$m':>5}{'vs':>14}{'xP':>6}")
    for p in ranked:
        print(f"  {p.position:<4}{p.name:<22}{p.nation:<14}{p.price:>5.1f}"
              f"{p.opponent:>14}{p.exp_points:>6.2f}")
    print()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_fetch_players(args):
    if args.har:
        players = players_provider.parse_har(args.har, args.price_divisor)
    elif args.json:
        players = players_provider.parse_fifa_json_file(args.json, args.price_divisor)
    elif args.csv:
        players = players_provider.load_players(args.csv)
    elif args.url:
        players = players_provider.fetch_players()
    else:
        # Default: pull the public FIFA feed directly.
        players = players_provider.fetch_public_players()
    players_provider.save_players(players)
    persistence.record_ownership(players)
    print(f"Pool saved: {len(players)} players -> {config.PLAYERS_FILE}")
    return 0


def _parse_dt(s):
    from datetime import datetime
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _fmt_delta(target, now):
    secs = (target - now).total_seconds()
    if secs < 0:
        return "passed"
    d, rem = divmod(int(secs), 86400)
    h, _ = divmod(rem, 3600)
    return (f"{d}d {h}h" if d else f"{h}h {(rem % 3600)//60}m")


def _notify_mac(title, message):
    """Best-effort macOS notification (no-op elsewhere)."""
    import subprocess
    msg = message.replace('"', "'")
    title = title.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}"'],
            check=False, capture_output=True, timeout=10)
    except (FileNotFoundError, OSError):
        pass


def cmd_agenda(args):
    from datetime import datetime
    rounds = sorted(schedule_provider.fetch_rounds(), key=lambda r: r.get("startDate", ""))
    keys = [r.key for r in config.ROUNDS]
    now = datetime.now().astimezone()

    # Owned nations from the most recent saved team (for "your players playing").
    owned, team_round = set(), None
    for rk in reversed(keys):
        try:
            t = persistence.load_team(rk)
            owned = {p["nation"] for p in t["starters"] + t["bench"]}
            team_round = rk
            break
        except FileNotFoundError:
            continue

    # Next squad/transfer deadline (a round's first kickoff).
    next_round = next_dl = None
    for i, r in enumerate(rounds):
        dl = _parse_dt(r.get("startDate"))
        if dl and dl > now and i < len(keys):
            next_round, next_dl = keys[i], dl
            break

    # Round currently in progress (for your players' remaining matches).
    inprog = None
    for i, r in enumerate(rounds):
        sd, ed = _parse_dt(r.get("startDate")), _parse_dt(r.get("endDate"))
        if sd and ed and sd <= now <= ed and i < len(keys):
            inprog = keys[i]
            break

    print(f"\n  WORLD CUP FANTASY — agenda  ({now.strftime('%a %d %b %H:%M')})")
    print("  " + "=" * 50)

    headline = None
    if next_round:
        saved = "✓ team saved" if _team_saved(next_round) else "⚠ NO TEAM YET"
        within = _fmt_delta(next_dl, now)
        print(f"  NEXT DEADLINE: {next_round} squad locks "
              f"{next_dl.strftime('%a %d %b %H:%M')}  (in {within})  [{saved}]")
        if (next_dl - now).total_seconds() < 36 * 3600:
            headline = (f"{next_round} squad locks in {within}"
                        + ("" if _team_saved(next_round) else " — no team saved!"))
            print(f"  → ACTION: run  ./wcf-run optimize --round {next_round}  "
                  "and enter your team.")
    else:
        print("  No upcoming deadlines (tournament finished?).")

    # Your players' remaining matches in the in-progress round (rolling locks).
    cur = inprog or next_round
    if cur and owned:
        fifa_names = sorted(owned)
        upcoming = [m for m in schedule_provider.fixtures(cur, list(owned))
                    if (_parse_dt(m["kickoff"]) or now) > now
                    and (m["home"] in owned or m["away"] in owned)]
        soon = [m for m in upcoming
                if (_parse_dt(m["kickoff"]) - now).total_seconds() < 30 * 3600]
        if soon:
            print(f"\n  YOUR PLAYERS' MATCHES NEXT 30h ({cur}) — confirmed-lineup window:")
            for m in sorted(soon, key=lambda x: x["kickoff"]):
                team = m["home"] if m["home"] in owned else m["away"]
                opp = m["away"] if m["home"] in owned else m["home"]
                k = _parse_dt(m["kickoff"])
                print(f"   {k.strftime('%a %H:%M')} (in {_fmt_delta(k, now)})  "
                      f"{team} v {opp}")
            print("   → run  ./wcf-run fetch-lineups --round "
                  f"{cur}  then re-check XI/captain.")
            if not headline:
                headline = f"{len(soon)} of your players play in the next 30h"
    print()

    if args.notify and headline:
        _notify_mac("WC Fantasy", headline)
    return 0


def _team_saved(round_key: str) -> bool:
    try:
        persistence.load_team(round_key)
        return True
    except FileNotFoundError:
        return False


def _age_hours(path):
    import time
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600.0


def _prev_saved_round(round_key):
    keys = [r.key for r in config.ROUNDS]
    idx = keys.index(round_key.upper())
    for k in reversed(keys[:idx]):
        if _team_saved(k):
            return k
    return None


def _opt_namespace(round_key, from_existing=None):
    import argparse
    return argparse.Namespace(
        round=round_key, horizon=0, planned_transfers=1, from_existing=from_existing,
        chip=None, captain_any=False, captain_risk="balanced", no_save=False,
        exclude=None)


def cmd_run_due(args):
    """Auto-pilot: optimise when a deadline is near, scan for opportunities, notify.
    This is the single command a scheduler (launchd / GitHub Actions) calls."""
    from datetime import datetime
    try:
        rounds = sorted(schedule_provider.fetch_rounds(), key=lambda r: r.get("startDate", ""))
    except Exception as e:
        print(f"[run-due] schedule unavailable: {e}")
        return 1
    keys = [r.key for r in config.ROUNDS]
    now = datetime.now().astimezone()

    next_round = next_dl = None
    for i, r in enumerate(rounds):
        dl = _parse_dt(r.get("startDate"))
        if dl and dl > now and i < len(keys):
            next_round, next_dl = keys[i], dl
            break
    inprog = None
    for i, r in enumerate(rounds):
        sd, ed = _parse_dt(r.get("startDate")), _parse_dt(r.get("endDate"))
        if sd and ed and sd <= now <= ed and i < len(keys):
            inprog = keys[i]
            break

    summary, actionable = [], False

    # 0) Learn who ACTUALLY started in the live/most-recent round from the feed's
    #    matchStatus (reliable start/bench). Persists so the next optimise reflects
    #    real line-ups, not pre-tournament name guesses (the backup-veteran fix).
    learn_round = inprog or next_round
    if learn_round:
        try:
            status = players_provider.live_lineup_status()
            if status:
                persistence.record_appearances(learn_round, status)
                started = sum(1 for v in status.values() if v == "started")
                print(f"[run-due] learned line-ups for {learn_round}: {started} "
                      f"started, {len(status) - started} benched.")
        except Exception as e:
            print(f"[run-due] appearance update skipped: {e}")

    # 1) Auto-optimise if the deadline is within the window.
    if next_round and next_dl and 0 < (next_dl - now).total_seconds() <= args.window * 3600:
        latest = config.ODDS_DIR / f"odds_{next_round}_latest.json"
        age = _age_hours(latest)
        if age is None or age > args.odds_max_age:
            try:
                odds_api.fetch_round_odds(next_round)
            except Exception as e:
                print(f"[run-due] odds fetch failed: {e}")
        prev = None if next_round == "MD1" else _prev_saved_round(next_round)
        try:
            cmd_optimize(_opt_namespace(next_round, from_existing=prev))
            summary.append(f"✅ {next_round} squad optimised & saved — review and enter. "
                           f"Locks in {_fmt_delta(next_dl, now)}.")
            actionable = True
        except Exception as e:
            print(f"[run-due] optimise failed: {e}")
    elif next_round and next_dl and (next_dl - now).total_seconds() < 36 * 3600:
        summary.append(f"⏰ {next_round} squad locks in {_fmt_delta(next_dl, now)}.")
        actionable = True

    # 2) Opportunity scan for the active round.
    cur = inprog or next_round
    if cur and _team_saved(cur):
        team = persistence.load_team(cur)
        players = _active_players()
        try:
            projections = persistence.load_projections(cur)
        except FileNotFoundError:
            projections = _ensure_projections(cur)
        try:
            confirmed, _ = players_provider.confirmed_from_fifa_feed()
        except Exception:
            confirmed = {}
        spec = config.get_round(cur)
        alerts = opportunities.detect(team, projections, players,
                                      confirmed_by_nation=confirmed or None,
                                      momentum=persistence.ownership_trend(),
                                      budget=spec.budget, nation_limit=spec.nation_limit)
        if alerts:
            summary.append("Opportunities:\n" + opportunities.format_alerts(alerts))
            if any(a.severity in ("high", "medium") for a in alerts):
                actionable = True

        # Confirmed-lineup windows: owned players kicking off within ~2h. Their
        # confirmed XIs are out and (rolling lockout) you can still change XI/captain.
        owned_nations = {p["nation"] for p in team["starters"] + team["bench"]}
        kicks = schedule_provider.nation_kickoffs(cur, sorted(owned_nations))
        soon = []
        for nat in owned_nations:
            k = kicks.get(nat)
            dt = _parse_dt(k["kickoff"]) if k else None
            if dt and 0 < (dt - now).total_seconds() <= 2 * 3600:
                soon.append((nat, dt))
        if soon:
            names = ", ".join(f"{n} (in {_fmt_delta(d, now)})"
                              for n, d in sorted(soon, key=lambda x: x[1]))
            summary.append(f"📋 Confirmed-line-up window — your players kick off soon: "
                           f"{names}. XIs are out; re-check XI/captain before lock.")
            actionable = True

    # 3) Data-integrity: odds<->player match audit (alert if linking degrades).
    if cur:
        snap = config.ODDS_DIR / f"odds_{cur}_latest.json"
        if snap.exists():
            try:
                import json as _json
                from . import projections as _P
                a = _P.audit_market(_active_players(), _json.loads(snap.read_text()))
                problems = []
                if a["rate"] < 0.93:
                    problems.append(f"match rate {a['rate']*100:.0f}% (below 93%)")
                if a["popular_unlinked"]:
                    nms = ", ".join(n for n, *_ in a["popular_unlinked"][:5])
                    problems.append(
                        f"{len(a['popular_unlinked'])} popular player(s) with no "
                        f"linked goal price: {nms}")
                if problems:
                    summary.append(
                        "⚠️ Odds↔player matching needs a look: " + "; ".join(problems)
                        + f". Run: ./wcf-run audit-odds --round {cur}")
                    actionable = True
            except Exception as e:
                print(f"[run-due] odds audit skipped: {e}")

    text = "\n\n".join(summary) if summary else "Nothing to do right now."
    print("\n" + text + "\n")
    if args.notify and actionable:
        import hashlib
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        statef = config.DATA_DIR / "last_notify.txt"
        prev = statef.read_text().strip() if statef.exists() else ""
        if h == prev:
            print("[notify] unchanged since last alert — skipping to avoid spam.")
        else:
            sent = notify.notify("⚽ WC Fantasy", text, channels=args.channels)
            config.ensure_dirs()
            statef.write_text(h)
            print(f"[notify] sent via: {', '.join(sent) or 'none (configure Telegram / run on macOS)'}")
    return 0


def cmd_notify_test(args):
    """Send a test notification to verify Telegram / macOS delivery."""
    from . import notify
    print(f"Telegram configured: {notify.telegram_configured()}")
    sent = notify.notify(
        "WC Fantasy — test",
        "✅ Notifications are working. I'll ping you only when it's worth a look "
        "(squad locks soon, confirmed line-ups, or a worthwhile transfer).",
        channels=args.channels)
    if sent:
        print(f"[notify] delivered via: {', '.join(sent)}")
    else:
        print("[notify] nothing delivered. To enable phone alerts, set "
              "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see wcf/notify.py "
              "for the BotFather steps), or run on macOS for local banners.")
    return 0


def cmd_audit_odds(args):
    """Report odds<->player matching quality and (optionally) lock confident
    matches into the verified alias table."""
    import json as _json
    from . import projections as _P
    from .providers import aliases
    players = _active_players()
    snap = config.ODDS_DIR / f"odds_{args.round}_latest.json"
    if not snap.exists():
        print(f"No odds snapshot for {args.round}. Run: fetch-odds --round {args.round}")
        return 1
    matches = _json.loads(snap.read_text())
    if args.seed:
        n = aliases.seed_from_matches(players, matches)
        print(f"[aliases] locked {n} confident matches into {aliases.ALIASES_FILE.name} "
              f"(now authoritative).")
    a = _P.audit_market(players, matches)
    by_id = {p.id: p for p in players}
    print(f"\n  ODDS↔PLAYER MATCH AUDIT — {args.round}")
    print(f"  match rate : {a['rate']*100:.1f}%  ({a['matched']}/{a['total']} priced names linked)")
    print(f"  popular players (own≥4% or ≥$7.5m) with NO linked goal price: "
          f"{len(a['popular_unlinked'])}")
    for nm, nat, pos, price, own in a["popular_unlinked"][:20]:
        print(f"     ! {nm} ({nat} {pos} ${price}m, {own}%)")
    print(f"  bookmaker names not in our pool (expected fringe): {len(a['unmatched_names'])}")
    print(f"  low-confidence (surname-only) matches to review: {len(a['low_confidence'])}")
    for bn, pid, sc in a["low_confidence"][:20]:
        nm = by_id[pid].name if pid in by_id else pid
        print(f"     ? {bn!r} -> {nm} (score {sc})")
    if args.seed:
        print("\n  Tip: re-run without --seed to review what's left after locking.")
    return 0


def cmd_fetch_odds(args):
    matches = odds_api.fetch_round_odds(args.round)
    print(f"Fetched {len(matches)} matches for {args.round}.")
    return 0


def cmd_fetch_lineups(args):
    # Refresh the pool from FIFA first — keeps availability + ownership current
    # (improves the prior as the deadline nears; catches new injuries).
    fresh = players_provider.fetch_public_players()
    players_provider.save_players(fresh)
    persistence.record_ownership(fresh)
    players = [p for p in fresh if p.available]
    fifa_names = sorted({p.nation for p in players})
    prior = lineups_provider.from_pool(
        players, momentum=persistence.ownership_trend(),
        trend_available=_trend_available())

    status_counts = {}
    if args.source == "apifootball":
        key = config.get_env("API_FOOTBALL_KEY")
        if not key:
            print("No API_FOOTBALL_KEY in .env.")
            return 1
        round_fixtures = schedule_provider.fixtures(args.round, fifa_names)
        try:
            confirmed = apifootball.confirmed_starters_for_round(
                round_fixtures, fifa_names, key)
        except apifootball.APIFootballError as e:
            print(f"[lineups] API-Football error: {e}")
            print("  (note: the free plan does not cover season 2026.)")
            return 1
    else:  # default: FIFA's own live feed
        confirmed, status_counts = players_provider.confirmed_from_fifa_feed()

    if not confirmed:
        lineups_provider.save_lineups(args.round, prior)
        print("[lineups] no confirmed XIs published yet — saved the refreshed "
              "availability/ownership prior.")
        if status_counts:
            print(f"  FIFA matchStatus values seen: {status_counts}")
        print("  Re-run ~1h before each of your players' kickoffs (see `schedule`),"
              " or enter a manual lineups csv.")
        return 0

    merged = lineups_provider.merge_confirmed(prior, confirmed, players)
    path = lineups_provider.save_lineups(args.round, merged)
    print(f"[lineups] confirmed XIs for {len(confirmed)} teams -> {path.name}. "
          "Re-run `project`/`optimize` to use them.")
    return 0


def cmd_schedule(args):
    players = _active_players()
    fifa_names = sorted({p.nation for p in players})
    deadline = schedule_provider.round_deadline(args.round)
    kickoffs = schedule_provider.nation_kickoffs(args.round, fifa_names)
    print(f"\n  {args.round} — squad/transfer deadline (first kickoff): "
          f"{_fmt_dt(deadline)}")
    print("  (XI & captain lock per player at each match below — rolling)")
    print("  " + "-" * 56)

    try:
        team = persistence.load_team(args.round)
        owned = team["starters"] + team["bench"]
        print("  YOUR PLAYERS' LOCK TIMES:")
        rows = []
        for p in owned:
            k = kickoffs.get(p["nation"])
            if k:
                rows.append((k["kickoff"], p, k))
        for kickoff, p, k in sorted(rows, key=lambda r: r[0]):
            tag = " (C)" if p.get("is_captain") else (" (V)" if p.get("is_vice") else "")
            print(f"   {_fmt_dt(kickoff):<17} {p['position']:<3} {p['name']:<20}"
                  f"{tag:<4} {p['nation']} v {k['opponent']}")
    except FileNotFoundError:
        print("  (no saved team yet — showing all fixtures)")
        for m in schedule_provider.fixtures(args.round, fifa_names):
            print(f"   {_fmt_dt(m['kickoff']):<17} {m['home']} v {m['away']}")
    print()
    return 0


def _fmt_dt(iso: str) -> str:
    if not iso:
        return "?"
    return iso.replace("T", " ")[:16]


def cmd_project(args):
    projections = _run_projection(args.round, refetch=args.refetch)
    _print_projection_table(projections, top=args.top)
    print(f"Saved projections for {len(projections)} players.")
    return 0


def _resolve_horizon(round_key: str, arg: int) -> int:
    """Auto horizon: remaining group games for group rounds, else 1."""
    if arg and arg > 0:
        return arg
    group_index = {"MD1": 0, "MD2": 1, "MD3": 2}
    if round_key.upper() in group_index:
        return 3 - group_index[round_key.upper()]
    return 1


def _cap_positions(args):
    return tuple(config.POSITIONS) if getattr(args, "captain_any", False) else ("MID", "FWD")


def _parse_exclude(s):
    return [t.strip().lower() for t in (s or "").split(",") if t.strip()]


def _is_excluded(pid, name, tokens):
    if not tokens:
        return False
    nm = (name or "").lower()
    return any(t == str(pid).lower() or t in nm for t in tokens)


def cmd_optimize(args):
    spec = config.get_round(args.round)
    horizon = _resolve_horizon(args.round, args.horizon)

    # Multi-round build (fresh squad, group stage). Transfers stay round-by-round.
    if horizon > 1 and not args.from_existing:
        return _optimize_horizon_cmd(args, spec, horizon)

    projections = _ensure_projections(args.round)
    ex = _parse_exclude(getattr(args, "exclude", None)) + persistence.load_exclusions()
    if ex:
        before = len(projections)
        projections = [p for p in projections if not _is_excluded(p.player_id, p.name, ex)]
        print(f"[optimize] excluded {before - len(projections)} player(s) matching: {args.exclude}")
    existing = None
    free_transfers = config.UNLIMITED
    if args.from_existing:
        existing = persistence.current_squad_ids(args.from_existing)
        if existing is None:
            print(f"No saved team for {args.from_existing} to transfer from.")
            return 1
        free_transfers = (config.UNLIMITED if args.chip == "wildcard"
                          else spec.free_transfers)

    sel = optimize_squad(
        projections, budget=spec.budget, nation_limit=spec.nation_limit,
        existing_squad=existing, free_transfers=free_transfers,
        captain_positions=_cap_positions(args))
    sel.round = args.round
    sel.chip = args.chip

    # Risk-adjusted captaincy from the points distribution.
    comp_by_id = {p.player_id: p.components for p in projections}
    pos_by_id = {p.player_id: p.position for p in projections}
    by_id = {p.player_id: p for p in projections}
    cap, vice, scored = _recaptain(sel.starters, comp_by_id, pos_by_id,
                                   set(_cap_positions(args)), args.captain_risk)
    sel.captain, sel.vice = cap, vice

    _print_selection(sel, projections)
    _print_captaincy(scored, lambda i: by_id[i].name, args.captain_risk)
    if not args.no_save:
        path = persistence.save_team(args.round, sel, projections)
        print(f"Team saved -> {path}")
    return 0


def _recaptain(starters, comp_by_id, pos_by_id, cap_pos, risk_level):
    """Re-pick captain/vice within the chosen XI on a risk-adjusted basis."""
    cands = [i for i in starters if pos_by_id.get(i) in cap_pos] or list(starters)
    scored = []
    for i in cands:
        st = risk.point_distribution(pos_by_id[i], comp_by_id.get(i, {}))
        scored.append((i, st, risk.captain_score(st, risk_level)))
    scored.sort(key=lambda t: t[2], reverse=True)
    captain = scored[0][0]
    vice = scored[1][0] if len(scored) > 1 else scored[0][0]
    return captain, vice, scored


def _print_captaincy(scored, name_of, risk_level, top=4):
    print(f"  CAPTAINCY  (risk: {risk_level}) — figures are DOUBLED captain returns")
    print("  " + "-" * 58)
    print(f"  {'PLAYER':<22}{'mean':>7}{'std':>7}{'ceiling':>9}{'P(haul)':>9}")
    for i, st, _ in scored[:top]:
        print(f"  {name_of(i):<22}{st['mean'] * 2:>7.1f}{st['std'] * 2:>7.1f}"
              f"{st['ceiling'] * 2:>9.1f}{st['p_haul']:>8.0%}")
    print()


def _optimize_horizon_cmd(args, spec, horizon):
    players = _active_players()
    ex = _parse_exclude(getattr(args, "exclude", None)) + persistence.load_exclusions()
    if ex:
        before = len(players)
        players = [p for p in players if not _is_excluded(p.id, p.name, ex)]
        print(f"[optimize] excluded {before - len(players)} player(s) matching: {args.exclude}")
    matches = _ensure_odds(args.round)
    lns = _resolve_lineups(args.round, players)
    roles = setpieces.roles_for_players(players)

    md1 = build_projections(players, matches, lns, set_piece_roles=roles)
    persistence.save_projections(args.round, md1)
    hz = multiround.build_horizon_projections(players, matches, lns, horizon=horizon,
                                              set_piece_roles=roles)

    squad, plan = optimize_horizon(
        hz.meta, hz.ep, hz.round_labels,
        budget=spec.budget, nation_limit=spec.nation_limit,
        captain_positions=_cap_positions(args),
        planned_transfers=args.planned_transfers)

    # Risk-adjusted captaincy per round (from each round's points distribution).
    cap_pos = set(_cap_positions(args))
    md1_scored = None
    for r, pr in enumerate(plan):
        comp_r = {pid: hz.components[pid][r] for pid in pr["starters"]}
        pos_r = {pid: hz.meta[pid]["position"] for pid in pr["starters"]}
        cap, vice, scored = _recaptain(pr["starters"], comp_r, pos_r,
                                       cap_pos, args.captain_risk)
        pr["captain"], pr["vice"] = cap, vice
        if r == 0:
            md1_scored = scored

    by_id = {p.player_id: p for p in md1}
    p0 = plan[0]
    sel = Selection(
        round=args.round, squad=squad, starters=p0["starters"], bench=p0["bench"],
        captain=p0["captain"], vice=p0["vice"], formation=p0["formation"],
        chip=args.chip, expected_points=p0["ep"],
        cost=round(sum(by_id[i].price for i in squad), 1))

    print(f"\n(Horizon build: squad optimised across "
          f"{', '.join(hz.round_labels)}; ≤{args.planned_transfers} planned "
          f"transfer/round, rest reserved)")
    _print_selection(sel, md1)
    _print_captaincy(md1_scored, lambda i: hz.meta[i]["name"], args.captain_risk)
    _print_plan(plan, hz)
    if not args.no_save:
        persistence.save_team(args.round, sel, md1)
        _save_plan(args.round, plan, hz)
        print("Team + group-stage plan saved.")
    return 0


def _print_plan(plan, hz):
    print("  GROUP-STAGE PLAN  (core squad held; planned upgrades shown)")
    print("  " + "-" * 56)
    for pr in plan:
        cap = hz.meta[pr["captain"]]["name"]
        print(f"   {pr['label']:<4} {pr['formation']:<7} "
              f"XI xP {pr['ep']:>5.1f}   captain: {cap}")
        if pr["transfers_out"]:
            outs = ", ".join(hz.meta[i]["name"] for i in pr["transfers_out"])
            ins = ", ".join(hz.meta[i]["name"] for i in pr["transfers_in"])
            print(f"        planned: OUT {outs}  ->  IN {ins}")
    print("  (≈1 planned upgrade/round; the rest of your free transfers stay free\n"
          "   for injuries/rotation. Re-optimise each round on fresh odds/line-ups.)\n")


def _save_plan(round_key, plan, hz):
    import json
    payload = {"built_for": round_key, "rounds": []}
    for pr in plan:
        payload["rounds"].append({
            "round": pr["label"], "formation": pr["formation"], "xi_xp": pr["ep"],
            "captain": hz.meta[pr["captain"]]["name"],
            "transfers_in": [hz.meta[i]["name"] for i in pr["transfers_in"]],
            "transfers_out": [hz.meta[i]["name"] for i in pr["transfers_out"]],
            "starters": [hz.meta[i]["name"] for i in pr["starters"]],
        })
    (config.TEAMS_DIR / f"plan_{round_key}.json").write_text(json.dumps(payload, indent=2))


def cmd_show_team(args):
    team = persistence.load_team(args.round)
    print(_team_to_text(team))
    return 0


def cmd_record(args):
    if args.source == "fifa":
        import csv
        rid = schedule_provider.round_id_for(args.round)
        if rid is None:
            print("Couldn't resolve the FIFA round id.")
            return 1
        pts, fallback = players_provider.fetch_round_points(rid)
        if not pts:
            print("[record] No round points from FIFA yet (round not scored). "
                  "Try again after the matches, or use a manual results CSV.")
            return 1
        config.ensure_dirs()
        path = config.RESULTS_DIR / f"results_{args.round}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["player_id", "minutes", "points"])
            for pid, p in pts.items():
                # points != 0 implies the player featured (appearance points);
                # used only to drive auto-subs.
                w.writerow([pid, 90 if p != 0 else 0, p])
        print(f"[record] built results from FIFA feed: {len(pts)} players"
              + (" (lastRoundPoints fallback)" if fallback else "") + ".")

    score = persistence.score_round(args.round)
    team = persistence.load_team(args.round)
    persistence.update_history(score, chip=team.get("chip"), cost=team.get("cost", 0.0))
    print(f"\n  {args.round} result")
    print("  " + "-" * 40)
    print(f"  base points     : {score['base_points']}")
    print(f"  captain ({score['captain_used']}) extra: {score['captain_extra']}")
    if score["auto_subs"]:
        print(f"  auto-subs       : {', '.join(score['auto_subs'])}")
    if score["hit_points"]:
        print(f"  transfer hit    : -{score['hit_points']}")
    print(f"  TOTAL           : {score['total_points']}  "
          f"(projected {score['expected_points']})")
    print()
    return 0


def cmd_report(args):
    rows = persistence.load_history()
    if not rows:
        print("No history yet. Use `record` after a round completes.")
        return 0
    print("\n  Season performance")
    print("  " + "-" * 58)
    print(f"  {'RND':<5}{'xP':>8}{'ACTUAL':>9}{'CAPT':>7}{'HIT':>6}{'CHIP':>10}{'CUM':>8}")
    cum = 0.0
    for r in rows:
        actual = float(r.get("total_points") or 0)
        cum += actual
        print(f"  {r['round']:<5}{float(r.get('expected_points') or 0):>8.1f}"
              f"{actual:>9.1f}{float(r.get('captain_extra') or 0):>7.1f}"
              f"{float(r.get('hit_points') or 0):>6.0f}{(r.get('chip') or ''):>10}"
              f"{cum:>8.1f}")
    print("  " + "-" * 58)
    print(f"  Total points: {cum:.1f} over {len(rows)} round(s)\n")
    return 0


def _team_to_text(team: dict) -> str:
    out = [f"\n  {team['round']}  formation {team['formation']}  "
           f"cost ${team['cost']}m  xP {team['expected_points']}"]
    out.append("  STARTING XI")
    for p in team["starters"]:
        mark = " (C)" if p["is_captain"] else (" (V)" if p["is_vice"] else "")
        out.append(f"   {p['position']:<3} {p['name']:<22}{mark:<4} "
                   f"{p['nation']:<14} ${p['price']:.1f}m  {p['exp_points']:.2f}")
    out.append("  BENCH")
    for p in team["bench"]:
        out.append(f"   {p['position']:<3} {p['name']:<22}     "
                   f"{p['nation']:<14} ${p['price']:.1f}m  {p['exp_points']:.2f}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def cmd_exclude(args):
    current = persistence.load_exclusions()
    if args.add:
        current = current + _parse_exclude(args.add)
        persistence.set_exclusions(current)
        print(f"Added. Now excluding: {persistence.load_exclusions()}")
    elif args.remove:
        rm = set(_parse_exclude(args.remove))
        persistence.set_exclusions([t for t in current if t not in rm])
        print(f"Removed. Now excluding: {persistence.load_exclusions()}")
    else:
        print(f"Currently excluding: {persistence.load_exclusions() or '(none)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wcf",
                                description="World Cup 2026 Fantasy optimizer & tracker")
    sub = p.add_subparsers(dest="command", required=True)

    fp = sub.add_parser("fetch-players",
                        help="load the FIFA player pool (default: public FIFA feed)")
    fp.add_argument("--har", help="auto-extract the pool from a browser .har export")
    fp.add_argument("--json", help="parse a saved FIFA players JSON file")
    fp.add_argument("--csv", help="import a players CSV")
    fp.add_argument("--url", action="store_true", help="fetch via FIFA_PLAYERS_URL in .env")
    fp.add_argument("--price-divisor", type=float, default=1.0,
                    help="divide raw JSON prices (e.g. 10 if 105 means 10.5)")
    fp.set_defaults(func=cmd_fetch_players)

    fo = sub.add_parser("fetch-odds", help="pull + snapshot betting odds for a round")
    fo.add_argument("--round", required=True)
    fo.set_defaults(func=cmd_fetch_odds)

    fl = sub.add_parser("fetch-lineups",
                        help="pull confirmed XIs into the round's line-ups")
    fl.add_argument("--round", required=True)
    fl.add_argument("--source", choices=["fifa", "apifootball"], default="fifa",
                    help="confirmed-lineup source (default: FIFA live feed)")
    fl.set_defaults(func=cmd_fetch_lineups)

    sc = sub.add_parser("schedule", help="show kickoff/lock times for a round")
    sc.add_argument("--round", required=True)
    sc.set_defaults(func=cmd_schedule)

    ag = sub.add_parser("agenda", help="what to do now / next (deadlines + your matches)")
    ag.add_argument("--notify", action="store_true",
                    help="also fire a macOS notification if action is needed soon")
    ag.set_defaults(func=cmd_agenda)

    rd = sub.add_parser("run-due",
                        help="auto-pilot: optimise when due, scan opportunities, notify")
    rd.add_argument("--window", type=float, default=18.0,
                    help="hours before a deadline to auto-optimise (default 18)")
    rd.add_argument("--odds-max-age", type=float, default=18.0,
                    help="refetch odds if the snapshot is older than this many hours")
    rd.add_argument("--notify", action="store_true", help="send Telegram + macOS alerts")
    rd.add_argument("--channels", choices=["auto", "telegram", "macos"], default="auto")
    rd.set_defaults(func=cmd_run_due)

    pr = sub.add_parser("project", help="compute expected points for a round")
    pr.add_argument("--round", required=True)
    pr.add_argument("--refetch", action="store_true", help="ignore cached odds snapshot")
    pr.add_argument("--top", type=int, default=25)
    pr.set_defaults(func=cmd_project)

    op = sub.add_parser("optimize", help="pick the optimal squad for a round")
    op.add_argument("--round", required=True)
    op.add_argument("--horizon", type=int, default=0,
                    help="rounds to plan across (0=auto: remaining group games)")
    op.add_argument("--planned-transfers", type=int, default=1,
                    help="planned upgrade transfers per round in group planning "
                         "(rest reserved for injuries); 0 = hold squad")
    op.add_argument("--from-existing", metavar="ROUND",
                    help="plan transfers from a previously saved team")
    op.add_argument("--chip", choices=["wildcard", "12thman", "maxcaptain",
                                       "qualification", "mystery"], default=None)
    op.add_argument("--captain-any", action="store_true",
                    help="allow GK/DEF as captain (default: only MID/FWD)")
    op.add_argument("--captain-risk", choices=["safe", "balanced", "upside"],
                    default="balanced",
                    help="captaincy risk appetite (weights the points ceiling)")
    op.add_argument("--exclude", default=None,
                    help="comma-separated player names/ids to leave out (e.g. injuries)")
    op.add_argument("--no-save", action="store_true")
    op.set_defaults(func=cmd_optimize)

    st = sub.add_parser("show-team", help="print a saved team")
    st.add_argument("--round", required=True)
    st.set_defaults(func=cmd_show_team)

    rc = sub.add_parser("record", help="score actual results and update history")
    rc.add_argument("--round", required=True)
    rc.add_argument("--source", choices=["manual", "fifa"], default="manual",
                    help="manual results CSV, or auto-pull points from the FIFA feed")
    rc.set_defaults(func=cmd_record)

    rp = sub.add_parser("report", help="show performance across rounds")
    rp.set_defaults(func=cmd_report)

    ex = sub.add_parser("exclude", help="manage players to always leave out (injuries)")
    ex.add_argument("--add", help="comma-separated names/ids to exclude")
    ex.add_argument("--remove", help="comma-separated names/ids to stop excluding")
    ex.set_defaults(func=cmd_exclude)

    au = sub.add_parser("audit-odds",
                        help="check odds<->player matching quality; --seed locks "
                             "confident matches as verified aliases")
    au.add_argument("--round", required=True)
    au.add_argument("--seed", action="store_true",
                    help="lock current confident matches into the alias table")
    au.set_defaults(func=cmd_audit_odds)

    nt = sub.add_parser("notify-test",
                        help="send a test notification (verify Telegram/macOS)")
    nt.add_argument("--channels", choices=["auto", "telegram", "macos"],
                    default="auto")
    nt.set_defaults(func=cmd_notify_test)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    config.load_env()
    config.ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
