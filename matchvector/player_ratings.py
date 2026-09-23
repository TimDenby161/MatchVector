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
               + TEAM_WEIGHT * club strength, where club strength = z of the club rank at the
                 time, averaged over the window's matches by minutes (the clubs he actually
                 played those matches for, as good as they were then). TEAM_WEIGHT = 1 makes
                 club level count about as much as his own stats (sd 0.59 vs 0.67 among regulars),
                 so players at weak clubs are marked down.
    score    = (score x minutes + MINUTES_PRIOR x SHRINK_MINUTES) / (minutes + SHRINK_MINUTES)
               (few minutes -> pulled toward a below-average level, so they're marked down)
    rank     = percentile of the score among regulars (900+ window minutes) in the same position
               across all matches, so 50 = an average regular in that position, 90 = better than
               90% of them

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
TEAM_WEIGHT = 1.0
MINUTES_PRIOR = -0.5       # score a player with no minutes is pulled toward (below an average regular)
PREDICT_MATCHES = 5

STATS = ("minutes", "rating_mins", "rated_mins", "goals", "assists", "shots_on", "key_passes",
         "passes", "passes_accurate", "tackles", "interceptions", "blocks", "duels", "duels_won",
         "dribbles_won", "fouls_committed", "yellow_cards", "red_cards", "saves", "goals_conceded")

# weight per metric by role group; negative weight = lower is better
WEIGHTS = {
    "GK": {"rating": .50, "save_pct": .30, "conceded": -.20},
    "CB": {"rating": .40, "duels_pct": .15, "tackles_int": .12, "blocks": .08, "pass_acc": .08,
           "passes": .05, "goals": .03, "discipline": -.07},
    "FB": {"rating": .35, "tackles_int": .12, "key_passes": .10, "assists": .08, "duels_pct": .08,
           "dribbles_won": .07, "passes": .07, "pass_acc": .05, "discipline": -.05},
    "DM": {"rating": .35, "tackles_int": .20, "passes": .12, "pass_acc": .10, "duels_pct": .10,
           "key_passes": .05, "blocks": .03, "discipline": -.05},
    "CM": {"rating": .35, "key_passes": .12, "passes": .10, "tackles_int": .10, "pass_acc": .08,
           "assists": .08, "goals": .07, "dribbles_won": .05, "duels_pct": .05},
    "AM": {"rating": .35, "key_passes": .15, "assists": .12, "goals": .12, "dribbles_won": .10,
           "shots_on": .08, "duels_pct": .05, "pass_acc": .03},
    "W": {"rating": .35, "goals": .12, "assists": .12, "key_passes": .12, "dribbles_won": .12,
          "shots_on": .08, "duels_pct": .04, "discipline": -.03},
    "ST": {"rating": .35, "goals": .25, "shots_on": .12, "assists": .08, "duels_pct": .07,
           "key_passes": .06, "dribbles_won": .04, "discipline": -.03},
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
        self.sums = dict.fromkeys(STATS + ("club_mins",), 0.0)   # club_mins: club strength x minutes
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


def compute_player_ratings(conn):
    norms = _norms(conn)
    team_rank = {(f, t): r for f, t, r in conn.execute(
        "select fixture_id, team_id, rank_before from team_rank_history")}
    ranks = list(team_rank.values())
    tr_mean = sum(ranks) / len(ranks)
    tr_sd = math.sqrt(sum((r - tr_mean) ** 2 for r in ranks) / len(ranks))

    fixtures = conn.execute(
        """select fixture_id, kickoff, home_team_id, away_team_id, status_short in ('NS', 'TBD')
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
        score += TEAM_WEIGHT * w.sums["club_mins"] / minutes
        return (score * minutes + MINUTES_PRIOR * SHRINK_MINUTES) / (minutes + SHRINK_MINUTES), pos, minutes

    # One replay collects raw scores; ranks are scaled afterwards by the spread among regulars
    sample = defaultdict(list) # position -> raw scores of players with a full window of minutes
    appearance_scores = []     # (fixture, player, raw score, position)
    team_rows = []             # (fixture, team, predicted XI [(player, pos, raw)], actual XI [(raw, pos)], upcoming)
    for fid, kickoff, home, away, upcoming in fixtures:
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
                        sample[pos].append(s)
                    if a["started"]:
                        actual.append((s, pos))
            team_rows.append((fid, team, [(p, pos, s, windows[p].label()) for p, pos, s, _ in predicted],
                              actual, upcoming))
        # after the match: update windows and team history
        for a in apps.get(fid, []):
            st = _row_stats(a)
            club_z = (team_rank.get((fid, a["team"]), tr_mean) - tr_mean) / tr_sd
            st["club_mins"] = club_z * (a["minutes"] or 0)
            windows[a["player"]].add(kickoff, st, a["role"], a["position"])
        for team in (home, away):
            played = {a["player"]: a["minutes"] for a in apps.get(fid, []) if a["team"] == team}
            if played:
                team_recent[team].append(played)

    cdf = {pos: sorted(v) for pos, v in sample.items()}
    pooled = sorted(x for v in sample.values() for x in v)

    def to_rank(s, pos):
        ref = cdf.get(pos) or pooled
        return round(100 * bisect.bisect_left(ref, s) / len(ref), 1)
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

    _write(conn, appearance_scores, to_rank, team_out, lineups, current)


def _write(conn, appearance_scores, to_rank, team_out, lineups, current):
    with conn.cursor() as cur:
        cur.execute("create temp table tmp_rank (fixture_id int, player_id int, player_rank numeric(4,1)) on commit drop")
        buf = io.StringIO()
        for fid, player, s, pos in appearance_scores:
            buf.write(f"{fid}\t{player}\t{to_rank(s, pos)}\n")
        with cur.copy("copy tmp_rank from stdin") as cp:
            cp.write(buf.getvalue())
        cur.execute("""update fixture_players fp set player_rank = t.player_rank from tmp_rank t
                       where fp.fixture_id = t.fixture_id and fp.player_id = t.player_id
                         and fp.player_rank is distinct from t.player_rank""")
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
    conn.commit()
    log.info("Player ratings written: %d current player ranks, %d predicted-lineup rows",
             len(current), len(lineups))
