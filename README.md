# MatchVector

Pulls football data from [API-Football](https://www.api-football.com/) (v3) into a Supabase Postgres database.

**Leagues:** Premier League, Championship, League One, League Two, La Liga, Serie A, Bundesliga, Ligue 1. Edit them in `matchvector/config.py`.
**Data:** leagues/seasons, teams and venues, fixtures and results, per-team match statistics (including xG), standings, and pre-match odds.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in API_FOOTBALL_KEY and DATABASE_URL
python -m matchvector status    # checks the API key and shows quota
python -m matchvector init-db   # creates the tables in Supabase
```

For `DATABASE_URL`, go to the Supabase dashboard, click **Connect**, and copy the **Session pooler** string. The direct connection only works over IPv6.

## Syncing

```bash
python -m matchvector sync all                      # everything, seasons 2020-2026
python -m matchvector sync fixtures --seasons 2026  # refresh the current season only
python -m matchvector sync stats --limit 5000       # stats backfill, resumable
python -m matchvector sync odds                     # upcoming fixtures' odds
```

Run `leagues` before the other targets, because every other table references it. `sync all` does this for you.

Every sync is an upsert, so you can re-run it safely. Match stats are fetched only for finished fixtures that don't have them yet. Each call covers 20 fixtures via `/fixtures?ids=`, so one season across all 8 leagues costs about 170 calls. Syncing stops cleanly when the daily quota drops to `API_DAILY_RESERVE`. Run it again the next day to carry on.

API-Football only serves **odds** from about 14 days before kickoff, so you can't backfill history. Run `sync odds` daily. Each run keeps the latest price per bookmaker and market (the markets are listed in `ODDS_BET_IDS`).

## Nightly refresh

`python -m matchvector nightly` refreshes everything that changes. It runs these steps in order:
1. Re-checks league metadata, which marks each competition's current season.
2. Refreshes teams, fixtures/results and standings for every current season, plus any season that ended in the last 14 days.
3. Fetches stats for newly finished matches.
4. Pulls odds for upcoming matches.
5. Updates the club rankings (see below).
6. Projects the score and win/draw/loss chances for every upcoming fixture, and backfills any finished fixture that has no projection.

A normal night uses about 350–500 API calls and takes a few minutes. If one competition fails, the others still run, and the exit code is non-zero.

The GitHub Actions workflow [`.github/workflows/nightly.yml`](.github/workflows/nightly.yml) runs this command every day at 03:00 UTC. You can also start it by hand: open the **Actions** tab, choose **Nightly sync**, then **Run workflow**. It needs two repository secrets, under **Settings → Secrets and variables → Actions**:

- `API_FOOTBALL_KEY`
- `DATABASE_URL`: use the Supabase **Session pooler** string.

## Players and injuries

For the 22 leagues in `config.PLAYER_LEAGUES`, the sync pulls the tables below. They are the English top five, La Liga, Serie A, the Bundesliga and Ligue 1, plus Turkey, Saudi Arabia, MLS, Portugal, the Netherlands, Belgium, Greece, Ukraine, Czechia, Austria, Norway, Azerbaijan and Slovakia.
- `players`: profiles.
- `player_seasons`: one row per player per club per league-season, with appearances, starts, minutes, rating, goals, assists, shots, passes, tackles, duels, cards and penalties.
- `injuries`: players listed as missing or doubtful for each fixture, with the reason.

The nightly job refreshes the current season. To backfill or add a league:

```bash
python -m matchvector sync players  --leagues 39 140 --seasons 2024 2025
python -m matchvector sync injuries --leagues 39 140 --seasons 2024 2025
```

API-Football's injury lists run from 2021 for the big five, the Championship, Turkey, the Netherlands, MLS and Norway. Elsewhere there's little or nothing before 2025.
- The National League has no player data for 2025 or 2026.
- Azerbaijan has no match ratings, and no player data for 2025 or 2026.
- Ukraine and Slovakia have little or no player data for 2026 so far.

## Club ranking

This is based on the Club Ranking Google Sheet. Every finished fixture is replayed oldest first, ordered by kickoff time, with the fixture ID breaking ties. For each fixture:

- Expected goal difference = (home rank − away rank + 30) / 100
- Rank change = (actual goal difference, capped at ±3 − expected goal difference) × 6
- The home team gains the rank change and the away team loses it.

This differs from the sheet, which uses (home × 1.09 − away) / 100, × 10 and no cap. Backtesting 2023–26 showed the ×1.09 gave 0.4–1.1 goals of home advantage, when the real figure is about 0.3 for every team. A smaller K and the goal cap also stop one freak result from swinging a rank. Prediction error fell from 1.77 to 1.68 goals per match. The settings are at the top of `matchvector/ranking.py`.

A team's first rank is `leagues.starting_rank` of the first league it plays in. For a team that only ever appears in cups, it's the `starting_rank` of the first cup it plays in.

- `team_rank_history` holds each team's rank before and after every match, like the Ranking Breakdown tab.
- `team_rankings` holds the current summary, like the Ranking tab: current rank, 30 and 100 Ranking, ST ALGO, LT ALGO, and HG/HA/AG/AA over the last 12 months.
- `team_rankings` also has a `reliability` score from 0 to 100, which isn't in the sheet:
  - It's mainly driven by games played: a team scores 66% after 38 games, 89% after 76 and 96% after 114.
  - It's reduced when a team's rank swings a lot. `rank_volatility` is the standard deviation of the team's last 30 per-match rank changes. At 10.5 or below (about 78% of teams) there's no reduction. Above that the score falls with the cube: 11.8 gives ×0.70 and 14 gives ×0.42.
  - The constants are at the top of `matchvector/ranking.py`.

```bash
python -m matchvector rank   # the nightly job runs this after syncing
```

Every run replays all fixtures from scratch, which takes seconds. Late results, corrected scores and changes to `starting_rank` are all picked up automatically.

## Match predictions

`fixture_predictions` holds one row per fixture. The `upcoming_predictions` view adds team and competition names, and shows percentages. The method is based on the sheet's RG tabs:

1. **Expected margin:** (home rank − away rank + 30) / 100, plus 0.2 goals in the Champions League, Europa League and Conference League, where home sides do better.
2. **Base goals for each side:** the average of the team's own goals scored and the opponent's goals conceded, at home for the home side and away for the away side. The averages cover the last 12 months, use **xG instead of goals** for any match that has it, and are shrunk towards the competition average by 6 games.
3. **Projected goals:** a proportional version of the sheet's "Buff" scales the favourite up and the underdog down by the same factor until the margin matches. The original moved goals in a straight line, which pushed underdogs to around 0 goals and made the model far too sure they wouldn't score.
4. **Probabilities:** Poisson distributions for 0–10 goals each side give home win, draw and away win. The draw chance is boosted by up to ×1.1 in close games; the boost fades to nothing at a 1.5-goal margin.

The sheet's "36% × strength ratio" blend is dropped. Backtested log loss on 51,000 matches from 2024 to 2026: the sheet's method 1.016, the first version 1.0053, the current one 1.0035. Guessing base rates scores about 1.07.

These were tested and not adopted:
- A faster rank K for the first games after the summer break. It was worse.
- Pulling ranks towards the league level after the summer. It made no difference.
- Blending in bookmaker odds. On the first 505 matches with odds, the bookmakers alone scored best (log loss 0.975 against the model's 0.991), so blending isn't worth it yet. Re-test once there are a few thousand matches.

**Bookmaker comparison.** `export.market_probabilities` averages each bookmaker's match-winner odds with its margin removed. Match cards show these alongside the model, and the Stats tab compares model and bookmakers on every finished match that has odds.

```bash
python -m matchvector predict   # the nightly job runs this after the rankings
```

## Website

`docs/index.html` is a single-page site in the same style as MatchLab. It has two tabs:
- **Matches:** projected scores, win/draw/loss chances and results. Filter by competition and day.
- **Rankings:** club rankings with form and reliability. Tap a club to see its fixtures.

The site reads `docs/data/matches.json` (the last 21 days and the next 60) and `docs/data/rankings.json`. These are written by `python -m matchvector export`, and the nightly workflow commits them, so the site never needs database credentials. To view it on this PC, run `python -m http.server` in `docs/` and open http://localhost:8000.

**Recording projections.** Predictions for a fixture stop updating at kickoff, so the last nightly projection before the match is kept. `fixture_predictions.source` shows where each projection came from:
- `live`: recorded before kickoff.
- `backfill`: reconstructed afterwards for matches since July 2023, using each team's pre-match rank and goal averages. It shows what the current model would have said at the time.

Result cards show `proj` for live projections and `recon` for backfilled ones. The **Stats** tab (`docs/data/stats.json`) scores the projections over 7, 30 and 90 days and 12 months, by competition. It shows how often the predicted result was right, exact scores, goal error, log loss, Brier score and calibration.

**Ratings.** Every finished match's projection is rated from 1 (terrible) to 5 (excellent), using MatchLab's grading ported to `matchvector/rating.py`. There are five factors, each scored 0–5 and weighted:
- Winner 30%
- Margin 25%
- Clean sheets 20%
- Shape 15%
- Goals 10%

The weighted total is rounded, and a 0 counts as 1. The overall rating and the five factor scores are stored on `fixture_predictions` (`rating`, `rating_winner` and so on). Result cards show the rating as a coloured badge, and tapping a card shows the breakdown. The Stats tab shows the average rating, the spread of 1s to 5s and each factor's average. New results are rated nightly, and the last 14 days are re-rated in case a score was corrected.

## Tables

`leagues`, `league_seasons`, `venues`, `teams`, `team_seasons`, `fixtures`, `fixture_team_stats`, `standings`, `bookmakers`, `bet_types`, `odds`, `team_rank_history`, `team_rankings`, `fixture_predictions` (and the view `upcoming_predictions`). See `db/schema.sql`.
