-- Additive and repeatable. No existing model output/history is modified.
CREATE TABLE IF NOT EXISTS model_versions (
    model_version_id text PRIMARY KEY CHECK (model_version_id ~ '^mv_[0-9a-f]{64}$'),
    model_type text NOT NULL CHECK (model_type IN ('club','player','lineup','match','betting','fantasy')),
    version_name text NOT NULL CHECK (length(trim(version_name)) > 0),
    code_sha text CHECK (code_sha IS NULL OR code_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    configuration jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(configuration) = 'object'),
    training_start timestamptz,
    training_end timestamptz,
    evaluation_start timestamptz,
    evaluation_end timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    notes text NOT NULL DEFAULT '',
    CHECK ((training_start IS NULL AND training_end IS NULL) OR
           (training_start IS NOT NULL AND training_end IS NOT NULL AND training_start < training_end)),
    CHECK ((evaluation_start IS NULL AND evaluation_end IS NULL) OR
           (evaluation_start IS NOT NULL AND evaluation_end IS NOT NULL AND evaluation_start < evaluation_end))
);
CREATE INDEX IF NOT EXISTS model_versions_type_created_idx ON model_versions(model_type, created_at);
-- Names are labels, not unique identities. Changed configuration/SHA gets a new ID.
COMMENT ON TABLE model_versions IS 'Immutable model provenance registry shared by domain-specific outputs/snapshots. Register through thecornerfc.model_versions.';
COMMENT ON COLUMN model_versions.created_at IS 'Database row insertion time. Never source observation time or event time.';
COMMENT ON COLUMN model_versions.training_start IS 'Inclusive training window start; end is exclusive. NULL pair means not applicable/unknown.';
COMMENT ON COLUMN model_versions.evaluation_start IS 'Inclusive evaluation window start; end is exclusive. NULL pair means not applicable/unknown.';

CREATE OR REPLACE FUNCTION prevent_model_version_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Model versions are immutable; register a new version instead';
END;
$$;
DROP TRIGGER IF EXISTS model_versions_immutable ON model_versions;
CREATE TRIGGER model_versions_immutable BEFORE UPDATE OR DELETE ON model_versions
FOR EACH ROW EXECUTE FUNCTION prevent_model_version_mutation();
ALTER TABLE model_versions ENABLE ROW LEVEL SECURITY;
