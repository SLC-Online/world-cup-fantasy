# Deploy & hands-off operating guide

The goal: it runs by itself in the cloud and only pings you when a look is worthwhile
(squad locks soon, confirmed line-ups for your players, a worthwhile transfer, or a
data-quality problem). You still enter the team in the FIFA game yourself (by design —
no auto-submit).

## What runs automatically
`./wcf-run run-due` is the single auto-pilot command (the cloud calls it hourly). Each run:
1. **Learns who actually played** from the FIFA `matchStatus` feed (start/bench) and
   stores it, so the next optimise uses real line-ups, not pre-tournament name guesses.
2. **Auto-optimises** when a deadline is within the window (default 18h) and saves the team.
3. **Scans opportunities** (injuries/benchings, captain on the bench, worthwhile transfers,
   market-backed differentials, ownership risers/fallers).
4. **Confirmed-line-up window**: pings when your players kick off within ~2h and their XIs are out.
5. **Odds↔player match audit**: pings if linking quality drops or a popular player loses their price.
6. **Notifies** (Telegram + macOS), de-duplicated so you're not spammed.

## 1. Telegram pings (5 min, do this first)
1. In Telegram, message **@BotFather** → `/newbot` → copy the **token**.
2. Message your new bot anything, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat` → `id`.
3. Put both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
4. Verify: `./wcf-run notify-test`  → you should get a phone message.

## 2. Run in the cloud (GitHub Actions — laptop can be off)
1. Create a **private** GitHub repo, then from this folder:
   ```
   git remote add origin git@github.com:<you>/world-cup-fantasy.git
   git push -u origin main
   ```
2. GitHub → **Settings → Secrets and variables → Actions** → add repository secrets:
   `ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (`API_FOOTBALL_KEY` optional).
3. GitHub → **Settings → Actions → General → Workflow permissions** → **Read and write**
   (so the auto-pilot can commit updated state back).
4. The workflow (`.github/workflows/wcf.yml`) then runs **hourly**. Trigger a first run
   manually under the **Actions** tab → *WC Fantasy auto-pilot* → *Run workflow*.

`.env` is git-ignored and never leaves your machine; the cloud uses the Actions secrets.

## 3. Dashboard (Streamlit Community Cloud — optional, free)
1. Go to **share.streamlit.io** → *New app* → pick your repo → main file `dashboard.py`.
2. In the app's **Settings → Secrets**, add `ODDS_API_KEY` (same format as `.env`).
3. It deploys to a URL you can open on your phone (Team, Fixtures, Players, Group plan,
   Projections, Schedule, Health tabs).

## Alternative: run locally on a schedule (macOS, laptop on)
```
bash deploy/install_agenda.sh      # installs the launchd job (hourly run-due --notify)
```
Local-only banners + Telegram; no cloud needed. (Cloud is better for "forget about it".)

## When it pings you — what to do
- **"squad locks in …"** → open the app / saved team, eyeball it, enter it in the FIFA game.
- **"confirmed line-up window"** → check your starters are actually starting; if one is
  benched, use the in-round XI/captain change or a transfer.
- **"opportunities"** → a suggested transfer/captain move; act if you agree.
- **"odds↔player matching needs a look"** → run `./wcf-run audit-odds --round <R>` and,
  if the flagged matches look right, `--seed` to lock them.

## Handy commands
| Command | What |
|---|---|
| `./wcf-run run-due` | the auto-pilot (what the cloud runs) |
| `./wcf-run optimize --round MDx` | (re)pick the squad for a round |
| `./wcf-run show-team --round MDx` | print the saved team |
| `./wcf-run audit-odds --round MDx [--seed]` | check/lock odds↔player matching |
| `./wcf-run notify-test` | verify notifications |
| `./wcf-run record --round MDx --source fifa` | pull actual points after a round |
