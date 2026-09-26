-- Requires model_versions. No existing lineups/injuries are modified.
CREATE TABLE IF NOT EXISTS lineup_prediction_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    fixture_id integer NOT NULL,
    team_id integer NOT NULL,
    model_version_id text NOT NULL REFERENCES model_versions(model_version_id) ON DELETE RESTRICT,
    source text NOT NULL CHECK(source IN ('prospective','late_observation')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    captured_at timestamptz NOT NULL,
    effective_at timestamptz NOT NULL,
    seconds_to_kickoff double precision NOT NULL,
    players jsonb NOT NULL CHECK(jsonb_typeof(players)='array'),
    selection_inputs jsonb NOT NULL CHECK(jsonb_typeof(selection_inputs)='object'),
    availability jsonb NOT NULL CHECK(jsonb_typeof(availability)='object'),
    content_hash text NOT NULL,
    CHECK(source <> 'prospective' OR (captured_at < effective_at AND created_at < effective_at)),
    UNIQUE(fixture_id,team_id,model_version_id,source,content_hash)
);
CREATE INDEX IF NOT EXISTS lineup_snapshot_fixture_idx ON lineup_prediction_snapshots(fixture_id,team_id,captured_at);
CREATE TABLE IF NOT EXISTS official_lineup_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    fixture_id integer NOT NULL,
    team_id integer NOT NULL,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    captured_at timestamptz NOT NULL,
    effective_at timestamptz NOT NULL,
    formation text,
    players jsonb NOT NULL CHECK(jsonb_typeof(players)='array'),
    content_hash text NOT NULL,
    UNIQUE(fixture_id,team_id,source,content_hash)
);
CREATE INDEX IF NOT EXISTS official_lineup_fixture_idx ON official_lineup_snapshots(fixture_id,team_id,captured_at);
CREATE OR REPLACE FUNCTION guard_lineup_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Lineup evidence is append only';
    END IF;
    NEW.created_at := clock_timestamp();
    IF TG_TABLE_NAME='lineup_prediction_snapshots' THEN
        IF NOT EXISTS (SELECT 1 FROM model_versions WHERE model_version_id=NEW.model_version_id AND model_type='lineup') THEN
            RAISE EXCEPTION 'Lineup predictions require a lineup model version';
        END IF;
        IF NEW.captured_at>=NEW.effective_at OR NEW.created_at>=NEW.effective_at THEN
            NEW.source := 'late_observation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lineup_prediction_guard ON lineup_prediction_snapshots;
CREATE TRIGGER lineup_prediction_guard BEFORE INSERT OR UPDATE OR DELETE ON lineup_prediction_snapshots
FOR EACH ROW EXECUTE FUNCTION guard_lineup_snapshot();
DROP TRIGGER IF EXISTS lineup_prediction_truncate_guard ON lineup_prediction_snapshots;
CREATE TRIGGER lineup_prediction_truncate_guard BEFORE TRUNCATE ON lineup_prediction_snapshots
FOR EACH STATEMENT EXECUTE FUNCTION guard_lineup_snapshot();
DROP TRIGGER IF EXISTS official_lineup_guard ON official_lineup_snapshots;
CREATE TRIGGER official_lineup_guard BEFORE INSERT OR UPDATE OR DELETE ON official_lineup_snapshots
FOR EACH ROW EXECUTE FUNCTION guard_lineup_snapshot();
DROP TRIGGER IF EXISTS official_lineup_truncate_guard ON official_lineup_snapshots;
CREATE TRIGGER official_lineup_truncate_guard BEFORE TRUNCATE ON official_lineup_snapshots
FOR EACH STATEMENT EXECUTE FUNCTION guard_lineup_snapshot();
ALTER TABLE lineup_prediction_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE official_lineup_snapshots ENABLE ROW LEVEL SECURITY;
