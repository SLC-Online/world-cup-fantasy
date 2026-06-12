"""World Cup Fantasy — local dashboard.

A pull-model GUI: open it whenever you like and it recomputes live, so you never
have to trust a silent background job. Shows what to do next, your team, the
group-stage plan, projections, your players' lock times, and a health panel
(data freshness, API keys, notifier status). Action buttons run the same
pipeline the CLI uses.

Launch:  ./.venv/bin/streamlit run dashboard.py   (or the .app — see deploy/)
"""
from __future__ import annotations

import sys
# Use ONLY this project's venv packages. A stray numpy/pandas in user
# site-packages (~/Library/Python/...) can otherwise shadow the venv copies and
# crash the import with a confusing "numpy source directory" error. Strip any
# user-site entries from the path before importing pandas/numpy.
sys.path[:] = [p for p in sys.path if "Library/Python" not in p]

import subprocess
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from wcf import config, persistence
from wcf import flags
from wcf.providers import schedule as schedule_provider

config.load_env()
ROOT = config.PROJECT_ROOT
ROUND_KEYS = [r.key for r in config.ROUNDS]

st.set_page_config(page_title="World Cup Fantasy", page_icon="⚽", layout="wide")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run_cli(args, timeout=240):
    """Run a wcf CLI command and return (ok, output)."""
    try:
        p = subprocess.run([sys.executable, "-m", "wcf.cli", *args],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "Timed out."


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def humanize(seconds):
    s = int(abs(seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    out = (f"{d}d " if d else "") + (f"{h}h " if (h or d) else "") + f"{m}m"
    return out.strip()


def age_of(path: Path):
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def freshness(path: Path):
    a = age_of(path)
    return "—" if a is None else f"{humanize(a)} ago"


@st.cache_data(ttl=900)
def get_rounds():
    return sorted(schedule_provider.fetch_rounds(), key=lambda r: r.get("startDate", ""))


def latest_saved_team():
    for rk in reversed(ROUND_KEYS):
        try:
            return rk, persistence.load_team(rk)
        except FileNotFoundError:
            continue
    return None, None


def next_deadline(rounds, now):
    for i, r in enumerate(rounds):
        dl = parse_dt(r.get("startDate"))
        if dl and dl > now and i < len(ROUND_KEYS):
            return ROUND_KEYS[i], dl
    return None, None


def _grad(frac: float) -> str:
    """CSS for a red→yellow→green cell (frac 0=worst/red, 1=best/green)."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        t = frac / 0.5
        r, g, b = 214 + (247 - 214) * t, 72 + (220 - 72) * t, 72 + (100 - 72) * t
    else:
        t = (frac - 0.5) / 0.5
        r, g, b = 247 + (60 - 247) * t, 220 + (174 - 220) * t, 100 + (96 - 100) * t
    return f"background-color: rgb({int(r)},{int(g)},{int(b)}); color:#111"


@st.cache_data(ttl=600)
def horizon_tables():
    """(round_labels, player_rows, fixture_rows) for the ticker + planner tabs.
    fixture cell = (opponent, expected_goals_for, clean_sheet_prob)."""
    import json
    import math
    from wcf import multiround
    from wcf.projections import compute_match_goals
    from wcf.providers import players as pp, lineups as lp, setpieces
    snap = config.ODDS_DIR / "odds_MD1_latest.json"
    if not snap.exists():
        return None
    matches = json.loads(snap.read_text())
    players = [p for p in pp.load_players() if p.available]
    own = {p.id: p.ownership for p in players}
    span = persistence.ownership_history_span_hours()
    lns = lp.from_pool(players, momentum=persistence.ownership_trend(),
                       trend_available=span >= 12)
    lns = lp.apply_appearances(lns, persistence.appearance_signal(players))
    roles = setpieces.roles_for_players(players)
    hz = multiround.build_horizon_projections(players, matches, lns, horizon=3,
                                              set_piece_roles=roles)
    mg = compute_match_goals(matches)
    fx = multiround._nation_ordered_fixtures(matches, mg)
    labels = hz.round_labels

    prows = []
    for pid, eps in hz.ep.items():
        m = hz.meta[pid]
        row = {"Player": f"{flags.flag(m['nation'])} {m['name']}", "Pos": m["position"],
               "$m": m["price"], "Own%": round(own.get(pid, 0.0), 1)}
        for idx, lab in enumerate(labels):
            row[lab] = round(eps[idx], 2) if idx < len(eps) else 0.0
        row["Total"] = round(sum(eps), 2)
        prows.append(row)

    grows = []
    for nation, ms in fx.items():
        row = {"Team": f"{flags.flag(nation)} {nation}"}
        for idx, lab in enumerate(labels):
            if idx < len(ms):
                g = mg[ms[idx]["match_id"]]
                row[lab] = (g.opponent_of(nation), round(g.mu_for(nation), 2),
                            round(math.exp(-g.mu_against(nation)), 2))
            else:
                row[lab] = None
        grows.append(row)
    return labels, prows, grows


# --------------------------------------------------------------------------- #
# Sidebar — controls
# --------------------------------------------------------------------------- #
now = datetime.now().astimezone()
try:
    rounds = get_rounds()
except Exception as e:  # network/feed issue
    rounds = []
    st.sidebar.error(f"Couldn't load the FIFA schedule: {e}")

nd_round, nd_dt = next_deadline(rounds, now) if rounds else (None, None)

st.sidebar.header("Controls")
default_round = nd_round or "MD1"
sel_round = st.sidebar.selectbox("Round", ROUND_KEYS,
                                 index=ROUND_KEYS.index(default_round))
st.sidebar.caption("Actions run the same pipeline as the CLI.")

if st.sidebar.button("↻ Refresh view"):
    st.cache_data.clear()
    st.rerun()

# Between matchdays you keep your squad and make only the allowed transfers, so a
# re-optimise must transfer FROM the previous round's team — not build from scratch.
_rkeys = [r.key for r in config.ROUNDS]
_idx = _rkeys.index(sel_round) if sel_round in _rkeys else 0
_prev = _rkeys[_idx - 1] if _idx > 0 else None
_carry = _prev if (_prev and persistence.team_path(_prev).exists()) else None
_opt_args = ["optimize", "--round", sel_round] + (
    ["--from-existing", _carry] if _carry else [])

if _carry:
    st.sidebar.caption(f"⚙ {sel_round} transfers from your {_carry} squad — it keeps "
                       "the team and changes only what the transfer rules allow.")
_opt_label = (f"Re-optimise {sel_round} (transfer from {_carry})" if _carry
              else f"Build {sel_round} squad (cached odds)")
if st.sidebar.button(_opt_label):
    with st.spinner("Optimising…"):
        ok, out = run_cli(_opt_args)
    st.session_state["last_output"] = out
    st.rerun()

if st.sidebar.button(f"Fetch line-ups {sel_round} (FIFA feed)"):
    with st.spinner("Fetching confirmed line-ups…"):
        ok, out = run_cli(["fetch-lineups", "--round", sel_round])
    st.session_state["last_output"] = out
    st.rerun()

with st.sidebar.expander("⚠ Fetch FRESH odds + re-optimise (uses API credits)"):
    st.caption("Only needed near a deadline; costs ~50 Odds-API credits.")
    if st.button(f"Confirm: fresh odds for {sel_round}"):
        with st.spinner("Fetching odds + optimising…"):
            ok1, o1 = run_cli(["fetch-odds", "--round", sel_round])
            ok2, o2 = run_cli(_opt_args)
        st.session_state["last_output"] = o1 + "\n" + o2
        st.rerun()

if "last_output" in st.session_state:
    with st.sidebar.expander("Last command output", expanded=False):
        st.code(st.session_state["last_output"][-4000:])


# --------------------------------------------------------------------------- #
# Header — status + action banner
# --------------------------------------------------------------------------- #
st.title("⚽ World Cup Fantasy")
c1, c2, c3 = st.columns(3)
c1.metric("Now", now.strftime("%a %d %b %H:%M"))
if nd_dt:
    delta = (nd_dt - now).total_seconds()
    c2.metric(f"Next deadline ({nd_round})", nd_dt.strftime("%a %d %b %H:%M"),
              f"in {humanize(delta)}")
    saved = False
    try:
        persistence.load_team(nd_round); saved = True
    except FileNotFoundError:
        pass
    c3.metric("Team saved for it?", "Yes ✓" if saved else "No ⚠")
    if delta < 36 * 3600:
        (st.success if saved else st.error)(
            f"ACTION: {nd_round} squad locks in {humanize(delta)}. "
            + ("Review your saved team and enter it." if saved
               else f"No team saved — use the sidebar to optimise {nd_round}."))
else:
    c2.metric("Next deadline", "—")

tabs = st.tabs(["Team", "Group plan", "Projections", "Schedule", "Health",
                "Fixtures", "Players"])


# --------------------------------------------------------------------------- #
# Team tab
# --------------------------------------------------------------------------- #
with tabs[0]:
    try:
        team = persistence.load_team(sel_round)
    except FileNotFoundError:
        team = None
    if not team:
        st.info(f"No saved team for {sel_round}. Optimise it from the sidebar.")
    else:
        st.subheader(f"{sel_round} — {team['formation']} · "
                     f"${team['cost']}m · {team['expected_points']} projected pts")
        cap, vice = team["captain"], team["vice"]

        # Make transfers explicit: only SQUAD changes cost a transfer; the XI is
        # free to reshuffle each matchday, so a different-looking XI is NOT transfers.
        _rkk = [r.key for r in config.ROUNDS]
        _pvr = (_rkk[_rkk.index(sel_round) - 1]
                if sel_round in _rkk and _rkk.index(sel_round) > 0 else None)
        if _pvr and persistence.team_path(_pvr).exists():
            try:
                _pt = persistence.load_team(_pvr)
                _pids = {p["id"]: p["name"] for p in _pt["starters"] + _pt["bench"]}
                _cids = {p["id"]: p["name"] for p in team["starters"] + team["bench"]}
                _out = [_pids[i] for i in _pids if i not in _cids]
                _in = [_cids[i] for i in _cids if i not in _pids]
                _ft = config.get_round(sel_round).free_transfers
                _ftx = "unlimited" if _ft == config.UNLIMITED else str(_ft)
                if _out:
                    st.success(f"**{len(_out)} transfer(s) from {_pvr}** (allowed: {_ftx}) — "
                               f"OUT: {', '.join(_out)} → IN: {', '.join(_in)}.  "
                               f"The other {15 - len(_out)} players carry over; the XI just "
                               f"reshuffles for {sel_round}'s fixtures (free).")
                else:
                    st.success(f"**No transfers from {_pvr}** — same 15; XI reshuffled "
                               f"for {sel_round}'s fixtures (free).")
            except Exception:
                pass

        def card(p):
            fl = flags.flag(p["nation"])
            badge = (" <b style='color:#c0392b'>(C)</b>" if p["id"] == cap
                     else (" <b style='color:#2c3e50'>(V)</b>" if p["id"] == vice else ""))
            return (
                "<div style='background:#fff;border:1px solid #e3e3e3;border-radius:10px;"
                "padding:8px 4px;margin:4px;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.12)'>"
                f"<div style='font-size:24px;line-height:1.1'>{fl}</div>"
                f"<div style='font-weight:600;font-size:13px;color:#111'>{p['name']}{badge}</div>"
                f"<div style='font-size:11px;color:#666'>${p['price']:.1f}m · {p['exp_points']:.1f} xP</div>"
                "</div>")

        starters = team["starters"]
        st.markdown(
            "<div style='background:linear-gradient(180deg,#1f7a37,#2e9e4a);border-radius:12px;"
            "padding:6px;margin-bottom:4px;text-align:center;color:#eafff0;font-size:12px;"
            "letter-spacing:1px'>STARTING XI</div>", unsafe_allow_html=True)
        for posrow in ("GK", "DEF", "MID", "FWD"):
            row = [p for p in starters if p["position"] == posrow]
            if not row:
                continue
            cols = st.columns(len(row))
            for col, p in zip(cols, row):
                col.markdown(card(p), unsafe_allow_html=True)

        st.markdown("**Bench** (auto-sub order)")
        bench = team["bench"]
        for col, p in zip(st.columns(max(len(bench), 1)), bench):
            col.markdown(card(p), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Group plan tab
# --------------------------------------------------------------------------- #
with tabs[1]:
    plan_path = config.TEAMS_DIR / f"plan_{sel_round}.json"
    if not plan_path.exists():
        st.info("No group-stage plan saved for this round "
                "(plans are produced for group-stage builds).")
    else:
        import json
        plan = json.loads(plan_path.read_text())
        for r in plan["rounds"]:
            line = (f"**{r['round']}** · {r['formation']} · XI {r['xi_xp']} pts · "
                    f"captain {r['captain']}")
            st.markdown(line)
            if r.get("transfers_out"):
                st.caption(f"planned: OUT {', '.join(r['transfers_out'])} → "
                           f"IN {', '.join(r['transfers_in'])}")
        st.caption("Future rounds assume ≤1 planned transfer; the rest of your "
                   "free transfers stay free for injuries. Re-optimise each round.")


# --------------------------------------------------------------------------- #
# Projections tab
# --------------------------------------------------------------------------- #
with tabs[2]:
    proj_path = config.PROJECTIONS_DIR / f"projections_{sel_round}.csv"
    if not proj_path.exists():
        st.info("No projections yet. Optimise the round to generate them.")
    else:
        df = pd.read_csv(proj_path)
        if "nation" in df.columns:
            df["nation"] = df["nation"].map(lambda n: f"{flags.flag(str(n))} {n}")
        pos = st.multiselect("Position", ["GK", "DEF", "MID", "FWD"],
                             default=["GK", "DEF", "MID", "FWD"])
        df = df[df["position"].isin(pos)]
        cols = ["name", "nation", "position", "price", "opponent",
                "exp_points", "value"]
        cols = [c for c in cols if c in df.columns]
        st.dataframe(df[cols].sort_values("exp_points", ascending=False).head(60),
                     hide_index=True, width="stretch")


# --------------------------------------------------------------------------- #
# Schedule tab
# --------------------------------------------------------------------------- #
with tabs[3]:
    if not rounds:
        st.warning("Schedule unavailable.")
    else:
        _, team = latest_saved_team()
        owned = {p["nation"] for p in (team["starters"] + team["bench"])} if team else set()
        try:
            fx = schedule_provider.fixtures(sel_round, sorted(owned) or None)
        except Exception as e:
            fx = []
            st.warning(f"Couldn't load fixtures: {e}")
        rows = []
        for m in fx:
            k = parse_dt(m["kickoff"])
            mine = m["home"] in owned or m["away"] in owned
            rows.append({
                "Kickoff": k.strftime("%a %d %b %H:%M") if k else m["kickoff"],
                "Match": f"{flags.flag(m['home'])} {m['home']} v {m['away']} {flags.flag(m['away'])}",
                "Yours?": "★" if mine else "",
            })
        if rows:
            st.caption(f"Squad/transfer deadline (first kickoff): "
                       f"{schedule_provider.round_deadline(sel_round).replace('T', ' ')[:16]}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.caption("★ = a team you own players from. Each of your players locks "
                       "at their own kickoff (rolling) — apply confirmed line-ups then.")


# --------------------------------------------------------------------------- #
# Health tab — so you can SEE the system is working
# --------------------------------------------------------------------------- #
with tabs[4]:
    st.subheader("System health")
    odds_latest = config.ODDS_DIR / f"odds_{sel_round}_latest.json"
    checks = {
        "Player pool (players.csv)": freshness(config.PLAYERS_FILE),
        f"Odds snapshot ({sel_round})": freshness(odds_latest),
        f"Line-ups ({sel_round})": freshness(config.LINEUPS_DIR / f"lineups_{sel_round}.csv"),
        f"Projections ({sel_round})": freshness(config.PROJECTIONS_DIR / f"projections_{sel_round}.csv"),
        f"Saved team ({sel_round})": freshness(config.TEAMS_DIR / f"team_{sel_round}.json"),
    }
    st.table(pd.DataFrame({"Last updated": checks}))

    keys = {
        "Odds API key": "set ✓" if config.get_env("ODDS_API_KEY") else "missing ⚠",
        "API-Football key": "set ✓" if config.get_env("API_FOOTBALL_KEY") else "not set",
    }
    st.table(pd.DataFrame({"Status": keys}))

    # Notifier (launchd) status — addresses "what if the background job died"
    st.markdown("**Background notifier (launchd)**")
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                             timeout=10).stdout
        loaded = "com.wcf.agenda" in out
    except Exception:
        loaded = False
    log = ROOT / "data" / "agenda.log"
    if loaded:
        st.success(f"Loaded. Last ran: {freshness(log)}.")
    else:
        st.warning("Not installed/loaded. This dashboard works regardless — but to "
                   "get push reminders run  bash deploy/install_agenda.sh")
    st.caption("Because this dashboard recomputes live when you open it, you don't "
               "have to rely on the notifier — if anything is stale, you'll see it "
               "above.")


# --------------------------------------------------------------------------- #
# Fixtures tab — FDR-style ticker (coloured by how good each fixture is)
# --------------------------------------------------------------------------- #
with tabs[5]:
    st.subheader("Fixture ticker — group stage")
    st.caption("How good each team's upcoming fixtures are, up to the Round-of-32 "
               "reset. Green = favourable. Toggle attack (expected goals) vs "
               "defence (clean-sheet chance).")
    data = horizon_tables()
    if not data:
        st.info("No odds snapshot yet — fetch odds first.")
    else:
        labels, prows, grows = data
        attack = st.radio("Rate by", ["Attack — expected goals for",
                                       "Defence — clean-sheet chance"],
                          horizontal=True, key="fx_rate").startswith("Attack")
        sort_round = st.selectbox("Order teams by (best at top)", labels, key="fx_sort")
        st.caption("Use this selector to sort — clicking a column header sorts the "
                   "opponent names alphabetically, not by rating.")
        teams = [g["Team"] for g in grows]
        disp, num = {}, {}
        for lab in labels:
            dcol, ncol = [], []
            for g in grows:
                cell = g.get(lab)
                if cell:
                    opp, muf, cs = cell
                    val = muf if attack else cs
                    dcol.append(f"{opp} ({val:.2f})")
                    ncol.append(val)
                else:
                    dcol.append("—")
                    ncol.append(float("nan"))
            disp[lab] = dcol
            num[lab] = ncol
        ddf = pd.DataFrame(disp, index=teams)
        ndf = pd.DataFrame(num, index=teams)
        order = ndf[sort_round].sort_values(ascending=False, na_position="last").index
        ddf, ndf = ddf.loc[order], ndf.loc[order]
        vmin, vmax = ndf.min().min(), ndf.max().max()

        def _fx_style(_):
            css = pd.DataFrame("", index=ddf.index, columns=ddf.columns)
            for lab in labels:
                for t in ddf.index:
                    v = ndf.loc[t, lab]
                    if pd.notna(v) and vmax > vmin:
                        css.loc[t, lab] = _grad((v - vmin) / (vmax - vmin))
            return css

        st.dataframe(ddf.style.apply(_fx_style, axis=None), width="stretch", height=620)


# --------------------------------------------------------------------------- #
# Players tab — planner: expected points per round, sortable
# --------------------------------------------------------------------------- #
with tabs[6]:
    st.subheader("Player planner")
    st.caption("Expected points per upcoming round for every player. Sort by a round "
               "to find that gameweek's best picks, or by Total for the group stage. "
               "Includes price and ownership.")
    data = horizon_tables()
    if not data:
        st.info("No odds snapshot yet — fetch odds first.")
    else:
        labels, prows, grows = data
        df = pd.DataFrame(prows)
        c1, c2, c3 = st.columns(3)
        positions = c1.multiselect("Position", ["GK", "DEF", "MID", "FWD"],
                                   default=["GK", "DEF", "MID", "FWD"], key="plan_pos")
        sort_opts = labels + ["Total"]
        sortby = c2.selectbox("Sort by", sort_opts, index=len(sort_opts) - 1,
                              key="plan_sort")
        topn = c3.slider("Show top", 10, 120, 40, step=10, key="plan_top")
        df = df[df["Pos"].isin(positions)].sort_values(sortby, ascending=False).head(topn)
        numcols = labels + ["Total"]

        def _pl_style(_):
            css = pd.DataFrame("", index=df.index, columns=df.columns)
            for col in numcols:
                cmin, cmax = df[col].min(), df[col].max()
                if cmax > cmin:
                    for i in df.index:
                        v = df.loc[i, col]
                        if pd.notna(v):
                            css.loc[i, col] = _grad((v - cmin) / (cmax - cmin))
            return css

        fmt = {c: "{:.1f}" for c in numcols}
        fmt.update({"$m": "{:.1f}", "Own%": "{:.1f}"})
        st.dataframe(df.style.apply(_pl_style, axis=None).format(fmt),
                     hide_index=True, width="stretch", height=620)
