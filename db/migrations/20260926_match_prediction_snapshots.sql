-- Requires 20260926_model_versions.sql. Existing prediction rows are untouched.
CREATE TABLE IF NOT EXISTS match_prediction_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    fixture_id integer NOT NULL,
    league_id integer NOT NULL,
    home_team_id integer NOT NULL,
    away_team_id integer NOT NULL,
    model_version_id text NOT NULL REFERENCES model_versions(model_version_id) ON DELETE RESTRICT,
    source text NOT NULL CHECK (source IN ('prospective','reconstruction','late_observation')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    captured_at timestamptz NOT NULL,
    effective_at timestamptz NOT NULL, -- kickoff known at capture time
    model_reference_at timestamptz NOT NULL, -- clock used for rank blend/history window
    seconds_to_kickoff double precision NOT NULL,
    p_home double precision NOT NULL CHECK (p_home BETWEEN 0 AND 1),
    p_draw double precision NOT NULL CHECK (p_draw BETWEEN 0 AND 1),
    p_away double precision NOT NULL CHECK (p_away BETWEEN 0 AND 1),
    home_xg double precision NOT NULL CHECK (home_xg >= 0),
    away_xg double precision NOT NULL CHECK (away_xg >= 0),
    exp_diff double precision NOT NULL,
    likely_score text,
    p_over25 double precision NOT NULL,
    p_btts double precision NOT NULL,
    inputs jsonb NOT NULL CHECK (jsonb_typeof(inputs)='object'),
    content_hash text NOT NULL,
    CHECK (abs(p_home+p_draw+p_away-1) < 0.000001),
    CHECK (source <> 'prospective' OR (captured_at < effective_at AND created_at < effective_at)),
    UNIQUE(fixture_id, model_version_id, source, content_hash)
);
CREATE INDEX IF NOT EXISTS match_snapshots_fixture_capture_idx ON match_prediction_snapshots(fixture_id,captured_at);
CREATE OR REPLACE FUNCTION guard_match_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Match prediction snapshots are append only';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM model_versions WHERE model_version_id=NEW.model_version_id AND model_type='match') THEN
        RAISE EXCEPTION 'Match snapshots require a match model version';
    END IF;
    NEW.created_at := clock_timestamp();
    IF NEW.source='prospective' AND (NEW.captured_at >= NEW.effective_at OR NEW.created_at >= NEW.effective_at) THEN
        NEW.source := 'late_observation';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS match_snapshots_guard ON match_prediction_snapshots;
CREATE TRIGGER match_snapshots_guard BEFORE INSERT OR UPDATE OR DELETE ON match_prediction_snapshots
FOR EACH ROW EXECUTE FUNCTION guard_match_snapshot();
DROP TRIGGER IF EXISTS match_snapshots_no_truncate ON match_prediction_snapshots;
CREATE TRIGGER match_snapshots_no_truncate BEFORE TRUNCATE ON match_prediction_snapshots
FOR EACH STATEMENT EXECUTE FUNCTION guard_match_snapshot();
ALTER TABLE match_prediction_snapshots ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE match_prediction_snapshots IS 'Append-only predictions. Only source=prospective is genuine pre-event capture; reconstructions and late observations must be excluded from prospective evaluation.';
