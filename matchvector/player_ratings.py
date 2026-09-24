"""Player ranks (0-100) from per-match stats, and predicted-XI team ratings - all backdated.

Everything is replayed in kickoff order, so every number is what could have been known before
that match:

Player rank
    window   = the player's last WINDOW_APPS appearances within WINDOW_DAYS (any team)
    role     = his most common starting role in the window (from line-up grids, positions.py:
               LB, CB, DM, RW, ...); players only seen off the bench use their broad position
    metrics  = per-90 stats and ratios from the window (see WEIGHTS), each turned into a
               z-score against players in the same role group (GK, CB, FB, DM, CM, AM, W, ST;
               norms: player-seasons with 900+ minutes - a distribution, not any one player's future)
    score    = sum(weight * z) for the player's role group
    score    = (score x minutes + MINUTES_PRIOR x SHRINK_MINUTES) / (minutes + SHRINK_MINUTES)
               (few minutes -> pulled toward a below-average level, so they're marked down)
    stat pct = percentile of the score among regulars (900+ window minutes) in the same position
               across all matches (50 = an average regular in that position)
    club     = club LT ALGO going into each match, averaged over the window's matches by minutes (the clubs he
               actually played those matches for, as good as they were then)
    rank     = stat pct x sqrt(min(club / CLUB_RANK_MAX, 1)): even a perfect player is capped by his
               club's level, e.g. at a 966 club he can reach at most 100 x sqrt(966 / 1200) = 90

Season rank (player_season_ranks): every player follows the age curve through all his seasons,
and only leaves it where he has the minutes to (season_model)
    evidence = for each season he played: his clubs' LT ALGO over his matches (weighted by his
               minutes), moved by his stat score that season (percentile among 900+ minute
               seasons in his role group; the top decile spread up to the best on record):
               final_rank(pct, club), i.e. 100 x club / CLUB_RANK_MAX - offset + weight x (pct - 50)
    curve    = the age curve for his role group (the one he's played most minutes in), in rank
               points, from how evidence changes between seasons (ANCHOR_MINUTES+ in the first,
               CURVE_NEXT_MINUTES+ in the next). Growth to CURVE_GROWTH_TO is measured, then a flat
               prime until DECLINE_FROM (31 outfield, 33 keepers), then decline that speeds up
               every year: beta x years past DECLINE_FROM, beta fitted per group (strikers
               ~-0.33, keepers ~-0.29 but from 33, defensive mids ~-0.10). The regression to the
               mean in the measured changes (seasons picked for minutes tend to be good ones) is
               taken off. Below 18 it's set by hand: YOUNG_STEP_17 a year at 17,
               YOUNG_STEP_EXTRA more each year younger. 0 at the peak, so a player's level is
               his rank at peak age
    level    = where he sits on the curve: the minutes-weighted average of (evidence - curve)
               over all his seasons, each weighted LEVEL_DECAY ^ years apart, plus PRIOR_LEVEL
               weighted as PRIOR_MINUTES, so a player with little data anywhere sits low
    rank     = level + curve for that season, plus the season's own difference from it, kept in
               proportion minutes / (minutes + DEVIATION_MINUTES): a thin season (an injury year,
               the start of this season, a teenager's debut) stays on his curve, and a full season
               moves off it most of the way
    gaps     = every season from FILL_FROM_SEASON to now with no minutes in these leagues gets
               level + curve (stored with minutes = 0, shown as an estimate). His club that season
               comes from player_career_teams; one we have fixtures for but no player data (a
               lower league) adds its level as a little evidence (GAP_CLUB_MINUTES)
    retired  = players a current-club check (/players/squads) finds in no squad since their last
               season, aged RETIRED_AGE+,
               (retired_players, ingest.check_retired) get no current rank, so they leave the
               players list, and no estimated seasons after their last one

Match ratings everywhere are league-adjusted: each rating minus (that league's average rating
for the position - the average across all leagues), so a 7.0 is compared within its league.

Team ratings per fixture and team
    predicted XI  = 1 goalkeeper + 10 outfielders with the most minutes over the team's last
                    PREDICT_MATCHES matches, excluding anyone on the injury list for this match
    predicted     = average rank of the predicted XI
    recent        = average rank of the XIs actually started in the last PREDICT_MATCHES matches
    actual        = average rank of this match's starting XI (finished matches)
"""
import bisect
import io
import logging
import math
from collections import Counter, defaultdict, deque
from datetime import timedelta

from . import config
from .cache import WEEK, cached_rows, rank_history
from .positions import FALLBACK, group as role_group

log = logging.getLogger(__name__)

WINDOW_APPS = 20
WINDOW_DAYS = 540
SHRINK_MINUTES = 900
CLUB_RANK_MAX = 1200       # club rank treated as the top of the scale (rank is scaled by club / this)
MINUTES_PRIOR = -0.5       # score a player with no minutes is pulled toward (below an average regular)
PREDICT_MATCHES = 5
ANCHOR_MINUTES = 1500      # a season with this many minutes measures the age curve
FILL_FROM_SEASON = 2021    # every season from here to now gets a number (estimated where he has no minutes)
CURVE_MIN_PAIRS = 30       # an age needs this many season pairs to measure the curve there
CURVE_NEXT_MINUTES = 900   # ... and the season after needs this many (so players losing their place count)
CURVE_GROWTH_TO = 24       # the curve's growth is measured up to this age
DECLINE_FROM = {"GK": 33, "OUT": 31}   # flat prime until this age, then decline that speeds up
CURVE_FIT_TO = 38          # oldest age used to fit the decline
CURVE_PRIOR_PAIRS = 200    # an outfield group's decline leans on the pooled outfield one, weighted as this many pairs
YOUNG_STEP_17 = 4.0        # age curve below the measured ages (too few regulars): yearly gain at 17,
YOUNG_STEP_EXTRA = 2.0     # plus this for each year younger, in rank points
PRIOR_MINUTES = 450        # a player's level starts as PRIOR_LEVEL, weighted as this many minutes
PRIOR_LEVEL = {"GK": 55.0, "OUT": 55.0}   # rank at peak age of a player we know nothing about
LEVEL_DECAY = 0.7          # a season's weight in his level for another season, per year apart
DEVIATION_MINUTES = 1500   # a season keeps minutes / (minutes + this) of its difference from the curve
GAP_CLUB_MINUTES = 450     # weight of a gap season's club level (a lower league we have no player data for)

STATS = ("minutes", "rating_mins", "rated_mins", "goals", "assists", "shots_on", "key_passes",
         "passes", "passes_accurate", "tackles", "interceptions", "blocks", "duels", "duels_won",
         "dribbles_won", "fouls_committed", "yellow_cards", "red_cards", "saves", "goals_conceded")

# weight per metric by role group; negative weight = lower is better
# Weights per role group. Tested against results (2021-26, club rank as the baseline): match
# rating added nothing, while shots on target, key passes, passing volume and duels won did, and
# goals / assists / save % beyond those mostly reflected luck that evened out. So rating is a
# small part everywhere, the lasting stats carry the weight, and goals still count for attackers.
WEIGHTS = {
    # keepers on match rating alone, corrected for workload (gk_rating, see metrics): goals
    # conceded mostly measures the defence in front of him (Trafford went 41 -> 96 moving from a
    # relegated Premier League side to the Championship's best defence) and save % is mostly luck
    "GK": {"gk_rating": 1.0},
    "CB": {"rating": .15, "duels_pct": .20, "passes": .18, "pass_acc": .10, "tackles_int": .12,
           "blocks": .05, "goals": .05, "shots_on": .05, "discipline": -.10},
    "FB": {"rating": .12, "key_passes": .18, "passes": .15, "duels_pct": .12, "tackles_int": .10,
           "dribbles_won": .08, "assists": .08, "shots_on": .05, "pass_acc": .05, "discipline": -.07},
    "DM": {"rating": .12, "passes": .20, "duels_pct": .15, "tackles_int": .15, "key_passes": .12,
           "pass_acc": .10, "shots_on": .05, "blocks": .03, "discipline": -.08},
    "CM": {"rating": .12, "key_passes": .20, "passes": .15, "shots_on": .12, "duels_pct": .10,
           "goals": .08, "tackles_int": .07, "assists": .06, "dribbles_won": .05, "pass_acc": .05},
    "AM": {"rating": .12, "key_passes": .22, "shots_on": .18, "goals": .12, "dribbles_won": .10,
           "assists": .08, "passes": .08, "duels_pct": .07, "pass_acc": .03},
    "W": {"rating": .12, "shots_on": .20, "key_passes": .20, "goals": .15, "dribbles_won": .10,
          "assists": .08, "passes": .06, "duels_pct": .06, "discipline": -.03},
    "ST": {"rating": .10, "shots_on": .28, "goals": .25, "key_passes": .10, "duels_pct": .10,
           "assists": .07, "passes": .05, "dribbles_won": .05},
}


# Keeper ratings rise with workload: busy keepers earn rating points for saves. Raya was 7.21 at
# Brentford (4.1 saves per 90) and 6.86 at Arsenal (1.4). A keeper's rating is compared with what
# his workload would give: GK_SAVES_ADJ per save per 90 away from GK_SAVES_MEAN. 0.10 made ratings
# most consistent for keepers who changed club (Raya 7.09 -> 7.01); stronger overshoots.
GK_SAVES_ADJ = 0.10
GK_SAVES_MEAN = 2.9


def metrics(s):
    """Metric values from summed stats s (dict)."""
    m = s["minutes"]
    if m <= 0:
        return None
    p90 = lambda k: s[k] * 90 / m
    ratio = lambda a, b: s[a] / s[b] if s[b] else None
    rating = s["rating_mins"] / s["rated_mins"] if s["rated_mins"] else None
    return {
        "rating": rating,
        "gk_rating": rating - GK_SAVES_ADJ * (p90("saves") - GK_SAVES_MEAN) if rating is not None else None,
        "goals": p90("goals"), "assists": p90("assists"), "shots_on": p90("shots_on"),
        "key_passes": p90("key_passes"), "dribbles_won": p90("dribbles_won"),
        "tackles_int": (s["tackles"] + s["interceptions"]) * 90 / m, "blocks": p90("blocks"),
        "passes": p90("passes"), "pass_acc": ratio("passes_accurate", "passes"),
        "duels_pct": ratio("duels_won", "duels"),
        "discipline": (s["fouls_committed"] + 3 * s["yellow_cards"] + 6 * s["red_cards"]) * 90 / m,
        "save_pct": s["saves"] / (s["saves"] + s["goals_conceded"]) if s["saves"] + s["goals_conceded"] else None,
        "conceded": p90("goals_conceded"),
    }


def _row_stats(r):
    """Stat dict for one appearance row from fixture_players."""
    d = dict(zip(STATS[3:], (x or 0 for x in r["stats"])))
    d["minutes"] = r["minutes"]
    d["rating_mins"] = (r["rating"] or 0) * r["minutes"] if r["rating"] else 0
    d["rated_mins"] = r["minutes"] if r["rating"] else 0
    return d


class Window:
    """A player's last WINDOW_APPS appearances with running sums."""
    __slots__ = ("apps", "sums", "roles", "broad")

    def __init__(self):
        self.apps = deque()                # (kickoff, stats dict, role, broad position)
        self.sums = dict.fromkeys(STATS + ("club_mins",), 0.0)   # club_mins: club rank x minutes
        self.roles = Counter()             # starting roles (LB, DM, ...)
        self.broad = Counter()             # API positions (G/D/M/F)

    def add(self, kickoff, stats, role, broad):
        self.apps.append((kickoff, stats, role, broad))
        for k, v in stats.items():
            self.sums[k] += v
        if role:
            self.roles[role] += 1
        self.broad[broad] += 1
        while len(self.apps) > WINDOW_APPS:
            self._pop()

    def expire(self, now):
        while self.apps and self.apps[0][0] < now - timedelta(days=WINDOW_DAYS):
            self._pop()

    def _pop(self):
        _, stats, role, broad = self.apps.popleft()
        for k, v in stats.items():
            self.sums[k] -= v
        if role:
            self.roles[role] -= 1
        self.broad[broad] -= 1

    def label(self):
        """Most common starting role, else the fallback group for his broad position."""
        roles = +self.roles
        if roles:
            return roles.most_common(1)[0][0]
        broad = +self.broad
        return FALLBACK.get(broad.most_common(1)[0][0]) if broad else None

    def position(self):
        """Role group used for comparison (GK, CB, FB, DM, CM, AM, W, ST)."""
        return role_group(self.label()) if self.apps else None


def _rating_offsets(conn):
    """Temp table rating_offsets(league_id, position, off): how far each league's average match
    rating for a position (G/D/M/F) sits above the average across all leagues."""
    conn.execute("create temp table if not exists rating_offsets (league_id int, position text, off double precision)")
    if conn.execute("select count(*) from rating_offsets").fetchone()[0]:
        return
    conn.execute("""insert into rating_offsets
                    select league_id, position, lr - pr from (
                        select f.league_id, fp.position, sum(fp.rating * fp.minutes) / sum(fp.minutes) as lr,
                               sum(sum(fp.rating * fp.minutes)) over (partition by fp.position)
                                 / sum(sum(fp.minutes)) over (partition by fp.position) as pr
                        from fixture_players fp join fixtures f using (fixture_id)
                        where fp.rating is not null and fp.minutes > 0
                        group by f.league_id, fp.position) t""")


def _appearances(conn):
    """Every appearance, from the local cache (cache.py), in fixture order:
    (fixture_id, team, player, minutes, started, position, role, rating, season, league_id,
    status, *STATS[3:]), rating not yet league-adjusted (see _adjusted)."""
    fcols = ", ".join(f"fp.{c}" for c in STATS[3:])
    return cached_rows(conn, "appearances", f"""
        select {WEEK.format('f.kickoff')} as part, fp.fixture_id, fp.team_id, fp.player_id,
               fp.minutes, fp.started, fp.position, fp.role, fp.rating::float8 as rating, f.season,
               f.league_id, f.status_short, {fcols}
        from fixture_players fp join fixtures f using (fixture_id)""",
        order_by="fixture_id, team_id, player_id")


def _offsets(conn):
    """{(league_id, position): rating offset} (see _rating_offsets)."""
    _rating_offsets(conn)
    return {(lg, pos): off for lg, pos, off in conn.execute(
        "select league_id, position, off from rating_offsets")}


def _adjusted(row, offsets):
    """League-adjusted match rating of an appearance row, or None if unrated."""
    return None if row[7] is None else row[7] - offsets.get((row[9], row[5]), 0.0)


def _add_season(entry, row, rating):
    """Add one appearance row to a player-season's running sums, roles and positions."""
    mins = row[3] or 0
    sums = entry["sums"]
    sums["minutes"] += mins
    if rating is not None:
        sums["rating_mins"] += rating * mins
        sums["rated_mins"] += mins
    for k, v in zip(STATS[3:], row[11:]):
        sums[k] += v or 0
    if row[6]:
        entry["roles"][row[6]] += mins
    entry["broad"][row[5]] += mins


def _norms(apps, offsets):
    """{position: {metric: (mean, sd)}} from player-seasons with 900+ minutes."""
    seasons = defaultdict(lambda: {"sums": dict.fromkeys(STATS, 0.0), "roles": Counter(), "broad": Counter()})
    for row in apps:
        _add_season(seasons[(row[2], row[8])], row, _adjusted(row, offsets))
    groups = defaultdict(list)
    for entry in seasons.values():
        if entry["sums"]["minutes"] < 900:
            continue
        pos = (role_group(entry["roles"].most_common(1)[0][0]) if entry["roles"]
               else FALLBACK.get(entry["broad"].most_common(1)[0][0]))
        m = metrics(entry["sums"])
        if pos in WEIGHTS and m:
            groups[pos].append(m)
    norms = {}
    for pos, rows in groups.items():
        norms[pos] = {}
        for k in WEIGHTS[pos]:
            vals = [r[k] for r in rows if r[k] is not None]
            mean = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1
            norms[pos][k] = (mean, sd)
    return norms


GK_CLUB_OFFSET = 10        # keeper rank = 100 x club / CLUB_RANK_MAX - this + GK_RATING_WEIGHT x (pct - 50)
GK_RATING_WEIGHT = 0.2
GK_FULL_SHARE = 0.8        # keepers playing less than this share of their club's minutes are scaled down, up to 20% (backups)


def keeper_rank(pct, club, share=None):
    """Keepers: a keeper's rating percentile is mostly noise (season ratings repeat ~0.3, and
    regular Premier League keepers all sit within 6.8-7.05), so stretching it over the whole
    scale gave near-random ranks. The rank leans on club level instead - good clubs sign good
    keepers - and the rating moves it by up to about +/-10. A keeper who plays less than
    GK_FULL_SHARE of his club's minutes (a backup) is scaled down by up to 20%, so a No. 2 at a
    top club isn't rated as elite; it's a share of the games so far, so early season is fine."""
    r = 100 * min(club / CLUB_RANK_MAX, 1) - GK_CLUB_OFFSET + GK_RATING_WEIGHT * (pct - 50)
    if share is not None:
        r *= min(1.0, 0.8 + 0.2 * share / GK_FULL_SHARE)
    return min(max(r, 0), 100)


OUT_CLUB_OFFSET = 12       # outfield rank = 100 x club / CLUB_RANK_MAX - this + OUT_STATS_WEIGHT x (pct - 50)
OUT_STATS_WEIGHT = 0.3     # stats move an outfield player up to about +/-15 (keepers +/-10: noisier)
OUT_FULL_SHARE = 0.7       # outfield players under this share of club minutes are scaled down, up to 20%
ELITE_FROM = 90            # outfield stats above this percentile earn ELITE_WEIGHT more per percentile,
ELITE_WEIGHT = 1.0         # up to +10: great players at clubs below the very top can reach the high 90s


def outfield_rank(pct, club, share=None):
    """Outfield players, like keepers, start from club level - a regular for a strong club is
    good evidence of quality - and their stats move them up or down. Elite stats (above
    ELITE_FROM) earn extra, so the club's level doesn't cap a great player (Messi at PSG, ~1040:
    89 -> 95). A player under OUT_FULL_SHARE of his club's minutes (squad player) is scaled
    down by up to 20%."""
    r = (100 * min(club / CLUB_RANK_MAX, 1) - OUT_CLUB_OFFSET + OUT_STATS_WEIGHT * (pct - 50)
         + ELITE_WEIGHT * max(0.0, pct - ELITE_FROM))
    if share is not None:
        r *= min(1.0, 0.8 + 0.2 * share / OUT_FULL_SHARE)
    return min(max(r, 0), 100)


def final_rank(pct, club, pos, share=None):
    return keeper_rank(pct, club, share) if pos == "GK" else outfield_rank(pct, club, share)


def stretched_pct(score, ref):
    """Percentile of score in sorted ref, except the top decile, which is spread by how far the
    score is above the 90th percentile (90 there, 100 at the 99.9th), so the best stand apart
    instead of all sitting at ~99."""
    n = len(ref)
    p = 100 * bisect.bisect_left(ref, score) / n
    if p < 90:
        return p
    lo, hi = ref[int(0.9 * n)], ref[min(int(0.999 * n), n - 1)]
    return 90 + 10 * min(max((score - lo) / (hi - lo), 0), 1) if hi > lo else p


def club_factor(club):
    """Multiplier for club level: sqrt(club / CLUB_RANK_MAX), capped at 1. The square root keeps
    the club's level in the rank but halves the gaps (Real Madrid 1063 vs Bayern 1121: 0.94 vs
    0.97 rather than 0.89 vs 0.93)."""
    return math.sqrt(min(club / CLUB_RANK_MAX, 1))


def _stat_score(sums, pos, norms):
    m = metrics(sums)
    if not m or pos not in norms:
        return None
    return sum(w * (m[k] - norms[pos][k][0]) / norms[pos][k][1]
               for k, w in WEIGHTS[pos].items() if m[k] is not None)


def _season_ranks(conn, norms, apps, offsets, team_rank, retired):
    """[(player, season, rank, minutes, gap-season club or None)] (see module docstring).
    team_rank: {(fixture, team): LT ALGO going into the match}."""
    q = lambda sql, p=None: conn.execute(sql, p).fetchall()
    return season_model(
        norms, apps, offsets, team_rank,
        born=q("select player_id, birth_date from players where birth_date is not null"),
        team_level=q("""select h.team_id, f.season, avg(h.lt_before), count(*) from team_rank_history h
                        join fixtures f using (fixture_id) group by 1, 2"""),
        careers=q("select player_id, season, team_id from player_career_teams where season > 0"),
        covered=q("select distinct fp.team_id, f.season from fixture_players fp join fixtures f using (fixture_id)"),
        retired=retired)


def season_model(norms, apps, offsets, team_rank, born, team_level, careers, covered, retired=None,
                 detail=None):
    """Season ranks: every player follows the age curve through all his seasons, from a level
    of his own, and only leaves it where he has the minutes to (see module docstring). Query
    results come in as rows so this can be run offline. retired: {player: last season}, no
    estimates after it (retired_players). detail: a dict to fill with the
    workings per (player, season): (evidence, weight, level, curve), for checking."""
    seasons = defaultdict(lambda: {"sums": dict.fromkeys(STATS, 0.0), "roles": Counter(), "broad": Counter(),
                                   "club": [0.0, 0.0]})
    for row in apps:
        if row[10] not in ("FT", "AET", "PEN"):
            continue
        e = seasons[(row[2], row[8])]
        _add_season(e, row, _adjusted(row, offsets))
        club = team_rank.get((row[0], row[1]))
        if club is not None:
            e["club"][0] += club * (row[3] or 0)
            e["club"][1] += row[3] or 0
    born = dict(born)

    # 1. Each season's evidence: his clubs' LT ALGO over his matches, moved by his stat score
    #    that season (percentile among 900+ minute seasons in his role group)
    raw = {}                   # (player, season) -> (stat score, minutes, role group, club)
    for (player, season), e in seasons.items():
        mins = e["sums"]["minutes"]
        if mins <= 0 or not e["club"][1]:
            continue
        pos = (role_group(e["roles"].most_common(1)[0][0]) if +e["roles"]
               else FALLBACK.get((+e["broad"]).most_common(1)[0][0]) if +e["broad"] else None)
        sc = _stat_score(e["sums"], pos, norms)
        if sc is not None:
            raw[(player, season)] = (sc, mins, pos, e["club"][0] / e["club"][1])
    ref = defaultdict(list)
    for sc, mins, pos, _ in raw.values():
        if mins >= 900:
            ref[pos].append(sc)
    for v in ref.values():
        v.sort()

    def pct(score, pos):
        return stretched_pct(score, ref[pos]) if ref.get(pos) else 50.0
    evidence = {k: (final_rank(pct(sc, pos), club, pos), mins, pos)
                for k, (sc, mins, pos, club) in raw.items()}

    # 2. Age curve (rank points), per role group, from how evidence changes from one season to
    #    the next (ANCHOR_MINUTES+ in the first, CURVE_NEXT_MINUTES+ in the next, so players
    #    losing their place still count). Seasons picked for their minutes tend to be good ones,
    #    so the next is worse on average at every age (regression to the mean): measured as the
    #    constant in the decline fit and taken off everything. The shape:
    #    - growth to CURVE_GROWTH_TO: average change at each age (outfield pooled, keepers apart)
    #    - a flat prime until DECLINE_FROM (31 outfield, 33 keepers)
    #    - then decline that speeds up every year: change = beta x (age - DECLINE_FROM), beta
    #      fitted per group (strikers fastest); outfield groups lean on the pooled outfield
    #      beta, weighted as CURVE_PRIOR_PAIRS pairs past DECLINE_FROM
    def age(player, season):             # age at the start of the season (1 July)
        b = born.get(player)
        return None if b is None else season - b.year - ((b.month, b.day) > (7, 1))
    young = {"GK": defaultdict(list), "OUT": defaultdict(list)}
    old = defaultdict(list)              # group -> [(age, change)]
    for (player, season), (ev, mins, pos) in evidence.items():
        nxt = evidence.get((player, season + 1))
        a = age(player, season)
        if a is None or mins < ANCHOR_MINUTES or not nxt or nxt[1] < CURVE_NEXT_MINUTES:
            continue
        if a <= CURVE_GROWTH_TO:
            young["GK" if pos == "GK" else "OUT"][max(a, 17)].append(nxt[0] - ev)
        elif a <= CURVE_FIT_TO:
            old[pos].append((a, nxt[0] - ev))
            if pos != "GK":
                old["OUT"].append((a, nxt[0] - ev))

    def hinge(pairs, start, const=None):  # change = const + beta x max(0, age - start)
        xs = [max(0, a - start) for a, _ in pairs]
        ys = [d for _, d in pairs]
        if const is None:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sum((x - mx) ** 2 for x in xs) or 1)
            return my - beta * mx, beta
        return const, sum(x * (y - const) for x, y in zip(xs, ys)) / (sum(x * x for x in xs) or 1)
    bias, beta = {}, {}
    for kind in ("OUT", "GK"):
        bias[kind], beta[kind] = hinge(old[kind], DECLINE_FROM[kind])
    for g, pairs in old.items():
        if g in ("OUT", "GK"):
            continue
        n = sum(1 for a, _ in pairs if a > DECLINE_FROM["OUT"])
        _, b = hinge(pairs, DECLINE_FROM["OUT"], bias["OUT"])
        beta[g] = (b * n + beta["OUT"] * CURVE_PRIOR_PAIRS) / (n + CURVE_PRIOR_PAIRS)
    growth = {}
    for kind, by_age in young.items():
        # smoothed over neighbouring ages (one age's average is noisy), less the bias, never below 0
        ages = {a for a in by_age if len(by_age[a]) >= CURVE_MIN_PAIRS}
        growth[kind] = {a: max(0.0, sum(x for b in (a - 1, a, a + 1) if b in ages for x in by_age[b])
                               / sum(len(by_age[b]) for b in (a - 1, a, a + 1) if b in ages) - bias[kind])
                        for a in ages}
    log.info("Age curve: growth to %d %s; regression to the mean %s; decline after %s: "
             "beta x years past it %s", CURVE_GROWTH_TO,
             {k: {a: round(v, 1) for a, v in sorted(c.items())} for k, c in growth.items()},
             {k: round(v, 2) for k, v in bias.items()}, DECLINE_FROM,
             {g: round(v, 3) for g, v in beta.items()})

    def step(a, g):                      # typical change from age a to a + 1
        kind = "GK" if g == "GK" else "OUT"
        c = growth[kind] or growth["OUT"]
        if a > CURVE_GROWTH_TO:          # flat prime, then decline
            return beta.get(g, beta["OUT"]) * max(0, min(a, 40) - DECLINE_FROM[kind])
        if a < 18:                       # too few regulars this young to measure: teenagers
            return max(c[min(c)], YOUNG_STEP_17 + YOUNG_STEP_EXTRA * (17 - a))   # develop fast
        if not c:
            return 0.0
        return c.get(min(max(a, min(c)), max(c)), 0.0)

    # curve position by age, 0 at the peak, so a player's level is his rank at peak age
    table = {}
    for g in beta:
        cum, t = 0.0, {}
        for a in range(14, 46):
            t[a] = cum
            cum += step(a, g)
        top = max(t.values())
        table[g] = {a: v - top for a, v in t.items()}

    def curve(player, season, g):
        a = age(player, season)
        return 0.0 if a is None else table.get(g, table["OUT"])[min(max(a, 14), 45)]

    # 3. Seasons to rate: every season with minutes, and every season from FILL_FROM_SEASON to
    #    now (estimated) for anyone with at least one. A gap season at a club we have fixtures
    #    for but no player data (a lower league) takes the club's level as a little evidence
    team_level = {(t, y): (float(r), n) for t, y, r, n in team_level}
    career = defaultdict(list)
    for p, y, t in careers:
        career[(p, y)].append(t)
    covered = set(covered)
    by_player = defaultdict(dict)        # player -> {season: (evidence, weight, pos, gap club)}
    for (player, season), (ev, mins, pos) in evidence.items():
        by_player[player][season] = (ev, mins, pos, None)
    last_season = max(y for _, y in evidence)
    for player, have in by_player.items():
        real = sorted(have)
        end = (retired or {}).get(player, last_season)   # retired: nothing after his last season
        for season in range(min(FILL_FROM_SEASON, real[0] + 1), end + 1):
            if season in have:
                continue
            pos = have[min(real, key=lambda y: abs(y - season))][2]
            known = [(team_level[(t, season)][1], t) for t in career.get((player, season), [])
                     if (t, season) in team_level]
            team = max(known)[1] if known else 0
            if team and (team, season) not in covered:
                have[season] = (final_rank(50, team_level[(team, season)][0], pos), GAP_CLUB_MINUTES, pos, team)
            else:                        # no club data, or a club we track where he didn't play
                have[season] = (None, 0, pos, team)

    # 4. His level off the curve: minutes-weighted average of (evidence - curve) over all his
    #    seasons (weighted LEVEL_DECAY ^ years apart), plus PRIOR_MINUTES of a below-average
    #    level, so a player with little data sits low
    # 5. Season rank = level + curve, plus the season's own difference from that, kept in
    #    proportion minutes / (minutes + DEVIATION_MINUTES): thin seasons stay on the curve
    rows = []
    for player, have in by_player.items():
        mins_in = Counter()              # his role group: the one he's played most minutes in
        for _, w, pos, team in have.values():
            if team is None:
                mins_in[pos] += w
        g = mins_in.most_common(1)[0][0]
        kind = "GK" if g == "GK" else "OUT"
        off = {y: (ev - curve(player, y, g), w) for y, (ev, w, _, _) in have.items() if w > 0}
        for season, (ev, w, pos, team) in have.items():
            c = curve(player, season, g)
            wsum = PRIOR_MINUTES
            level = PRIOR_LEVEL[kind] * PRIOR_MINUTES
            for y, (o, wy) in off.items():
                k = wy * LEVEL_DECAY ** abs(y - season)
                level += o * k
                wsum += k
            level /= wsum
            r = level + c
            if ev is not None and team is None:
                r += (ev - r) * w / (w + DEVIATION_MINUTES)
            if detail is not None:
                detail[(player, season)] = (ev, w, level, c)
            rows.append((player, season, round(min(max(r, 0), 100), 1), 0 if team is not None else int(w),
                         team or None))
    return rows


RETIRED_AGE = 34           # a player in no club's squad counts as retired from this age (younger ones
                           # are usually free agents or late transfers; they leave the list anyway once
                           # their last match is WINDOW_DAYS old)


def retired_players(conn, apps):
    """{player: last season} for players we know have retired: a current-club check made after
    his last season here (ingest.check_retired) found him in no club's squad, aged RETIRED_AGE+."""
    last = {}
    for row in apps:
        if row[3]:
            last[row[2]] = max(last.get(row[2], row[8]), row[8])
    return {p: last[p] for p, checked, age in conn.execute(
                """select c.player_id, c.season, extract(year from age(p.birth_date))
                   from player_career_checks c left join players p using (player_id)
                   where c.team_id is null""")
            if p in last and checked > last[p] and (age or 0) >= RETIRED_AGE}


def compute_player_ratings(conn):
    appearances = _appearances(conn)
    retired = retired_players(conn, appearances)
    offsets = _offsets(conn)
    norms = _norms(appearances, offsets)
    team_rank = {(r[0], r[1]): r[8] for r in rank_history(conn)}
    ranks = list(team_rank.values())
    tr_mean = sum(ranks) / len(ranks)

    fixtures = conn.execute(
        """select fixture_id, kickoff, home_team_id, away_team_id, status_short in ('NS', 'TBD'), season
           from fixtures where league_id = any(%s)
             and (status_short in ('FT', 'AET', 'PEN') or (status_short in ('NS', 'TBD') and kickoff > now()))
           order by kickoff, fixture_id""", [config.INJURY_MODEL_LEAGUES]).fetchall()
    apps = defaultdict(list)
    for r in appearances:
        apps[r[0]].append({"team": r[1], "player": r[2], "minutes": r[3], "started": r[4],
                           "position": r[5], "role": r[6], "rating": _adjusted(r, offsets),
                           "stats": r[11:]})
    injured = defaultdict(set)
    for fid, team, player in cached_rows(conn, "injuries", f"""
            select {WEEK.format('f.kickoff')} as part, i.fixture_id, i.team_id, i.player_id
            from injuries i join fixtures f using (fixture_id)""",
            order_by="fixture_id, team_id, player_id"):
        injured[(fid, team)].add(player)

    windows = defaultdict(Window)
    team_recent = defaultdict(lambda: deque(maxlen=PREDICT_MATCHES))     # team -> [{player: minutes}]

    def raw_score(player, now):
        w = windows.get(player)
        if w is None:
            return None, None, 0
        w.expire(now)
        pos = w.position()
        m = metrics(w.sums) if w.apps else None
        if not m or pos not in norms:
            return None, pos, 0
        score = 0.0
        for k, weight in WEIGHTS[pos].items():
            if m[k] is not None:
                mean, sd = norms[pos][k]
                score += weight * (m[k] - mean) / sd
        minutes = w.sums["minutes"]
        score = (score * minutes + MINUTES_PRIOR * SHRINK_MINUTES) / (minutes + SHRINK_MINUTES)
        return (score, w.sums["club_mins"] / minutes), pos, minutes     # (stat score, club rank)

    # One replay collects raw scores; ranks are scaled afterwards by the spread among regulars
    sample = defaultdict(list) # position -> raw scores of players with a full window of minutes
    appearance_scores = []     # (fixture, player, (stat score, club rank), position)
    team_rows = []             # (fixture, team, predicted XI [(player, pos, raw)], actual XI [(raw, pos)], upcoming)
    for fid, kickoff, home, away, upcoming, _season in fixtures:
        for team in (home, away):
            # predicted XI from recent matches, excluding the injury list
            minutes = Counter()
            for g in team_recent[team]:
                minutes.update(g)
            out = injured.get((fid, team), set())
            candidates = [(p, m) for p, m in minutes.most_common() if p not in out]
            scored = []
            for p, m in candidates:
                s, pos, _ = raw_score(p, kickoff)
                if s is not None:
                    scored.append((p, pos, s, m))
            keepers = [x for x in scored if x[1] == "GK"][:1]
            outfield = [x for x in scored if x[1] != "GK"][:10]
            predicted = keepers + outfield
            actual = []
            for a in apps.get(fid, []):
                if a["team"] != team:
                    continue
                s, pos, mins = raw_score(a["player"], kickoff)
                if s is not None:
                    appearance_scores.append((fid, a["player"], s, pos))
                    if mins >= SHRINK_MINUTES:
                        sample[pos].append(s[0])
                    if a["started"]:
                        actual.append((s, pos))
            team_rows.append((fid, team, [(p, pos, s, windows[p].label()) for p, pos, s, _ in predicted],
                              actual, upcoming))
        # after the match: update windows and team history
        for a in apps.get(fid, []):
            st = _row_stats(a)
            st["club_mins"] = team_rank.get((fid, a["team"]), tr_mean) * (a["minutes"] or 0)
            windows[a["player"]].add(kickoff, st, a["role"], a["position"])
        for team in (home, away):
            played = {a["player"]: a["minutes"] for a in apps.get(fid, []) if a["team"] == team}
            if played:
                team_recent[team].append(played)

    cdf = {pos: sorted(v) for pos, v in sample.items()}
    pooled = sorted(x for v in sample.values() for x in v)

    def to_rank(s, pos):
        """(stat score, club rank) -> stat percentile scaled by the club's level."""
        score, club = s
        ref = cdf.get(pos) or pooled
        return round(final_rank(stretched_pct(score, ref), club, pos), 1)
    log.info("Player ratings: %d appearances, %d team-fixtures, regulars per position %s, %d retired",
             len(appearance_scores), len(team_rows), {p: len(v) for p, v in cdf.items()}, len(retired))

    # Team ratings (recent = mean of the team's previous actual XI ratings)
    team_out, lineups = [], []
    xi_hist = defaultdict(lambda: deque(maxlen=PREDICT_MATCHES))
    for fid, team, predicted, actual, upcoming in team_rows:
        pred = [to_rank(s, pos) for _, pos, s, _ in predicted]
        act = [to_rank(s, pos) for s, pos in actual]
        recent = sum(xi_hist[team]) / len(xi_hist[team]) if xi_hist[team] else None
        team_out.append((fid, team, sum(pred) / len(pred) if pred else None, len(pred), recent,
                         sum(act) / len(act) if act else None))
        if act:
            xi_hist[team].append(sum(act) / len(act))
        if upcoming:
            lineups.extend((fid, team, p, label, to_rank(s, pos)) for p, pos, s, label in predicted)

    # Current rank per player: latest window
    current = []
    for player in list(windows):
        if player in retired:            # off the players list
            continue
        s, pos, minutes = raw_score(player, fixtures[-1][1] if fixtures else None)
        if s is not None:
            current.append((player, to_rank(s, pos), windows[player].label(), int(minutes)))

    season_rows = _season_ranks(conn, norms, appearances, offsets, team_rank, retired)
    _write(conn, appearance_scores, to_rank, team_out, lineups, current, season_rows)


def _write(conn, appearance_scores, to_rank, team_out, lineups, current, season_rows):
    with conn.cursor() as cur:
        # Rebuilt with truncate + copy rather than updating fixture_players, so the big table
        # isn't rewritten (and bloated with dead rows) on every run
        cur.execute("truncate fixture_player_ranks")
        buf = io.StringIO()
        for fid, player, s, pos in appearance_scores:
            buf.write(f"{fid}\t{player}\t{to_rank(s, pos)}\n")
        with cur.copy("copy fixture_player_ranks (fixture_id, player_id, player_rank) from stdin") as cp:
            cp.write(buf.getvalue())
        cur.execute("truncate fixture_team_ratings")
        buf = io.StringIO()
        for row in team_out:
            buf.write("\t".join(r"\N" if v is None else str(round(v, 2) if isinstance(v, float) else v)
                                for v in row) + "\n")
        with cur.copy("copy fixture_team_ratings (fixture_id, team_id, predicted_xi_rating, predicted_xi_size, "
                      "recent_xi_rating, actual_xi_rating) from stdin") as cp:
            cp.write(buf.getvalue())
        cur.execute("truncate predicted_lineups")
        cur.executemany("insert into predicted_lineups (fixture_id, team_id, player_id, position, player_rank) "
                        "values (%s, %s, %s, %s, %s)", lineups)
        cur.execute("update players set current_rank = null, rank_position = null, rank_minutes = null")
        cur.execute("create temp table tmp_cur (player_id int, r numeric(4,1), pos text, mins int) on commit drop")
        buf = io.StringIO()
        for p, r, pos, mins in current:
            buf.write(f"{p}\t{r}\t{pos}\t{mins}\n")
        with cur.copy("copy tmp_cur from stdin") as cp:
            cp.write(buf.getvalue())
        cur.execute("""update players pl set current_rank = t.r, rank_position = t.pos, rank_minutes = t.mins
                       from tmp_cur t where pl.player_id = t.player_id""")
        cur.execute("truncate player_season_ranks")
        with cur.copy("copy player_season_ranks (player_id, season, season_rank, minutes, team_id) from stdin") as cp:
            cp.write("".join(f"{p}\t{y}\t{r}\t{m}\t{t if t else chr(92) + 'N'}\n" for p, y, r, m, t in season_rows))
    conn.commit()
    log.info("Player ratings written: %d current player ranks, %d predicted-lineup rows, %d player-seasons",
             len(current), len(lineups), len(season_rows))
