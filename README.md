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

**Adding a league.** Add its API-Football id to `config.LEAGUES`, and set a starting rank for it with `update leagues set starting_rank = … where league_id = …` once the league row exists. The next nightly run spots that the league has no fixtures yet and pulls every season in `config.DEFAULT_SEASONS`, then keeps it up to date. To get it sooner, run the **Backfill leagues** workflow ([`.github/workflows/backfill.yml`](.github/workflows/backfill.yml)) with the new ids. It pulls every season's teams, fixtures, standings, match stats and odds, then re-ranks and republishes the site.

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

## Player ranks and team XI ratings

`matchvector/player_ratings.py` gives every player a **rank from 0 to 100** from his stats, for the 10 leagues with per-match player data (`config.INJURY_MODEL_LEAGUES`). Everything is **backdated**: it's replayed in kickoff order, so every number is what could have been known before that match.

**Player rank**
1. Take the player's last 20 appearances within 18 months.
2. Work out his **role** from the line-ups. `matchvector/positions.py` turns each starter's grid position and the team's formation into a role: GK, LB, CB, RB, LWB, RWB, DM, CM, AM, LM, RM, LW, RW or ST. For example, 4-2-3-1 row 4 gives LW, AM and RW. His role is the one he's started in most over the window; players only seen as substitutes use their broad position. Roles are compared in groups, with left and right together: GK, CB, full-back, DM, CM, AM, winger and ST.
3. Work out his per-90 stats and ratios, with weights set for each role group:
   - Centre-backs: mostly the match rating (60%), plus duels, passing, tackles + interceptions and blocks. Tackles, blocks and duels pile up for defenders under pressure, so they would undersell centre-backs at dominant clubs.
   - Full-backs: defending plus key passes, assists and dribbles.
   - DM: tackles + interceptions and passing.
   - AM and wingers: key passes, assists, goals and dribbles.
   - Strikers: goals and shots on target.
   - Goalkeepers: save rate and goals conceded.
   - Every role group also includes the average match rating.
4. Compare each stat with other players in the same role group.
5. **Mark down players with few minutes:** pull them towards a below-average level (−0.5, weighted as 900 minutes), not towards the average.
6. Turn the result into a **percentile among regulars in that role group**: 50 is an average regular, and 90 is better than 90% of them.
7. **Scale by club level:** multiply the percentile by the club's rank ÷ 1200 (capped at 1). The club rank is the average rank, at the time, of the clubs he played those 20 matches for, weighted by his minutes. Even a perfect player is capped by his club: at a 966 club he can reach at most 100 × 966 / 1200 = 80. Bayern (1121) caps its players at 93.

**Where it's stored**
- `fixture_player_ranks` holds each player's rank going into every match. It's a separate table, cleared and refilled on each run, so the 700,000-row `fixture_players` table isn't rewritten every night.
- `players.current_rank` holds his rank now.

**Team ratings** (`fixture_team_ratings`, for every match and team)
- **Predicted XI rating:** the average rank of the predicted XI. That's 1 goalkeeper plus 10 outfield players with the most minutes over the last 5 matches, leaving out anyone on the injury list.
- **Recent rating:** the average rank of the XIs actually started in the last 5 matches.
- **Actual XI rating:** the average rank of the XI that started, for finished matches.
- `predicted_lineups` holds the predicted XI for upcoming matches.

Line-up roles are stored in `fixture_players.role` and `fixture_players.grid`, and formations in `fixture_formations`. The site shows player ranks on the Rankings tab (Players view, filterable by role group, with each player's photo). The table shows each player's age and his **season ranks** for 26/27 back to 21/22, sorted by the current season: worked out from **that season's matches only** (`player_season_ranks`). His season totals get the same stat score as above, turned into a percentile among player-seasons with 900+ minutes in the same role group, then multiplied by his clubs' average rank that season ÷ 1200. Few minutes pull it down, scaled by how many games his club has played, so a season in progress isn't marked down for being early. A season is blank if he didn't play in these leagues. Hovering a season cell shows the clubs he played for in that season, his minutes for each, the club's average rank over those matches and his average match rating. That detail is in `docs/data/player_seasons.json`, loaded only when the Players view opens. It also shows the predicted XI for a team's next match in the team pop-up, and the XI ratings on match cards.

**Backtest (2024/25 onwards):** home XI rating minus away XI rating added nothing on top of the team ranks. Predicted XI against the recent average gave a tiny gain in the unexpected direction, so the XI ratings are shown but not used in the projections.

```bash
python -m matchvector player-ratings   # the nightly job runs this after the club rankings
```

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

1. **Expected margin:** (home match rank − away match rank + 30) / 100. Add 0.2 goals in the Champions League, Europa League and Conference League, where home sides do better.
   - Each team's **match rank** blends its current rank (Now) with LT ALGO. The blend depends on how far away the match is: 60% Now for a match today, sliding in a straight line to 20% Now for a match a year or more away (40% at six months).
   - Why: in a point-in-time backtest, 60% Now + 40% LT beat Now alone in both 2022–23 (log loss 1.0026 → 1.0012) and 2024 onwards (1.0034 → 1.0007), and beat ST, LT, the 30 and 100 Rankings and other blends. Re-running it with ranks as they stood 91, 182 and 365 days before kickoff, the best Now weight fell to 0.4, 0.4 and 0.2. Now alone was worse at every horizon.
2. **Base goals for each side:** the average of the team's own goals scored and the opponent's goals conceded, at home for the home side and away for the away side. The averages cover the last 12 months, use **xG instead of goals** for any match that has it, and are shrunk towards the competition average by 6 games.
3. **Projected goals:** a proportional version of the sheet's "Buff" scales the favourite up and the underdog down by the same factor until the margin matches. The original moved goals in a straight line, which pushed underdogs to around 0 goals and made the model far too sure they wouldn't score.
4. **Injuries**, in the 10 leagues in `config.INJURY_MODEL_LEAGUES` that have injury history: the Premier League, Championship, La Liga, Serie A, the Bundesliga, Ligue 1, Turkey, the Netherlands, MLS and Norway.
   - Each team's **missing strength** is the total, over players listed as out or doubtful, of each player's share of the team's minutes in its last 10 matches. 1.0 means one ever-present player. Long-term absentees have no recent minutes, so they add almost nothing.
   - The expected margin moves by 0.1 goals per unit of (away missing − home missing).
   - Minutes come from `fixture_players` (per-match minutes, fetched for these leagues). See `matchvector/injuries.py`.
   - In a train/test backtest (trained on 2021/22–2023/24, tested on 2024/25 onwards), it improved test log loss from 1.0066 to 1.0059. That's small but consistent.
   - Match cards show each side's missing strength.
5. **Probabilities:** Poisson distributions for 0–10 goals each side give home win, draw and away win. The draw chance is boosted by up to ×1.1 in close games; the boost fades to nothing at a 1.5-goal margin.

The sheet's "36% × strength ratio" blend is dropped. Backtested log loss on 51,000 matches from 2024 to 2026: the sheet's method 1.016, the first version 1.0053, the current one 1.0035. Guessing base rates scores about 1.07.

These were tested and not adopted:
- A faster rank K for the first games after the summer break. It was worse.
- Pulling ranks towards the league level after the summer. It made no difference.
- Variations on the injury weighting: 5 or 20 recent matches, rating-weighted, or goalkeepers and attackers weighted more. They made no real difference.
- Blending in bookmaker odds. On the first 505 matches with odds, the bookmakers alone scored best (log loss 0.975 against the model's 0.991), so blending isn't worth it yet. Re-test once there are a few thousand matches.

**Bookmaker comparison.** `export.market_probabilities` averages each bookmaker's match-winner odds with its margin removed. Match cards show these alongside the model, and the Stats tab compares model and bookmakers on every finished match that has odds.

```bash
python -m matchvector predict   # the nightly job runs this after the rankings
```

## Paper betting

`matchvector/betting.py` records the bets the model *would* place. It never uses real money. There are two strategies, tracked separately:
- **early:** placed by the nightly run for matches in the next 36 hours.
- **late:** placed by the match-day run within 75 minutes of kickoff, after late injury news.

A bet is placed when model chance × the best price across bookmakers is at least 3% better than even, at odds up to 10. Each bet is 1 unit, with at most one bet per selection per strategy. Markets:
- match result
- over/under 2.5 goals (`p_over25`)
- both teams to score (`p_btts`)

The goal-market probabilities come from the same Poisson grid, calibrated towards the base rate. Bets settle on the 90-minute score and are stored in `paper_bets`.

**Closing line value.** `odds.first_odd` keeps the opening price. `odds.odd` is never updated after kickoff, so it holds the closing price. Each bet records `clv` = odds taken × the fair closing probability − 1. Consistently positive CLV is the early sign of a real edge. Profit needs thousands of bets before it means much.

**Match-day job.** `.github/workflows/matchday.yml` runs `python -m matchvector matchday` every 30 minutes. For matches starting within 3 hours, it refreshes odds, which captures the closing price, and injury lists. It then re-projects, places the late bets, refreshes recent results and settles bets. It commits `docs/data/bets.json` only when bets change. The site's **Bets** tab shows the results.

A line-up adjustment (the strength of the starting XI against normal) was backtested and didn't help (test log loss 1.0058 against 1.0057), so line-ups aren't used in the projections.

## Website

`docs/index.html` is a single-page site in the same style as MatchLab. It has two tabs:
- **Matches:** projected scores, win/draw/loss chances and results. Filter by competition and day.
- **Rankings:** club rankings with form and reliability. Tap a club to see its fixtures.

**Club pages.** Club names link to `#/club/<team_id>`. The page shows:
- the stat tiles
- a rank-over-time chart with a hover readout
- 12-month goal averages
- next matches with projections
- recent results with each match's rank change
- the predicted XI and the squad's player ranks

Each club's history comes from `docs/data/clubs/<team_id>.json`, which is written by `export_clubs` for clubs active in the last 400 days and loaded only when the page opens.

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

`leagues`, `league_seasons`, `venues`, `teams`, `team_seasons`, `fixtures`, `fixture_team_stats`, `standings`, `bookmakers`, `bet_types`, `odds`, `team_rank_history`, `team_rankings`, `fixture_predictions` (and the view `upcoming_predictions`), `players`, `player_seasons`, `injuries`, `fixture_players`, `paper_bets`. See `db/schema.sql`.
