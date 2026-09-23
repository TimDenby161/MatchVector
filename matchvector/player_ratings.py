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
    club     = club rank at the time, averaged over the window's matches by minutes (the clubs he
               actually played those matches for, as good as they were then)
    rank     = stat pct x sqrt(min(club / CLUB_RANK_MAX, 1)): even a perfect player is capped by his
               club's level, e.g. at a 966 club he can reach at most 100 x sqrt(966 / 1200) = 90

Season rank (player_season_ranks), from that season's own matches
    score    = the same stat score from his season totals, blended with
               - his previous season's score, weighted PRIOR_SEASON_MINUTES x (1 - club games /
                 FULL_SEASON_GAMES): early in a season it leans on last season, and by the end it
                 stands on its own (no previous season: an average regular, 0)
               - for the rest of the weight (SHRINK_MINUTES x club games / FULL_SEASON_GAMES): an
                 estimate of where he'd be with more data - his nearest season with ANCHOR_MINUTES+
                 walked through the typical age curve (average season-to-season change in score by
                 age, measured from players with ANCHOR_MINUTES+ in both seasons). So a 15-year-old's
                 debut season sits a normal amount below his first full season. With no such
                 season anywhere, MINUTES_PRIOR, so a thin season is marked down
    pct      = percentile among player-seasons with 900+ minutes in the same role group, except
               the top decile, which is spread by how far the score is above the 90th percentile
               (90 at the 90th percentile score, 100 at the 99.9th), so the best seasons stand
               apart instead of all sitting at ~99
    rank     = pct x club_factor(club), club = his clubs' average rank over his matches

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
from .positions import FALLBACK, group as role_group

log = logging.getLogger(__name__)

WINDOW_APPS = 20
WINDOW_DAYS = 540
SHRINK_MINUTES = 900
CLUB_RANK_MAX = 1200       # club rank treated as the top of the scale (rank is scaled by club / this)
MINUTES_PRIOR = -0.5       # score a player with no minutes is pulled toward (below an average regular)
PREDICT_MATCHES = 5
FULL_SEASON_GAMES = 34     # a full league season, for scaling the minutes pull on season ranks
PRIOR_SEASON_MINUTES = 1350  # weight of last season's score at the start of a season (15 full games)
ANCHOR_MINUTES = 1500      # a season with this many minutes is measured well enough to estimate others from

STATS = ("minutes", "rating_mins", "rated_mins", "goals", "assists", "shots_on", "key_passes",
         "passes", "passes_accurate", "tackles", "interceptions", "blocks", "duels", "duels_won",
         "dribbles_won", "fouls_committed", "yellow_cards", "red_cards", "saves", "goals_conceded")

# weight per metric by role group; negative weight = lower is better
# Weights per role group. Tested against results (2021-26, club rank as the baseline): match
# rating added nothing, while shots on target, key passes, passing volume and duels won did, and
# goals / assists / save % beyond those mostly reflected luck that evened out. So rating is a
# small part everywhere, the lasting stats carry the weight, and goals still count for attackers.
WEIGHTS = {
    # no lasting keeper signal showed up, so keepers stay mostly on rating and shot-stopping
    "GK": {"rating": .40, "save_pct": .30, "conceded": -.30},
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


def metrics(s):
    """Metric values from summed stats s (dict)."""
    m = s["minutes"]
    if m <= 0:
        return None
    p90 = lambda k: s[k] * 90 / m
    ratio = lambda a, b: s[a] / s[b] if s[b] else None
    return {
        "rating": s["rating_mins"] / s["rated_mins"] if s["rated_mins"] else None,
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


def _norms(conn):
    """{position: {metric: (mean, sd)}} from player-seasons with 900+ minutes."""
    cols = ", ".join(f"sum(coalesce(fp.{c}, 0))" for c in STATS[3:])
    seasons = defaultdict(lambda: {"sums": dict.fromkeys(STATS, 0.0), "roles": Counter(), "broad": Counter()})
    for row in conn.execute(
            f"""select fp.player_id, f.season, fp.role, fp.position, sum(fp.minutes),
                       sum(case when fp.rating is not null then fp.rating * fp.minutes else 0 end),
                       sum(case when fp.rating is not null then fp.minutes else 0 end), {cols}
                from fixture_players fp join fixtures f using (fixture_id)
                group by fp.player_id, f.season, fp.role, fp.position"""):
        entry = seasons[(row[0], row[1])]
        for k, v in zip(STATS, row[4:]):
            entry["sums"][k] += float(v)
        if row[2]:
            entry["roles"][row[2]] += float(row[4])
        entry["broad"][row[3]] += float(row[4])
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


def _season_ranks(conn, norms):
    """[(player, season, rank, minutes)] from each season's own matches (see module docstring)."""
    cols = ", ".join(f"sum(coalesce(fp.{c}, 0))" for c in STATS[3:])
    seasons = defaultdict(lambda: {"sums": dict.fromkeys(STATS, 0.0), "roles": Counter(), "broad": Counter(),
                                   "club": [0.0, 0.0], "games": 0})
    team_games = {(t, y): n for t, y, n in conn.execute(
        """select team_id, season, count(*) from (
               select home_team_id as team_id, season from fixtures
               where league_id = any(%(l)s) and status_short in ('FT', 'AET', 'PEN')
               union all
               select away_team_id, season from fixtures
               where league_id = any(%(l)s) and status_short in ('FT', 'AET', 'PEN')) g
           group by 1, 2""", {"l": config.INJURY_MODEL_LEAGUES})}
    born = dict(conn.execute("select player_id, birth_date from players where birth_date is not null"))
    for row in conn.execute(
            f"""select fp.player_id, f.season, fp.team_id, fp.role, fp.position, sum(fp.minutes),
                       sum(case when fp.rating is not null then fp.rating * fp.minutes else 0 end),
                       sum(case when fp.rating is not null then fp.minutes else 0 end), {cols},
                       sum(h.rank_before * fp.minutes),
                       sum(case when h.rank_before is not null then fp.minutes else 0 end)
                from fixture_players fp join fixtures f using (fixture_id)
                left join team_rank_history h on h.fixture_id = fp.fixture_id and h.team_id = fp.team_id
                where f.status_short in ('FT', 'AET', 'PEN')
                group by 1, 2, 3, 4, 5"""):
        e = seasons[(row[0], row[1])]
        for k, v in zip(STATS, row[5:5 + len(STATS)]):
            e["sums"][k] += float(v)
        mins = float(row[5])
        if row[3]:
            e["roles"][row[3]] += mins
        e["broad"][row[4]] += mins
        e["club"][0] += float(row[-2] or 0)
        e["club"][1] += float(row[-1] or 0)
        e["games"] = max(e["games"], team_games.get((row[2], row[1]), 0))
    # Raw season scores, then the age curve from well-measured consecutive seasons
    raw = {}
    for (player, season), e in seasons.items():
        mins = e["sums"]["minutes"]
        if mins <= 0:
            continue
        pos = (role_group(e["roles"].most_common(1)[0][0]) if +e["roles"]
               else FALLBACK.get((+e["broad"]).most_common(1)[0][0]) if +e["broad"] else None)
        sc = _stat_score(e["sums"], pos, norms)
        if sc is not None:
            raw[(player, season)] = (sc, mins, pos)

    def age(player, season):             # age at the start of the season (1 July)
        b = born.get(player)
        return None if b is None else season - b.year - ((b.month, b.day) > (7, 1))
    steps = defaultdict(list)
    for (player, season), (sc, mins, _) in raw.items():
        nxt = raw.get((player, season + 1))
        a = age(player, season)
        if a is not None and mins >= ANCHOR_MINUTES and nxt and nxt[1] >= ANCHOR_MINUTES:
            steps[min(max(a, 18), 36)].append(nxt[0] - sc)
    curve = {a: sum(v) / len(v) for a, v in steps.items() if len(v) >= 30}

    def step(a):                         # typical change in score from age a to a + 1
        if a is None or not curve:
            return 0.0
        a = min(max(a, min(curve)), max(curve))
        return curve.get(a, 0.0)
    anchors = defaultdict(list)
    for (player, season), (sc, mins, _) in raw.items():
        if mins >= ANCHOR_MINUTES:
            anchors[player].append(season)

    def estimate(player, season):
        """Score from his nearest well-measured season, moved through the age curve."""
        near = [y for y in anchors.get(player, []) if y != season]
        if not near:
            return None
        y = min(near, key=lambda x: (abs(x - season), x < season))   # nearest; ties: the later one
        est = raw[(player, y)][0]
        for t in range(season, y):       # anchor later: take off the growth between
            est -= step(age(player, t))
        for t in range(y, season):       # anchor earlier: add it on
            est += step(age(player, t))
        return est

    scored = []
    ref = defaultdict(list)
    final = {}                 # (player, season) -> blended score, for the next season's prior
    for (player, season), e in sorted(seasons.items(), key=lambda kv: kv[0][1]):
        if (player, season) not in raw:
            continue
        s, mins, pos = raw[(player, season)]
        done = min(e["games"] / FULL_SEASON_GAMES, 1)
        prev = final.get((player, season - 1), final.get((player, season - 2), 0.0))
        w_prev = PRIOR_SEASON_MINUTES * (1 - done)
        w_low = SHRINK_MINUTES * done
        est = estimate(player, season)
        low = MINUTES_PRIOR if est is None else est
        s = (s * mins + prev * w_prev + low * w_low) / (mins + w_prev + w_low)
        final[(player, season)] = s
        club = e["club"][0] / e["club"][1] if e["club"][1] else None
        scored.append((player, season, s, pos, club, int(mins)))
        if mins >= 900:
            ref[pos].append(s)
    for v in ref.values():
        v.sort()

    def pct(score, ref):
        n = len(ref)
        p = 100 * bisect.bisect_left(ref, score) / n
        if p < 90:
            return p
        lo, hi = ref[int(0.9 * n)], ref[min(int(0.999 * n), n - 1)]
        return 90 + 10 * min(max((score - lo) / (hi - lo), 0), 1) if hi > lo else p

    log.info("Age curve (score change per year by age): %s",
             {a: round(v, 3) for a, v in sorted(curve.items())})
    rows = []
    for player, season, s, pos, club, mins in scored:
        if ref.get(pos) and club:
            rows.append((player, season, round(pct(s, ref[pos]) * club_factor(club), 1), mins))
    return rows


def compute_player_ratings(conn):
    norms = _norms(conn)
    team_rank = {(f, t): r for f, t, r in conn.execute(
        "select fixture_id, team_id, rank_before from team_rank_history")}
    ranks = list(team_rank.values())
    tr_mean = sum(ranks) / len(ranks)

    fixtures = conn.execute(
        """select fixture_id, kickoff, home_team_id, away_team_id, status_short in ('NS', 'TBD'), season
           from fixtures where league_id = any(%s)
             and (status_short in ('FT', 'AET', 'PEN') or (status_short in ('NS', 'TBD') and kickoff > now()))
           order by kickoff, fixture_id""", [config.INJURY_MODEL_LEAGUES]).fetchall()
    apps = defaultdict(list)
    cols = ", ".join(STATS[3:])
    for r in conn.execute(f"""select fixture_id, team_id, player_id, minutes, started, position, role, rating,
                                     {cols} from fixture_players"""):
        apps[r[0]].append({"team": r[1], "player": r[2], "minutes": r[3], "started": r[4],
                           "position": r[5], "role": r[6], "rating": float(r[7]) if r[7] is not None else None,
                           "stats": r[8:]})
    injured = defaultdict(set)
    for fid, team, player in conn.execute("select fixture_id, team_id, player_id from injuries"):
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
        return round(100 * bisect.bisect_left(ref, score) / len(ref) * club_factor(club), 1)
    log.info("Player ratings: %d appearances, %d team-fixtures, regulars per position %s",
             len(appearance_scores), len(team_rows), {p: len(v) for p, v in cdf.items()})

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
        s, pos, minutes = raw_score(player, fixtures[-1][1] if fixtures else None)
        if s is not None:
            current.append((player, to_rank(s, pos), windows[player].label(), int(minutes)))

    season_rows = _season_ranks(conn, norms)
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
        with cur.copy("copy player_season_ranks (player_id, season, season_rank, minutes) from stdin") as cp:
            cp.write("".join(f"{p}\t{y}\t{r}\t{m}\n" for p, y, r, m in season_rows))
    conn.commit()
    log.info("Player ratings written: %d current player ranks, %d predicted-lineup rows, %d player-seasons",
             len(current), len(lineups), len(season_rows))
