"""Rate each finished fixture's projection from 1 (terrible) to 5 (excellent).

A port of MatchLab's grading (AutoModel/docs/index.html, predictionFactorMetrics and
scoreAccuracyTier), rescaled to 1-5: MatchLab's 0 ("Miles off") becomes 1.

Five factors, each scored 0-5, then weighted:
    Winner 30%, Margin 25%, Clean sheets 20%, Shape 15%, Goals 10%
rating = round(weighted), clamped to 1-5.
"""
import logging
import math

log = logging.getLogger(__name__)

WEIGHTS = {"winner": 0.30, "margin": 0.25, "clean_sheets": 0.20, "shape": 0.15, "goals": 0.10}
LABELS = {1: "Terrible", 2: "Poor", 3: "Decent", 4: "Very good", 5: "Excellent"}
CLEAN_SHEET_CALL = 0.4   # predicted clean sheet when its Poisson chance is at least this


def _by_error(error, excellent, good, decent, loose, poor):
    for score, limit in ((5, excellent), (4, good), (3, decent), (2, loose), (1, poor)):
        if error <= limit:
            return score
    return 0


def _result(gd):
    return "home" if gd > 0 else "away" if gd < 0 else "draw"


def _projected_result(gd):
    return "draw" if abs(gd) <= 0.25 else _result(gd)


def _goal_band(total):
    return 0 if total < 2 else 1 if total < 3 else 2 if total < 4 else 3


def _goal_band_score(projected, actual):
    if _goal_band(projected) == _goal_band(actual):
        return _by_error(abs(projected - actual), 0.5, 1, 1.5, 2.5, 3.5)
    if projected >= 3 and actual >= 4:
        return 4
    if projected >= 2.5 and actual >= 3:
        return 3
    if projected < 2 and actual < 3:
        return 3
    return 1


def factor_scores(proj_h, proj_a, act_h, act_a):
    """Dict of the five factor scores (0-5) for one match."""
    act_gd, proj_gd = act_h - act_a, proj_h - proj_a
    act_total, proj_total = act_h + act_a, proj_h + proj_a

    goals = max(_by_error(abs(act_total - proj_total), 0.5, 1, 1.5, 2.5, 3.5),
                _goal_band_score(proj_total, act_total))

    actual, projected = _result(act_gd), _projected_result(proj_gd)
    if actual == projected:
        winner = 5
    elif actual == "draw" and abs(proj_gd) <= 0.6:
        winner = 3
    elif "draw" in (actual, projected) and abs(act_gd) <= 1 and abs(proj_gd) <= 0.25:
        winner = 2
    elif actual != "draw" and projected != "draw":
        winner = 0 if (abs(proj_gd) >= 1 or abs(act_gd) >= 2 or abs(act_gd - proj_gd) >= 2.5) else 1
    else:
        winner = 1

    gd_error = abs(act_gd - proj_gd)
    max_team_error = max(abs(act_h - proj_h), abs(act_a - proj_a))
    act_close, proj_close = abs(act_gd) <= 1, abs(proj_gd) <= 1
    margin = _by_error(gd_error, 0.5, 1, 1.5, 2.5, 3.5)
    if actual == projected and act_close == proj_close:
        margin = max(margin, 3)

    if act_close == proj_close:
        if actual == projected:
            shape = 5 if max_team_error <= 0.5 and gd_error <= 0.5 else 4
        elif not act_close and not proj_close:
            shape = 1
        else:
            shape = 4 if max_team_error <= 1.5 else 3 if max_team_error <= 3 else 2
    else:
        shape = 3 if gd_error <= 1.5 else 2 if gd_error <= 2.5 else 1

    home_cs_p, away_cs_p = math.exp(-proj_a), math.exp(-proj_h)
    home_cs_call, away_cs_call = home_cs_p >= CLEAN_SHEET_CALL, away_cs_p >= CLEAN_SHEET_CALL
    home_cs, away_cs = act_a == 0, act_h == 0
    hits = (home_cs_call == home_cs) + (away_cs_call == away_cs)
    confidence = ((home_cs_p if home_cs else 1 - home_cs_p)
                  + (away_cs_p if away_cs else 1 - away_cs_p)) / 2
    if hits < 2:
        cs = 2 if hits == 1 else 0
    elif (home_cs_call or away_cs_call) == (home_cs or away_cs) and (home_cs or away_cs):
        cs = 5
    else:
        cs = next(s for s, lim in ((5, 0.85), (4, 0.7), (3, 0.55), (2, 0.5), (1, -1)) if confidence >= lim)
    most_conceded = max(act_h, act_a)
    cs = min(cs, 3 if most_conceded >= 4 else 4 if most_conceded >= 3 else 5)

    return {"winner": winner, "margin": margin, "clean_sheets": cs, "shape": shape, "goals": goals}


def overall(scores):
    # Summed in MatchLab's order so floating-point ties (e.g. 3.4999...) round the same way
    weighted = 0.0
    for k in ("goals", "winner", "margin", "shape", "clean_sheets"):
        weighted += WEIGHTS[k] * scores[k]
    return max(1, min(5, math.floor(weighted + 0.5)))   # Math.round: half up


def rate_fixtures(conn):
    """Rate finished fixtures with a projection: new ones, plus the last 14 days again in case
    API-Football corrected a score."""
    rows = conn.execute(
        """select p.fixture_id, p.home_xg, p.away_xg, f.home_goals, f.away_goals
           from fixture_predictions p join fixtures f using (fixture_id)
           where (p.rating is null or f.kickoff >= now() - interval '14 days')
             and f.status_short in ('FT', 'AET', 'PEN')
             and f.home_goals is not null and p.home_xg is not null""").fetchall()
    updates = []
    for fid, ph, pa, hg, ag in rows:
        s = factor_scores(ph, pa, hg, ag)
        updates.append((overall(s), s["winner"], s["margin"], s["clean_sheets"], s["shape"],
                        s["goals"], fid))
    with conn.cursor() as cur:
        cur.executemany(
            """update fixture_predictions set rating = %s, rating_winner = %s, rating_margin = %s,
               rating_clean_sheets = %s, rating_shape = %s, rating_goals = %s
               where fixture_id = %s""", updates)
    conn.commit()
    log.info("Rated %d finished fixtures", len(updates))
