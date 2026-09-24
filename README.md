# The Corner FC

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

**Query cache.** The nightly job keeps a local copy of the big historical query results (every player appearance, finished fixture with xG, rank history and injury list) in `.cache/`, so it doesn't download them from Supabase every night. See `matchvector/cache.py`.
- Each run, the database sends one fingerprint (row count and a hash) per week of matches, and only weeks whose fingerprint changed are downloaded again. New results, corrected scores, deleted rows and old matches added by a league backfill are all picked up.
- In GitHub Actions the folder is kept between runs with `actions/cache` (about 75 MB). Without it, for example on a new machine, the first run downloads everything once.
- A night's database egress fell from about 350 MB to about 30 MB.

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

`matchvector/player_ratings.py` gives every player a **rank from 0 to 100** from his stats, for the 13 leagues with per-match player data (`config.MATCH_PLAYER_LEAGUES`). That's the 10 injury-model leagues plus League One, League Two and the Saudi Pro League, whose match-by-match data runs from 2020/21. Players are always compared with the players in the original 10 leagues (`config.RATING_REFERENCE_LEAGUES`) for stat averages, percentiles and the "regulars" sample. So adding a league doesn't move everyone else's rank. Everything is **backdated**: it's replayed in kickoff order, so every number is what could have been known before that match.

**Player rank**
1. Take the player's last 20 appearances within 18 months.
2. Work out his **role** from the line-ups. `matchvector/positions.py` turns each starter's grid position and the team's formation into a role: GK, LB, CB, RB, LWB, RWB, DM, CM, AM, LM, RM, LW, RW or ST. For example, 4-2-3-1 row 4 gives LW, AM and RW. His role is the one he's started in most over the window; players only seen as substitutes use their broad position. A **season** rank uses the role group he started the most minutes in that season, with the roles summed by group. So 30% LW + 30% RW + 40% ST is rated as a winger, not a striker. Roles are compared in groups, with left and right together: GK, CB, full-back, DM, CM, AM, winger and ST.
3. Work out his per-90 stats and ratios, with weights set for each role group. The weights lean on the stats that reflect lasting ability:
   - **Centre-backs:** duels won, passing volume and accuracy, tackles + interceptions and blocks. Minus times dribbled past, his team's xG conceded while he's on the pitch, and fouls and cards. Scoring isn't his job, so goals and shots on target are left out. His match rating has the goal bonus taken off: API-Football adds about 0.78 to a centre-back's rating when he scores (6.93 against 7.70). The same goes for full-backs (+0.82) and defensive mids (+0.79). The assist bonus (about +0.5) stays, because creating chances is part of those jobs. Each stat was checked on centre-backs with 1,500+ minutes in back-to-back seasons, by how well it repeats from one season to the next, overall and after a move to another club:

     | Stat | Repeat, all | Repeat, changed club | Used? |
     |---|---|---|---|
     | Dribbled past | 0.63 | 0.55 | Yes: a player trait |
     | Team xG conceded | 0.42 | 0.29 | Yes, lightly: mostly the team |
     | Penalties conceded | 0.04 | 0.02 | No: noise |
     | Goals | 0.17 | 0.08 | No: luck |
     | Shots on target | 0.37 | 0.18 | No: team and role |

     Dribbled past and penalties conceded come from API-Football's per-match stats (`fixture_players.dribbled_past`, `penalties_committed`, fetched for every match).
   - **Full-backs:** tackles + interceptions, duels won and times dribbled past (defending), plus dribbles won, key passes and passing (going forward). Goals, shots on target, assists and team xG conceded are left out. Assists repeat only 0.10 for full-backs who changed club, which is luck beyond key passes, and team xG conceded repeats 0.05, which is all team.
   - **Defensive mids:** passing volume, tackles + interceptions, duels won, times dribbled past and key passes. There's only a sliver of shots on target, and no goals or team xG conceded (it goes negative across a move).
   - CM and AM: key passes, shots on target, passing, goals and dribbles.
   - Wingers: shots on target, key passes, goals, dribbles and assists.
   - Strikers: goals (33%), shots on target (25%), key passes and duels. Goals per 90 repeat 0.44 for strikers who changed club (0.36 for wingers, 0.27 for attacking mids), almost as well as shots on target, so scoring counts more for attackers. Wingers' goals are 22% and attacking mids' 17%. Conversion rate (0.17) is luck and isn't used.
   - Goalkeepers: match rating (75%, corrected for workload) and save % (25%). Save % repeats 0.28 for keepers who changed club, which is some skill. Saves per 90 (0.18) mostly measure how much work his defence lets through, and goals prevented against xG (0.05) is noise, so neither is used. On the rating's workload correction: Busy keepers earn rating points for saves: Raya was 7.21 at Brentford (4.1 saves per 90) and 6.86 at Arsenal (1.4). So a keeper's rating has 0.10 taken off per save per 90 above the average of 2.9, or added per save below it. That was the correction that made ratings most consistent for keepers who changed club, and it took Raya's move from 7.21 → 6.86 to 7.09 → 7.01. Goals conceded mostly measures the defence in front of him, and save % is mostly luck. For example, Trafford went from 41 to 96 moving from a relegated Premier League side to the Championship's best defence. Rating alone repeated best from season to season: 0.60, and 0.61 for keepers who changed club. A keeper's season rating only repeats at about 0.28 from one season to the next, against about 0.55 for outfield players, so one keeper season says little on its own (see season ranks below).
   - **Keeper rank leans on club level.** Even after all that, regular Premier League keepers sit within 6.8–7.05, a gap that's mostly noise. Stretching it over the whole scale gave near-random ranks (Henderson 37, Kelleher 67). So a keeper's rank is 100 × club rank ÷ 1200 − 6, plus 0.2 × (rating percentile − 50). Club level carries it, since good clubs sign good keepers, and the rating moves it by up to about ±10.
   - **Match ratings are league-adjusted everywhere:** each rating has the league's average for that position taken off, and the average across all leagues added back. The adjustments are small, from −0.09 for Norwegian keepers to +0.06 for Dutch keepers.
   - Two things were tested for keepers and not used:
     - *API-Football's "goals prevented":* it only exists from 2024/25, and it gives the same number to both teams in a match, so it can't say whose it is.
     - *Adjusting ratings for shots faced:* it barely moved anything and repeated no better.
   - Match rating is only 10–12% for outfield players.
   - **How the weights were checked:**
     - *Against results (2021–26):* with club rank as the baseline, match rating added nothing. Shots on target, key passes, passing volume and duels won did. Goals, assists and save % beyond those were mostly luck that evened out.
     - *For repeatability:* these weights repeat better from season to season than the old rating-heavy ones. The Spearman correlation is 0.54 against 0.50, and 0.44 against 0.40 for players who changed clubs.
4. Compare each stat with other players in the same role group.
5. **Mark down players with few minutes:** pull them towards a below-average level (−0.5, weighted as 900 minutes), not towards the average.
6. Turn the result into a **percentile among regulars in that role group**: 50 is an average regular, and 90 is better than 90% of them.
7. **Club level first, stats adjust it** (all players): rank = 100 × club rank ÷ 1200 − 8, plus 0.3 × (stats percentile − 50). The −8 (it was −12) puts an average Premier League regular at about 75. Club level sets the base, because being a regular for a strong club is good evidence of quality, and stats move a player by up to about ±15 (keepers ±10, since their stats are noisier). **Elite seasons get extra:** an outfield player earns 1 more point for each percentile above the 90th, up to +10, so a club's level doesn't cap a great player. Everyone below the 90th percentile is unchanged. **The top of the scale is a soft ceiling rather than a hard cap at 100:** above 86, a rank r becomes 86 + 14 × (1 − e^(−(r − 86) / 14)). So the best still spread out below 100 instead of piling up against it, in the same order. Without it, Van Dijk read about 99 in every season and six players had a current rank of exactly 100. With it:

| Player | Season ranks |
|---|---|
| Van Dijk | 91–94.5 |
| Davies | 86–91 |
| Messi at PSG (LT about 1040), 22/23 | 94.2, up from 87 before the elite bonus |
| Kane | up to 96.4 |

The same ceiling applies to keepers.

**Low ranks are lifted more than high ones.** After the ceiling, every rank's gap to 100 is multiplied by 0.85: 95 becomes 95.8, 75 becomes 78.8, and 60 becomes 66. Adding a constant lifted everyone equally, but the lower half of the scale was spread too far down. Squad players at mid-table clubs sat in the 60s, for example Awoniyi at 66. The starting level (64.3) and the hand-set teenage steps (+3.4 a year at 17, +1.7 more for each year younger) are on the same scale. `GAP_SCALE` in `matchvector/player_ratings.py`.

**Positions aren't worth the same.** Stats are compared within a role group, so without an adjustment a 97th-percentile full-back counted for more than a 94th-percentile striker. That put Davies above Haaland and Alexander-Arnold above Kane. So each role group's stats part is scaled, and a flat offset is added:

| Role group | Stats × | Offset |
|---|---|---|
| Striker | 1.0 | +2 |
| Winger, attacking mid | 1.0 | +1 |
| Central mid | 0.85 | 0 |
| Defensive mid | 0.85 | −1 |
| Centre-back | 0.7 | −2 |
| Full-back | 0.7 | −3 |

These are set by judgement. The results data can't measure position value, because the XI ratings added nothing on top of the club ranks. The settings are `POSITION_STATS` and `POSITION_OFFSET` in `matchvector/player_ratings.py`. The club rank is the clubs' **LT ALGO going into each match** he played for them (weighted by his minutes), rather than their rank on the day, so one hot or cold run doesn't swing a whole squad. It's stored as `team_rank_history.lt_before`.

**Where it's stored**
- `fixture_player_ranks` holds each player's rank going into every match. It's a separate table, cleared and refilled on each run, so the 700,000-row `fixture_players` table isn't rewritten every night.
- `players.current_rank` holds his rank now, which is his season rank for the current season (see season ranks below), so the players list follows the same age curve and minutes weighting as his seasons. The last-20-appearances rank above is what goes into each match (`fixture_player_ranks`) and the team XI ratings.

**Team ratings** (`fixture_team_ratings`, for every match and team)
- **Predicted XI rating:** the average rank of the predicted XI. That's 1 goalkeeper plus 10 outfield players with the most minutes over the last 5 matches, leaving out anyone on the injury list.
- **Recent rating:** the average rank of the XIs actually started in the last 5 matches.
- **Actual XI rating:** the average rank of the XI that started, for finished matches.
- **By line:** the same averages for the goalkeeper, defenders, midfielders and forwards, for both the XI that started and the predicted XI (`fixture_team_ratings.actual_gk`, `actual_def`, `actual_mid`, `actual_fwd` and the `predicted_…` columns). A starter's line is the role he started in. LM and RM count as midfield here, although they're rated alongside the wingers; LW, RW and ST are forwards. The match cards show them when you hover the XI rating, and each club result shows them after the competition.
- `predicted_lineups` holds the predicted XI for upcoming matches.

Line-up roles are stored in `fixture_players.role` and `fixture_players.grid`, and formations in `fixture_formations`. The site shows player ranks on the Rankings tab (Players view, filterable by role group). Each row has the player's club badge and photo, with his club and nationality under his name. A player is listed if any of these hold:
- he has 450+ minutes in his last 20 appearances;
- he has a season with 1,500+ minutes among the seasons shown, which keeps established players who've been injured, such as John Stones;
- he's in a current squad, which lists new signings straight away.

**His club** comes from, in order:
1. the squad he's in now: every club in the 13 leagues has its squad fetched nightly from `/players/squads?team=` (`team_squads`, `python -m matchvector sync squads`);
2. the club the weekly current-club check found this season, which is often outside our leagues;
3. his last appearance, if neither of those is known.

So a player who has left drops off his old club's list, even before he plays for his new one. A nationality can be corrected by hand in `player_overrides`, for example Elliot Anderson as England. The nightly sync would overwrite a change made on `players` itself. Clicking his name opens a **player page** (`#/player/<id>`). A header shows his club, league, position, age, nationality and current rank, and four tabs sit under it:
- **Overview:**
  - his club's next match, with the win chance, the projected score, and whether he's in the predicted XI, doubtful or out (with the injury reason);
  - a chart of his rank going into each of his last 20 league matches;
  - **a pitch of his positions** (the spots of the position filter): in every position he can play, his rank there and his share of starting minutes in that spot, over the last 12 months or ever (our data runs from 2020/21). Ranks are per role group, so LB and RB share his full-back rank. Positions he hasn't started in (0%) are left blank; hovering over one still shows his rank there if he has one. Starting minutes are the role he started in, from the line-up and formation; minutes off the bench have no position and aren't counted;
  - his match ratings over the last 10 matches, and this season so far.
- **Stats:** one season's league stats, as totals or per 90 minutes: attacking, passing, defending and discipline (goalkeeping for keepers), added up across his clubs that season.
- **Matches:** his last 20 league appearances, with the result, minutes, position, key stats, cards, match rating and his rank going into the match.
- **Career:** a chart of his season ranks (estimated seasons hollow), a season-by-season table (club, club rank, minutes, rating, goals and assists, positions, and which position group the season was rated as), and the clubs his current rank is built on.

The Stats and Matches tabs, and the injury status, come from `docs/data/players/<player_id>.json`, which is loaded only when the page opens. `export_player_pages` writes one file for each listed player, about 2.5 KB each. It builds them from the query cache (appearances, finished fixtures and the season totals in leagues without per-match data), so the only new database reads are the per-match ranks for those 20 matches and injuries for upcoming fixtures. Season stats in leagues without per-match data come from `player_seasons` totals, so starts and pass accuracy are missing there.

Clicking a nationality opens a **nationality page** (`#/nation/<name>`) with:
- how many ranked players it has, the best XI's average rank, and the average age;
- a best XI by current rank (1 keeper, 2 centre-backs, 2 full-backs, 3 midfielders and 3 forwards);
- which leagues they play in;
- every one of those players, ranked.

Player names on club pages and in predicted XIs link to the player page too. Above the league filter there's an age range slider, and a small pitch of positions you click to filter by (you can pick several). A player's position on the site is the one where he's started the most minutes over the last 12 months. Szoboszlai is AM (1,170 minutes) rather than DM (891). A player shows under a position if it's his main one, or if he started there for 25%+ of his starting minutes in the last 12 months (`positions_12m` in `players.json`). Bernardo Silva appears under CM and DM, for example. With positions picked, the table adds an **"As ST"** column (or whichever position) and sorts by it. It shows how good he is now in that position (`player_position_ranks`, one per outfield role group):
- **Stats as that position:** his last three seasons' stats (450+ minutes each, recent seasons weighted more) are scored with that position's weights, against its players and with its position weighting. The difference from the position he actually played is added to his current rank.
- **Familiarity:** up to 6 points come off for a position he hasn't played. There's no penalty once it's 40% of his starting minutes.
- **Anchored on his shown position:** his rank in the position shown for him (Pos) always equals his overall rank. Other positions sit below it by how much worse his stats fit them, and no position is ever above his overall rank. Cherki was a winger in 24/25 and an attacking mid since. He's 92.1 as an AM, the same as overall, and 91.0 as a winger.
- **Only positions he has played:** a position only gets a rank if he has started there at some point in our data (from 2020/21). With several positions picked, the column shows his best of them.

Gakpo is 90.3 as a winger but 85.8 as a striker, below Dembélé (89.2) and Isak (90.1). Van Dijk is 92.7 as a CB and 87.7 as a DM. The player page lists his rank in every position. Under it, **Club & nationality** takes one or more clubs and nationalities, typed or picked from a list. Each shows as a chip you click to remove. They combine with the other filters, except that a club shows its players whatever league is selected. The counts in the filter follow both. The table shows each player's age and his **season ranks** for 26/27 back to 21/22, sorted by the current season (`player_season_ranks`, see below).

**Season ranks follow the age curve.** Every player follows the typical age curve through all his seasons, and only moves off it where he has the minutes to (`season_model` in `matchvector/player_ratings.py`):
1. **Evidence for each season he played:** his clubs' average LT ALGO over his matches that season, moved by his stat score. The score is the same as above, as a percentile among player-seasons with 900+ minutes in the same role group. It goes through the same club-first formula as above, with no smoothing across seasons. A player under 70% of his club's minutes (80% for keepers) is scaled down by up to 20%, because a rotation player at a top club is evidence of being below its regulars. Gabriel Jesus played 55% of City's minutes in 21/22 and 44% of Arsenal's in 23/24.
   - **Seasons in leagues without match data** (Portugal, Belgium, Greece and the other player leagues) come from API-Football's season totals (`player_seasons`), scored the same way against the same players and at the club's real level that season. Their match ratings have the league's offset taken off (Portuguese ratings run high), Pass accuracy is left out where the API has none. Other stats the API doesn't have are left out too, rather than counted as zero. A season with no match rating uses 6.5. For example, the National League only has minutes, goals and assists. Liam Mandeville's 22/23 at Chesterfield (10 goals) used to be scored as if he had no shots, passes or duels at all. Gyökeres's Sporting seasons (29 and 39 league goals) used to count as average stats at Sporting's level. They now read about 90, and his 26/27 at Arsenal is about 83.
2. **The age curve depends on position.** Each player uses the curve for the role group he's played most minutes in. It's in rank points, measured from how evidence changes from one season to the next, for players with 1,500+ minutes in the first season and 900+ in the next. The lower bar for the next season keeps players who lost their place, so decline isn't understated. Seasons chosen for their minutes tend to be good ones, so the next season is worse on average at every age (regression to the mean, about −0.7 a year). That's measured and taken off, otherwise it looks like players decline from 23.
   - **Growth to 24**, measured: +4.1 a year at 18, +2.4 at 21, +1.1 at 24. Keepers gain about +1 a year from 22 to 24.
   - **A flat prime until 31**, or **33 for keepers.** This is set, not measured: the data is too noisy to place it.
   - **Then decline that speeds up every year:** the yearly change is β × years past the start of the decline, with β fitted for each role group. Groups with less data lean towards the all-outfield β, weighted as 200 season pairs.

     | Role group | β | Curve at 35 | at 40 |
     |---|---|---|---|
     | Striker | −0.33 | −2.0 | −12.0 |
     | Centre-back, full-back, CM, AM | −0.23 to −0.24 | about −1.4 | about −8.5 |
     | Winger | −0.15 | −0.9 | −5.3 |
     | Defensive mid | −0.10 | −0.6 | −3.7 |
     | Keeper (from 33) | −0.29 | −0.3 | −6.2 |

     Kane (32) is still on the flat part of his curve. Giroud's curve is −9 at 39, and Neuer's is −6 at 40.
   - **Below 18** there are too few regulars to measure, so it's set by hand: +3.4 a year at 17, and 1.7 more for each year younger.
3. **His level on the curve** is the average of (evidence − curve) over all his seasons, weighted by minutes. When rating one season, the others count 0.7 ^ years apart. A starting level of 64.3 (his rank at peak age) is added in, weighted as 450 minutes, so a player with little data anywhere sits below an average regular.
4. **Season rank** = level + curve for that season, plus the season's own difference from it, kept in proportion minutes ÷ (minutes + 1,500). A full season (3,000 minutes) keeps two thirds of its difference. A thin one stays on his curve: an injury year, the first weeks of this season, or a teenager's debut. Rodri's 24/25 (80 minutes, injured) is 93.6, where the old model gave 79. Tah's weak 22/23 (evidence 68) is 72, between his curve (79) and the season.

Holding out each 1,500+ minute season and predicting it from the player's other seasons (level + curve) misses by 6.0 rank points on average. The settings are at the top of `matchvector/player_ratings.py`: `PRIOR_LEVEL`, `PRIOR_MINUTES`, `LEVEL_DECAY` and `DEVIATION_MINUTES`.

**Every season from 21/22 to now has a number for every player.** A season with no minutes in these leagues is level + curve. That can be a year in a league without player data, the years before his debut here, or the current season if he hasn't played yet. Lamine Yamal reads 65 at 14, 67 at 15 (11 minutes) and 83 in his first full season. His club that season comes from API-Football's list of the clubs a player has been at (`player_career_teams`, fetched once per player with a gap, and by the nightly job for new ones). A club we have fixtures for but no player data, a lower league say, adds its level as a little evidence, weighted as 450 minutes. For example, Joan García's 23/24 is Espanyol in the Segunda. The hover names the club. It's stored with 0 minutes and shown on the site as an outlined chip in italics.

**Retired players are taken off the list.** API-Football has no retired flag. So every night, players who played in the last 18 months but not yet this season have their current club looked up with `/players/squads`, at most once a week each (`python -m matchvector sync retired`, stored in `player_career_checks`). National and youth squads don't count. If a check made after his last season finds him in no club's squad, and he's 34 or older, he counts as retired. A younger player without a club isn't marked retired, because he's usually a free agent or a late transfer the squad lists haven't caught up with yet (Sancho, Ramsdale and Odobert all showed no squad in September 2026). He still leaves the list 18 months after his last match, like everyone else. He comes off the players list, and his season ranks stop at his last season instead of being estimated forward. If he plays again, or a later check finds him at a club, he's back. A free agent of 34+ drops off too, until he signs somewhere. `/players/teams` doesn't work for this: early in a season it often doesn't list the new season yet, even for regulars. It flagged Buongiorno and Ansu Fati.

The top 10% of the scale is spread by how far a player's score is above the 90th percentile, up to the best seasons on record, so the very best stand apart instead of all sitting at about 99. A season is blank after a player has retired. Hovering a season cell shows his age that season: his age now for the current season, and one less for each season before. Then, for each club he played for that season, it shows the club's average rank over his matches, followed by his minutes, average match rating, goals and assists. That detail is in `docs/data/player_seasons.json`, loaded only when the Players view opens. It also shows the predicted XI for a team's next match in the team pop-up, and the XI ratings on match cards.

**Backtest (2024/25 onwards):** home XI rating minus away XI rating added nothing on top of the team ranks. Predicted XI against the recent average gave a tiny gain in the unexpected direction, so the XI ratings are shown but not used in the projections.

```bash
python -m matchvector player-ratings   # the nightly job runs this after the club rankings
```

## Club ranking

This is based on the Club Ranking Google Sheet. Every finished fixture is replayed oldest first, ordered by kickoff time, with the fixture ID breaking ties. For each fixture:

- Expected goal difference = (home rank − away rank + 30) / 100
- Result = actual goal difference, capped at ±3. When both teams have **xG** for the match, result = 30% capped goal difference + 70% xG difference.
- Rank change = (result − expected goal difference) × K, where K is 6, or 10 for matches with xG.
- One-off curtain-raisers count for a third: the rank change is × 1/3 for the **Community Shield** and the **UEFA Super Cup** (`COMPETITION_WEIGHT`). Sides treat them as pre-season, so a result says less than a league match. The 1/3 is World Football Elo's friendly-to-World-Cup ratio. Only two or three of these games are played a year, too few to backtest a value.
- The home team gains the rank change and the away team loses it.

This differs from the sheet, which uses (home × 1.09 − away) / 100, × 10 and no cap. Backtesting 2023–26 showed the ×1.09 gave 0.4–1.1 goals of home advantage, when the real figure is about 0.3 for every team. A smaller K and the goal cap also stop one freak result from swinging a rank. Prediction error fell from 1.77 to 1.68 goals per match. The settings are at the top of `matchvector/ranking.py`.

**Why xG is blended in.** xG exists from 2022/23, for about 35–50% of matches (the bigger leagues). Replaying the rankings and projections from 2024/25 onwards:

| Result used | Log loss, all matches | Log loss, matches with xG |
|---|---|---|
| Goals only (before) | 1.00046 | 1.00446 |
| xG only, K 10 | 0.99889 | 1.00085 |
| 30% goals + 70% xG, K 10 | 0.99834 | 0.99993 |

- The blend was better in 2023/24, 2024/25 and 2025/26 taken separately.
- Anything from 25–35% goals with K 10–11 scored about the same.
- An xG difference is much less noisy than a goal difference, so those matches take a bigger K.
- Capping the xG difference made it worse, and home advantage and K for matches without xG were best left as they were.

`team_rank_history.act_diff` still holds the actual goal difference.

**Attack, defence, home and away.** These are worked out alongside the rank from the same replay (`side_ratings` in `matchvector/ranking.py`). They don't change the rank.
- **Attack and defence** average to the rank (Form), on the same scale. Expected home goals = the competition's average home goals + ((home attack − away defence) / 2 + 15) / 100, and the same the other way round for away goals. So 200 points of attack over the other side's defence is about one more goal. After each match, both sides move by 1.0 × (actual total goals − expected total) / 2: a club in high-scoring games drifts towards attack, one in low-scoring games towards defence. Goals are capped at 5 a side and blended with xG like the rank. Replaying 2024/25 onwards, this cut the error on total goals from 1.4205 to 1.4083. On its own, learning rates from 0.5 to 1 scored about the same; 1.0 was best for the projections. For example, Arsenal lean on defence and Barcelona and Bayern on attack.
- **Home and away** are the rank plus or minus the club's own home edge, on top of the standard 30 points. Both sides' edge moves by 0.2 × (result − expected), so a club doing better at home than away builds a positive edge. The gain was small (goal-difference error 1.3220 → 1.3205). Most clubs' edges are within a few points, because home advantage is mostly the same for everyone.
- Stored after every match in `team_rank_history` (`attack_after`, `defence_after`, `home_after`, `away_after`, plus `split_before`, `edge_before` and `goal_base` going into it, for the projections) and for now in `team_rankings` (`attack`, `defence`, `home_rating`, `away_rating`). The club page and team popup show them.
- The projections use them (see Match predictions). The attack learning rate (1.0) was tuned with the projections on 2023/24.

**Line-ups in the rank update: tested, not used.** The idea: when a club starts a weaker XI than usual and loses, its rank should fall less. The expected goal difference in the update was shifted by the starting XI rating against the club's usual one (actual − recent XI rating), for the 13 leagues with line-up ratings. Replaying every fixture and scoring 2024/25 onwards with line-up-free forecasts, it didn't help: log loss 1.00490 with no shift, 1.00496, 1.00513 and 1.00579 with shifts of 0.06, 0.12 and 0.24 goals per XI point. Goal-difference error was 1.6676 at best, with no shift. Rotation does matter at the extremes: a side starting an XI 4–8 points weaker than its opponent's shortfall does about 0.2 goals worse. But only about 4% of line-up-rated matches have gaps that big. A squad player's rank is built from his club's level too, so reserves rate close to starters. The XI gap's spread is only 1.9 points (about 0.08 goals). The pre-match predicted XI gap also cut goal-difference error by only 0.1% out of sample (1.6299 → 1.6284), in line with the prediction backtests further down.

A team's first rank is `leagues.starting_rank` of the first league it plays in. For a team that only ever appears in cups, it's the `starting_rank` of the first cup it plays in.

- `team_rank_history` holds each team's rank before and after every match, like the Ranking Breakdown tab.
- `team_rankings` holds the current summary, like the Ranking tab: current rank, 30 and 100 Ranking, ST ALGO, LT ALGO, and HG/HA/AG/AA over the last 12 months.
- `team_rankings` also has a `reliability` score from 0 to 100, which isn't in the sheet:
  - It's mainly driven by games played: a team scores 66% after 38 games, 89% after 76 and 96% after 114.
  - It's reduced when a team's rank swings a lot. `rank_volatility` is the standard deviation of the team's last 30 per-match rank changes, measured at K 6: a match with xG moves the rank with K 10, so its change is scaled by 6/10. That way it measures how surprising a team's results are, not the size of the step. At 10.5 or below (about 78% of teams) there's no reduction. Above that the score falls with the cube: 11.8 gives ×0.70 and 14 gives ×0.42.
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
5. **Attack, defence, home edge and line-ups** (added September 2026; see Club ranking for how each is worked out):
   - **Home edge:** the expected margin moves by (the home side's own home edge + the away side's) / 100.
   - **Line-ups:** the expected margin moves by 0.005 goals per point of difference between the two predicted XIs' average rank in defence, midfield and attack, separately. It's only used when both sides have all four lines (the 13 leagues with line-up ratings). The goalkeeper line made predictions worse, so it isn't used.
   - **How open the game is:** the base goal total is 75% the attack/defence model's (the competition's goal base + both sides' attack/defence split) and 25% the 12-month averages'. The home/away shape stays the 12-month one.
   - These were tuned on 2023/24 and tested on 2024/25 onwards (44,339 matches, injuries left out of both). Each part helped on its own, and together they helped every market:

     | | W/D/L log loss | Over 2.5 | Both teams score | Goals RMSE |
     |---|---|---|---|---|
     | Before | 0.99847 | 0.67923 | 0.68747 | 1.1739 |
     | + attack/defence goals | 0.99822 | 0.67718 | 0.68674 | 1.1717 |
     | + home edge | 0.99812 | 0.67922 | 0.68748 | 1.1735 |
     | + line-ups by line | 0.99795 | 0.67917 | 0.68751 | 1.1738 |
     | All three (current) | **0.99734** | **0.67715** | **0.68678** | **1.1712** |

     In the line-up leagues alone, W/D/L went from 1.00392 to 1.00153. Predictions already made before the change, backfilled or live, are left as they were.
6. **Probabilities:** Poisson distributions for 0–10 goals each side give home win, draw and away win. The draw chance is boosted by up to ×1.1 in close games; the boost fades to nothing at a 1.5-goal margin.

The sheet's "36% × strength ratio" blend is dropped. Backtested log loss on 51,000 matches from 2024 to 2026: the sheet's method 1.016, the first version 1.0053, the current one 1.0035. Guessing base rates scores about 1.07.

These were tested and not adopted:
- A faster rank K for the first games after the summer break. It was worse.
- Pulling ranks towards the league level after the summer. It made no difference.
- Variations on the injury weighting: 5 or 20 recent matches, rating-weighted, or goalkeepers and attackers weighted more. They made no real difference.
- Blending in bookmaker odds. On the first 506 finished matches with odds (16–24 September 2026), the bookmakers alone scored best. Blending still didn't help: the best weight fitted on the first half gave the model 10%, and that blend scored slightly worse than the odds alone on the second half. Re-test once there are a few thousand matches. Scores, with the model recomputed as it stood before each match:

  | | Log loss | Brier | Favourite won |
  |---|---|---|---|
  | Bookmakers, closing odds | 0.9741 | 0.5800 | 52.4% |
  | Model with xG in the ranks (current) | 0.9874 | 0.5897 | 52.0% |
  | Model with goals-only ranks (before) | 0.9920 | 0.5928 | 51.6% |

  - Blending xG into the ranks cut the gap to the bookmakers from 0.018 to 0.013.
  - The 95% range for the current gap is 0.000 to 0.027, so the sample can't yet rule out the model matching the bookmakers.
  - Opening odds scored the same as closing odds (0.9742).
- **When the model and the bookmakers disagree** (checked 25 September 2026, the same 506 matches). On the 82 matches where the new model and the closing consensus differed by 10+ points on some outcome, the bookmakers were right. The model gave its side 42% on average, the bookmakers 30%, and it won 29%. Backing the model's side at the best closing price lost 2.6% (old model: −13%), with a 95% range of −38% to +41%. At 15+ points it showed +48%, but that's 19 bets, 7 winners and three long shots, which is noise. The model's side was the market underdog in 58 of the 82. Overall the model is slightly timid on strong favourites (said 84%, won 87% in the 80–90% band, 2024/25 onwards), but stretching the margin gained almost nothing (test W/D/L 0.99734 → 0.99728 at ×1.1), so it isn't used. Split by league: in the big five the model matched the bookmakers (−0.0013 log loss, 53 matches), and elsewhere it was 0.017 worse.
- **Every market with odds** (same 506 matches, new model reconstructed pre-match, 90-minute results). Log loss is model minus bookmakers, so negative means the model was better. The 10+ column covers matches where the model rated a selection 10+ points above the bookmaker consensus, with the model's, the bookmakers' and the actual rate, and the return at the best closing price. Value bets are the paper-betting rule: model × best price ≥ 1.03.

  | Market | Model − bookmakers (95%) | 10+ above market | Value bets |
  |---|---|---|---|
  | Match result | +0.0147 (+0.001 to +0.028) | 58: 48 / 34 / 36%, +17.6% (−28 to +71) | 359, −8.7% |
  | Double chance | +0.0079 (0.000 to +0.015) | 64: 64 / 50 / 53%, −0.4% | 263, −8.8% |
  | Both teams score | +0.0058 (−0.003 to +0.014) | 25: 45 / 33 / 32%, −5.3% | 240, −4.2% |
  | Over/under 0.5 | −0.0040 (−0.012 to +0.004) | none | 11, −89.5% |
  | Over/under 1.5 | +0.0034 (−0.003 to +0.010) | 1 | 233, −9.7% |
  | Over/under 2.5 | +0.0044 (−0.005 to +0.013) | 22: 46 / 34 / 36%, +5.2% | 301, −1.7% |
  | Over/under 3.5 | +0.0024 (−0.007 to +0.013) | 35: 58 / 46 / 54%, +5.2% | 251, +1.2% |
  | Over/under 4.5 | −0.0022 (−0.012 to +0.008) | 23: 73 / 62 / 70%, −1.7% | 175, −11.8% |

  - Nothing beats the bookmakers with confidence. Every range crosses zero or sits on the bookmakers' side.
  - The goal markets are much closer than the match result: within ±0.005, against 0.015.
  - The lines other than 2.5 use a calibration fitted on 2023/24 (base, shrink): 0.5 (0.96, 1.1), 1.5 (0.76, 0.9), 3.5 (0.36, 0.9), 4.5 (0.04, 1.0).
  - *Opening prices:* these matches' odds were first downloaded on 23 September, after most had been played, so `first_odd` equals the closing price for 99.9% of rows. Opening vs closing can only be tested on matches from 24 September on.

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

**Match-day job.** `.github/workflows/matchday.yml` runs `python -m matchvector matchday` every 30 minutes. For matches starting within 3 hours, it refreshes odds, which captures the closing price, and injury lists. It then re-projects those matches (downloading only their teams' and competitions' results), places the late bets, refreshes recent results and settles bets. It commits `docs/data/bets.json` only when bets change. The site's **Bets** tab shows the results.

A line-up adjustment (the strength of the starting XI against normal) was backtested and didn't help (test log loss 1.0058 against 1.0057), so line-ups aren't used in the projections.

## Website

`docs/index.html` is a single-page site in the same style as MatchLab. It has two tabs:
- **Matches:** projected scores, win/draw/loss chances and results. Filter by competition and day.
- **Rankings:** club rankings with rating, form and trend, sorted by rating (reliability is kept in the data but not shown). Tap a club to see its fixtures.

The site labels the club rank as an **Elo rating** (**Rating** = LT ALGO, **Form** = current rank, **Trend** = Form minus Rating, how far a club is playing above or below its long-term level). The numbers are the same as the club rank below, and on that scale 100 points is worth one goal a game.

**Club pages.** Club names link to `#/club/<team_id>`. The page has tabs:
- **Overview:** the next match, the squad on a pitch by the positions each player can play, recent form, the rank tiles and 12-month goal averages
- **Predicted XI:** the predicted line-up for the next match
- **Formations:** the manager, and the formations used since he took over and this season
- **Matches:** fixtures with projections, and every result with its rank change
- **Squad:** the club's ranked players by line
- **History:** the rank-over-time chart and season-by-season ranks

Each club's history comes from `docs/data/clubs/<team_id>.json`, which is written by `export_clubs` for clubs active in the last 400 days and loaded only when the page opens. Each match carries the formation from its line-up (`fixture_formations`; only the leagues with match-by-match player data have line-ups).

**League and country pages.** Wherever a club shows "Country · League" (the club page header, and Rankings rows when searching or showing all leagues), both parts are links:
- `#/league/<league_id>` has tabs: **Table** (this season's standings from `standings`, with each group, the zones API-Football describes, last-five form and each club's Rating), **Matches** (one round at a time, opening on the round with the next fixture, with the model's chances for upcoming games) and **Clubs** (the league's clubs by Rating, with their table position). Cups have no Table tab. The badge is the average Rating of the clubs playing in the league.
- `#/country/<country>` lists the country's leagues, strongest first by the average Rating of their clubs, then its cups, then all its league clubs by Rating. UEFA and FIFA competitions are under International (`#/country/World`).

Each competition's current season comes from `docs/data/leagues/<league_id>.json`, written by `export_leagues` and loaded only when the page opens (about 2 MB for all 70). A season starting from June onwards is labelled 2026/27, and a calendar-year one 2026, because API-Football's end dates only reach the last fixture it has scheduled.

**Managers.** Every night, each club in those leagues gets its manager from `/coachs?team=`, at most once a week each, or the next night after a line-up names a different coach (`team_coaches`, `python -m matchvector sync coaches`). The API keeps former managers listed with no end date, so the one on the club's latest line-up wins, otherwise the one who started last. Line-ups store their coach in `fixture_formations.coach_id`. API-Football seems to fill a season's line-ups with one coach, so they can't pin down a mid-season change, and the start date comes from `/coachs`.

**Kit colours.** The club page pitches are drawn in the club's home kit: the shirt and number colours from its latest home line-up (`team_colors`). New line-ups update them as they're fetched, and `python -m matchvector sync colors` fills in clubs that have none (it also runs nightly).

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
