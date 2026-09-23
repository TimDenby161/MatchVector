-- MatchVector schema for API-Football data (Supabase / Postgres).
-- Idempotent: safe to re-run.

create table if not exists leagues (
    league_id     int primary key,
    name          text not null,
    type          text,
    country       text,
    country_code  text,
    logo          text,
    updated_at    timestamptz not null default now()
);

create table if not exists league_seasons (
    league_id   int not null references leagues(league_id),
    season      int not null,
    start_date  date,
    end_date    date,
    is_current  boolean,
    coverage    jsonb,
    updated_at  timestamptz not null default now(),
    primary key (league_id, season)
);

create table if not exists venues (
    venue_id  int primary key,
    name      text,
    address   text,
    city      text,
    capacity  int,
    surface   text,
    image     text,
    updated_at timestamptz not null default now()
);

create table if not exists teams (
    team_id   int primary key,
    name      text not null,
    code      text,
    country   text,
    founded   int,
    national  boolean,
    logo      text,
    venue_id  int references venues(venue_id),
    updated_at timestamptz not null default now()
);

-- Which teams took part in which league season
create table if not exists team_seasons (
    team_id    int not null references teams(team_id),
    league_id  int not null references leagues(league_id),
    season     int not null,
    primary key (team_id, league_id, season)
);

create table if not exists fixtures (
    fixture_id     int primary key,
    league_id      int not null references leagues(league_id),
    season         int not null,
    round          text,
    kickoff        timestamptz,
    referee        text,
    venue_id       int,
    venue_name     text,
    venue_city     text,
    status_short   text,
    status_long    text,
    elapsed        int,
    home_team_id   int not null references teams(team_id),
    away_team_id   int not null references teams(team_id),
    home_goals     int,
    away_goals     int,
    ht_home        int,
    ht_away        int,
    ft_home        int,
    ft_away        int,
    et_home        int,
    et_away        int,
    pen_home       int,
    pen_away       int,
    home_winner    boolean,
    away_winner    boolean,
    stats_fetched_at timestamptz,
    updated_at     timestamptz not null default now()
);
create index if not exists fixtures_league_season_idx on fixtures (league_id, season);
create index if not exists fixtures_kickoff_idx on fixtures (kickoff);
create index if not exists fixtures_home_idx on fixtures (home_team_id);
create index if not exists fixtures_away_idx on fixtures (away_team_id);

-- One row per team per fixture
create table if not exists fixture_team_stats (
    fixture_id         int not null references fixtures(fixture_id) on delete cascade,
    team_id            int not null references teams(team_id),
    is_home            boolean,
    shots_on_goal      int,
    shots_off_goal     int,
    total_shots        int,
    blocked_shots      int,
    shots_inside_box   int,
    shots_outside_box  int,
    fouls              int,
    corners            int,
    offsides           int,
    possession_pct     numeric(5,2),
    yellow_cards       int,
    red_cards          int,
    goalkeeper_saves   int,
    passes_total       int,
    passes_accurate    int,
    passes_pct         numeric(5,2),
    expected_goals     numeric(6,2),
    goals_prevented    numeric(6,2),
    raw                jsonb,
    updated_at         timestamptz not null default now(),
    primary key (fixture_id, team_id)
);

create table if not exists standings (
    league_id       int not null references leagues(league_id),
    season          int not null,
    group_name      text not null,
    team_id         int not null references teams(team_id),
    rank            int,
    points          int,
    goal_diff       int,
    form            text,
    status          text,
    description     text,
    played          int,
    win             int,
    draw            int,
    lose            int,
    goals_for       int,
    goals_against   int,
    home_played     int,
    home_win        int,
    home_draw       int,
    home_lose       int,
    home_goals_for  int,
    home_goals_against int,
    away_played     int,
    away_win        int,
    away_draw       int,
    away_lose       int,
    away_goals_for  int,
    away_goals_against int,
    api_updated_at  timestamptz,
    updated_at      timestamptz not null default now(),
    primary key (league_id, season, group_name, team_id)
);

create table if not exists bookmakers (
    bookmaker_id  int primary key,
    name          text not null
);

create table if not exists bet_types (
    bet_id  int primary key,
    name    text not null
);

-- Latest pre-match price per fixture / bookmaker / market / selection.
-- API-Football only serves odds ~1-14 days before kickoff, so run the odds sync regularly.
create table if not exists odds (
    fixture_id      int not null,
    bookmaker_id    int not null references bookmakers(bookmaker_id),
    bet_id          int not null references bet_types(bet_id),
    selection       text not null,
    odd             numeric(10,3),
    api_updated_at  timestamptz,
    updated_at      timestamptz not null default now(),
    primary key (fixture_id, bookmaker_id, bet_id, selection)
);
create index if not exists odds_fixture_idx on odds (fixture_id);

-- Club ranking (see matchvector/ranking.py). Rebuilt from scratch on every run.
-- Every team starts from leagues.starting_rank of the first league it plays in.
alter table leagues add column if not exists starting_rank numeric;

create table if not exists team_rank_history (
    fixture_id   int not null,
    team_id      int not null,
    match_no     int not null,
    kickoff      timestamptz,
    is_home      boolean,
    opponent_id  int,
    rank_before  double precision,
    rank_after   double precision,
    exp_diff     double precision,
    act_diff     int,
    rank_change  double precision,
    primary key (fixture_id, team_id)
);
create index if not exists team_rank_history_team_idx on team_rank_history (team_id, match_no);

create table if not exists team_rankings (
    team_id        int primary key,
    league_id      int,
    starting_rank  double precision,
    played         int,
    last_match     timestamptz,
    current_rank   double precision,
    st_algo        double precision,
    rank_30        double precision,
    rank_100       double precision,
    lt_algo        double precision,
    hg             double precision,
    ha             double precision,
    ag             double precision,
    aa             double precision
);
alter table team_rankings add column if not exists rank_volatility double precision;
alter table team_rankings add column if not exists reliability double precision;

-- Projected score and win/draw/loss chances for upcoming fixtures (see
-- matchvector/predictions.py). Rebuilt nightly after the rankings.
create table if not exists fixture_predictions (
    fixture_id        int primary key,
    kickoff           timestamptz,
    league_id         int,
    home_team_id      int,
    away_team_id      int,
    home_rank         double precision,
    away_rank         double precision,
    exp_diff          double precision,   -- expected goal difference (home - away)
    home_xg           double precision,   -- projected home goals
    away_xg           double precision,   -- projected away goals
    p_home            double precision,
    p_draw            double precision,
    p_away            double precision,
    likely_score      text,               -- single most likely scoreline
    home_reliability  double precision,
    away_reliability  double precision,
    updated_at        timestamptz not null default now()
);

create or replace view upcoming_predictions as
select p.kickoff, l.name as competition, l.country, f.round,
       h.name as home_team, a.name as away_team,
       round(p.home_xg::numeric, 2) as home_goals, round(p.away_xg::numeric, 2) as away_goals,
       p.likely_score,
       round((100 * p.p_home)::numeric, 1) as home_win_pct,
       round((100 * p.p_draw)::numeric, 1) as draw_pct,
       round((100 * p.p_away)::numeric, 1) as away_win_pct,
       round(p.exp_diff::numeric, 2) as expected_margin,
       round(p.home_rank::numeric, 0) as home_rank, round(p.away_rank::numeric, 0) as away_rank,
       round(least(p.home_reliability, p.away_reliability)::numeric, 0) as reliability,
       p.fixture_id
from fixture_predictions p
join fixtures f using (fixture_id)
join leagues l on l.league_id = p.league_id
join teams h on h.team_id = p.home_team_id
join teams a on a.team_id = p.away_team_id
order by p.kickoff;

-- Supabase exposes the public schema through its REST API; enable RLS with no
-- policies so these tables are only reachable via the postgres/service role.
alter table leagues            enable row level security;
alter table league_seasons     enable row level security;
alter table venues             enable row level security;
alter table teams              enable row level security;
alter table team_seasons       enable row level security;
alter table fixtures           enable row level security;
alter table fixture_team_stats enable row level security;
alter table standings          enable row level security;
alter table bookmakers         enable row level security;
alter table bet_types          enable row level security;
alter table odds               enable row level security;
alter table team_rank_history  enable row level security;
alter table team_rankings      enable row level security;
alter table fixture_predictions enable row level security;
