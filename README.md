# World Cup 2026 Fantasy — optimizer & tracker

A small Python toolkit that picks an optimal FIFA World Cup 2026 Fantasy team and
tracks it across the tournament. It turns **betting markets** (which already price
in form, injuries, expected line-ups and venue) into **expected fantasy points**
per player, then runs an **integer-linear-program optimizer** to choose the best
15 / starting XI / captain under the game's budget, squad and nation rules.

Why this game is a good fit for optimization: **player prices are fixed for the
whole tournament**, so there's no price-rise metagame. You capture the player pool
**once**, then each round only needs fresh odds.

> The repo ships with clearly-labelled **sample data** (a synthetic 12-nation
> mini-tournament) so everything runs immediately, with no API key. Swap in the
> real player pool + an Odds API key for live use.

---

## 1. Setup

```bash
cd world-cup-fantasy
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env            # then paste your Odds API key (see §3)
```

Run commands either way:

```bash
./wcf-run <command> ...                       # convenience wrapper (uses .venv)
# or
./.venv/bin/python -m wcf.cli <command> ...
```

Sanity check (uses bundled sample data):

```bash
./wcf-run project  --round MD1
./wcf-run optimize --round MD1
```

Run the tests with `./.venv/bin/python -m pytest`.

---

## 2. The sustainable workflow

**Once, before the tournament:**

1. Get the real player pool into `data/players/players.csv` (see §3).
2. Put a free Odds API key in `.env` (see §3).

**Each of the 8 rounds** (MD1, MD2, MD3, R32, R16, QF, SF, FIN):

```bash
./wcf-run fetch-odds --round MD1        # pull + snapshot betting markets
# (optional) edit data/lineups/lineups_MD1.csv with the latest team news
./wcf-run project    --round MD1        # odds + line-ups -> expected points
./wcf-run optimize   --round MD1        # best 15 / XI / captain  (saved)
```

From round 2 onwards, plan **transfers** off your previous team (it accounts for
the free-transfer allowance and -3 hits automatically):

```bash
./wcf-run optimize --round MD2 --from-existing MD1
./wcf-run optimize --round R16 --from-existing R32 --chip wildcard   # unlimited transfers
```

**After the matches**, record results and track performance:

```bash
# create data/results/results_MD1.csv  (player_id,minutes,points)
./wcf-run record --round MD1
./wcf-run report
```

---

### Staying on top of it (low attention)
You only *must* act at ~8 moments: the squad/transfer deadline before each round.
Everything else is optional polish (auto-subs + your vice cover non-players).

**Dashboard app (recommended).** Build a double-click app:
```bash
bash deploy/make_app.sh        # creates ~/Applications/World Cup Fantasy.app
```
Double-click it any time: it launches a local dashboard in your browser that
**recomputes live**, so you never have to trust a background job. It shows the
next deadline + countdown, your team, the group-stage plan, projections, your
players' lock times, and a **Health** tab (when each data source was last
refreshed, whether API keys are set, and whether the notifier is running) — so
if anything is stale or broken, you'll see it immediately. Action buttons
(re-optimise, fetch line-ups, fetch fresh odds) run the same pipeline. Run it
directly with `./.venv/bin/streamlit run dashboard.py`.

`./wcf-run agenda` prints the same "what to do now / next" summary on the CLI.

**Optional push notifier.** If you also want to be pinged:
```bash
bash deploy/install_agenda.sh        # macOS LaunchAgent; uninstall: ... uninstall
```
It runs `agenda --notify` at 09:00 and 17:00 daily and fires a macOS notification
*only* when a deadline is within ~36h or your players play within ~30h. The
dashboard's Health tab shows whether it's loaded and when it last ran, so a silent
failure is visible.

### Full auto-pilot
`./wcf-run run-due` is the one-shot "brain": it auto-optimises when a deadline is
within the window (refreshing odds only if stale), scans for **opportunities**
(unavailable players, captain/vice not in the confirmed XI, a worthwhile transfer,
low-owned differentials, a Wildcard hint), and — with `--notify` — sends a
**Telegram** message (and macOS notification), de-duplicated so repeated runs don't
spam you. Configure Telegram via `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in
`.env` (see `.env.example` for the BotFather steps).

Results are automatic too: `./wcf-run record --round MD1 --source fifa` pulls each
player's points from FIFA's feed (no manual entry), scores your team and updates
history.

### Running without your laptop (cloud)
Two independent things:
* **The dashboard** can be deployed to **Streamlit Community Cloud** for a
  view-from-anywhere URL. Caveat: it only serves the web app (it sleeps when idle
  and its storage is ephemeral), so it is *not* an automation engine.
* **The automation** (optimise + opportunity pings) runs free on **GitHub Actions**
  via `.github/workflows/wcf.yml` — a cron that runs `run-due --notify` every few
  hours and commits state back, so it works with your laptop off and pings you on
  Telegram. Setup: push the repo to GitHub, add `ODDS_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` as Actions secrets, and enable
  read/write workflow permissions (details in the workflow file). The macOS
  launchd notifier is the laptop-on equivalent.

## 3. Getting the data

### Player pool (capture once — prices never change)
`data/players/players.csv`, columns: `id,name,nation,position,price,club`.

**Easiest (default):** pull FIFA's public feed directly — no login, no HAR:
```bash
./wcf-run fetch-players
```
This joins `play.fifa.com/json/fantasy/players.json` (1,484 players, prices &
positions) with `squads.json` (the 48 nations) and writes `players.csv`. It also
saves the raw snapshot (with ownership %, player status and the game's own points)
to `data/players/` for later use. Re-run any time; once is enough since prices are
fixed.

Fallbacks if the public feed ever changes:
* `--har file.har` — auto-extract from a browser network export (open the
  squad screen, Network tab, **Save All As HAR**).
* `--json file.json` — a saved players response (`--price-divisor 10` if prices
  arrive as integers like `105` = `10.5`).
* `--csv file.csv` — import a hand-made/community list.
* `--url` — a custom endpoint set in `.env` (`FIFA_PLAYERS_URL`).

### Betting odds (free)
Sign up at **the-odds-api.com** for a free key (~500 requests/month is plenty),
put it in `.env` as `ODDS_API_KEY`. The provider pulls 1X2 + over/under (featured
markets) and **anytime goalscorer** (player props) for `soccer_fifa_world_cup`,
takes the median across bookmakers, and snapshots each pull to `data/odds/`.
With no key it falls back to the bundled sample so the pipeline still runs.

### Line-ups, the deadline, and rolling lockouts
Two different locks matter:
* **Squad + transfers** lock at the matchday's **first kickoff** (e.g. 20:00 UK,
  11 June for MD1). You commit the 15 *before* line-ups are confirmed, so the
  squad uses a prior: FIFA `status` (unavailable players are dropped) plus a
  start probability from ownership and price (this reliably identifies nailed
  starters — e.g. Raya over a backup keeper).
* **XI + captain** use **rolling lockouts** — each player locks only at *their*
  match. MD1 games run to 18 June, and official line-ups publish ~1h before each
  kickoff, so you refine your XI/captain match-by-match across the week.

Commands:
* `./wcf-run schedule --round MD1` — shows the squad deadline and each of your
  players' individual lock (kickoff) times, so you know when to check each one.
* `./wcf-run fetch-lineups --round MD1` — refreshes availability/ownership and
  pulls **confirmed XIs from FIFA's own live feed** (free, no key), overriding the
  prior for teams whose XI is out, then writes `data/lineups/lineups_MD1.csv`.
  Re-run through the matchday as more confirm, then re-run `project`/`optimize`.
  (`--source apifootball` is available but the API-Football *free* plan does not
  cover season 2026.) A hand-made `lineups_<round>.csv` (`player_id,status` with
  status=start|bench|doubt|out) works from any source and always wins.

---

## 4. How the expected-points model works

For each player in a round:

```
EP = appearance + goals + assists + clean sheet + goals conceded
     + (GK saves | MID tackles & chances | FWD shots on target) + cards
```

* **Match expected goals** `mu` for each team come from the 1X2 (+ totals) market
  via an independent-Poisson model (vig removed; supremacy solved by bisection).
* **Goals**: anytime-scorer price → `P(scores ≥1)` → `lambda = -ln(1-P)`, then a
  team's player rates are rescaled so they sum to that team's `mu` (this removes
  the goalscorer market's overround and ties player goals to the match).
* **Clean sheet** = `e^(-mu_opponent)`; **goals conceded** uses the game's rule
  that the first concede is free and each extra costs the GK/DEF a point.
* **Assists** are shared across a team from its `mu`, weighted by position and
  goal threat. **Bonus categories** (tackles/chances/shots/saves) use per-90
  priors you can override with a stats file. Every component is saved so a
  projection is fully auditable (`data/projections/`).

The **scoring rules** are the single source of truth in `wcf/scoring.py`; the same
numbers drive both projections and actual-result scoring. One value is flagged as
ambiguous across sources (keeper goal = 9) and is trivial to change — it has
no practical effect since keepers don't score.

---

## 5. The optimizer

An integer linear program (PuLP + CBC) maximises starters' EP + captain (doubled)
+ a small bench credit, subject to:

* squad 2 GK / 5 DEF / 5 MID / 3 FWD, 15 total;
* budget (\$100m group stage, \$105m from R32);
* ≤ nation limit per country (3 → 4 → 5 → 6 → 8 as the knockouts progress);
* a legal XI (1 GK, 3–5 DEF, 3–5 MID, 1–3 FWD = the game's seven formations);
* captain is one starter.

In transfer mode it adds the cost of moves beyond the free allowance (−3 each) and
decides whether a hit is worth it. A pure-Python heuristic runs if PuLP is missing.

### Multi-round (group-stage) planning
A fresh build during the group stage plans **across the remaining group games**,
not just the next one. `optimize` does this automatically (`--horizon 0` = auto:
3 games at MD1, 2 at MD2, 1 at MD3); override with `--horizon N`.

It maximises the discounted sum of each round's best-XI points (default weights
1.0 / 0.6 / 0.4 — later rounds count less, being less certain). MD2/MD3 goal rates
reuse each player's MD1 goalscorer *share* applied to those fixtures' expected
goals, so no extra odds calls are needed.

**Planned transfers.** The squad may change by up to `--planned-transfers N`
players between rounds (default **1**) — enough to earmark a one-week punt for an
upgrade, while deliberately *reserving* the rest of your free-transfer allowance
for injuries and rotation. `0` holds the squad; setting it to the full allowance
assumes every transfer goes on upgrades (not advised). The printed plan shows the
intended OUT→IN per round (`data/teams/plan_<round>.json`); you still re-optimise
each round on fresh odds/line-ups via `--from-existing`.

### Risk-adjusted captaincy
A captain is one doubled bet, so its *ceiling* matters more than its mean. From
the goal rate (Poisson) and clean-sheet probability (Bernoulli) the tool builds
each candidate's full points distribution — mean, standard deviation, an
85th-percentile ceiling and P(haul) — and captains on a risk setting:
`--captain-risk safe|balanced|upside` (default balanced, weighting the ceiling).
The captaincy table is printed so the choice is transparent. By default only
MID/FWD are eligible; `--captain-any` allows GK/DEF.

---

## 6. Eight-round playbook (rules reference)

| Round | Budget | Max/nation | Free transfers |
|------|-------:|-----------:|----------------|
| MD1  | 100 | 3 | unlimited (pre-tournament) |
| MD2  | 100 | 3 | 2 |
| MD3  | 100 | 3 | 2 (one can roll over within the group stage) |
| R32  | 105 | 3 | unlimited |
| R16  | 105 | 4 | 4 |
| QF   | 105 | 5 | 4 |
| SF   | 105 | 6 | 5 |
| Final| 105 | 8 | 6 |

Chips (one per round, five total): **Wildcard** (unlimited transfers a round — not
MD1 or R32), **12th Man** (an extra out-of-squad player scores that round),
**Maximum Captain** (auto-captains your top scorer), **Qualification Booster**
(+2 to starters who progress, R32+), **Mystery** (revealed before R32). The tool
records the chip on a team; pass `--chip wildcard` to optimize with unlimited
transfers.

**Live-round note:** the game lets you change your XI/captain before each player's
match kicks off. You can ignore that and play set-and-forget (auto-subs + vice
cover non-players, which `record` simulates). To squeeze the extra edge, re-run
`optimize` near a deadline and make only the captain/bench tweaks it suggests.

---

## 7. Caveats

* Goals, clean sheets and appearances are well-modelled; assists, cards and the
  tackle/chance/save bonuses are noisier approximations (improve them with a
  per-90 stats file).
* A 48-team World Cup is high-variance (rotation, fatigue, heat, shocks). This
  gives you a strong, low-admin team and removes the rules overhead — it won't
  guarantee a top global finish. The edge is biggest in mini-leagues.
* Team submission is manual by design: the tool prints/saves the team, you enter
  it in the game. (Automating login/submission would brush FIFA's terms.)

---

## 8. Project layout

```
wcf/
  config.py          paths, round specs (budget/nation/transfers)
  scoring.py         scoring rules (single source of truth) + actual scoring
  odds_math.py       odds -> probabilities -> expected goals (Poisson/Skellam)
  projections.py     expected-points engine
  optimizer.py       ILP (PuLP) + heuristic fallback
  persistence.py     CSV/JSON store: projections, teams, results, history
  cli.py             command-line interface
  providers/         odds_api.py, players.py, lineups.py
data/                players/, fixtures/, lineups/, odds/, projections/,
                     teams/, results/, history.csv
tests/               scoring, odds maths, projections, optimizer
data/_generate_sample.py   regenerates the sample mini-tournament
```

Commands: `fetch-players`, `fetch-odds`, `project`, `optimize`, `show-team`,
`record`, `report` (each with `--help`).
