-- Requires model_versions. Existing player tables/history are untouched.
CREATE TABLE IF NOT EXISTS player_rating_captures (
    capture_id bigserial PRIMARY KEY,
    capture_date date NOT NULL UNIQUE,
    captured_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    model_version_id text NOT NULL REFERENCES model_versions(model_version_id) ON DELETE RESTRICT,
    season integer NOT NULL,
    population integer NOT NULL CHECK(population>=0),
    CHECK(capture_date=(captured_at AT TIME ZONE 'UTC')::date)
);
CREATE TABLE IF NOT EXISTS player_rating_history (
    capture_id bigint NOT NULL REFERENCES player_rating_captures(capture_id) ON DELETE RESTRICT,
    player_id integer NOT NULL,
    rating numeric(4,1) NOT NULL,
    world_rank integer NOT NULL,
    position text,
    rating_group text,
    team_id integer,
    team_source text NOT NULL,
    window_minutes integer,
    season_minutes integer,
    rating_source text NOT NULL,
    PRIMARY KEY(capture_id,player_id)
);
CREATE INDEX IF NOT EXISTS player_rating_history_player_idx ON player_rating_history(player_id,capture_id);
CREATE OR REPLACE FUNCTION guard_player_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Player rating history is append only';
    END IF;
    IF TG_TABLE_NAME='player_rating_captures' THEN
        NEW.created_at:=clock_timestamp();
        IF NOT EXISTS(SELECT 1 FROM model_versions WHERE model_version_id=NEW.model_version_id AND model_type='player') THEN
            RAISE EXCEPTION 'Player captures require a player model version';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['player_rating_captures','player_rating_history'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS player_history_guard ON %I',t);
        EXECUTE format('CREATE TRIGGER player_history_guard BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION guard_player_history()',t);
        EXECUTE format('DROP TRIGGER IF EXISTS player_history_truncate_guard ON %I',t);
        EXECUTE format('CREATE TRIGGER player_history_truncate_guard BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION guard_player_history()',t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
    END LOOP;
END; $$;
CREATE OR REPLACE VIEW player_rating_snapshot_history AS
SELECT h.*, c.captured_at,c.created_at,c.capture_date,c.model_version_id,c.season,c.population
FROM player_rating_history h JOIN player_rating_captures c USING(capture_id);
-- Positive rating change = improved rating. Positive rank movement = moved up.
-- Baselines use actual stored observations at/before the requested horizon.
CREATE OR REPLACE VIEW player_rating_movement AS
WITH latest AS (
    SELECT DISTINCT ON(player_id) * FROM player_rating_snapshot_history
    ORDER BY player_id,captured_at DESC,capture_id DESC
)
SELECT n.player_id,n.captured_at,n.rating,n.world_rank,n.model_version_id,n.season,
       horizon.label AS horizon, b.captured_at AS baseline_at,b.model_version_id AS baseline_model_version_id,
       n.rating-b.rating AS rating_change,b.world_rank-n.world_rank AS rank_movement,
       CASE WHEN b.capture_id IS NOT NULL THEN n.model_version_id IS DISTINCT FROM b.model_version_id END AS model_changed,
       n.population AS population,b.population AS baseline_population
FROM latest n CROSS JOIN (VALUES ('7d',7),('30d',30),('90d',90),('season',0)) horizon(label,days)
LEFT JOIN LATERAL (
    SELECT p.* FROM player_rating_snapshot_history p
    WHERE p.player_id=n.player_id AND
      ((horizon.days>0 AND p.captured_at<=n.captured_at-make_interval(days=>horizon.days))
       OR (horizon.days=0 AND p.season=n.season AND p.captured_at<n.captured_at))
    ORDER BY CASE WHEN horizon.days=0 THEN p.captured_at END ASC,
             CASE WHEN horizon.days>0 THEN p.captured_at END DESC,p.capture_id DESC
    LIMIT 1
) b ON true;

REVOKE ALL ON player_rating_snapshot_history,player_rating_movement FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
    FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
            EXECUTE format('REVOKE ALL ON player_rating_snapshot_history,player_rating_movement FROM %I',r);
        END IF;
    END LOOP;
END; $$;
