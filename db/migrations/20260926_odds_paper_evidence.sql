-- Requires model_versions and match_prediction_snapshots. No legacy rows are rewritten.
CREATE TABLE IF NOT EXISTS odds_observations (
    observation_id bigserial PRIMARY KEY,
    fixture_id integer NOT NULL,
    bookmaker_id integer NOT NULL,
    market_id integer NOT NULL,
    selection text NOT NULL,
    odds numeric NOT NULL CHECK(odds > 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    captured_at timestamptz NOT NULL,
    effective_at timestamptz,
    provider_updated_at timestamptz,
    source text NOT NULL
);
CREATE INDEX IF NOT EXISTS odds_observations_lookup_idx
ON odds_observations(fixture_id,bookmaker_id,market_id,selection,captured_at DESC,observation_id DESC);
CREATE TABLE IF NOT EXISTS paper_decisions (
    decision_id bigserial PRIMARY KEY,
    paper_bet_id bigint NOT NULL UNIQUE REFERENCES paper_bets(bet_id) ON DELETE RESTRICT,
    fixture_id integer NOT NULL,
    strategy text NOT NULL,
    strategy_version_id text NOT NULL REFERENCES model_versions(model_version_id) ON DELETE RESTRICT,
    model_version_id text NOT NULL REFERENCES model_versions(model_version_id) ON DELETE RESTRICT,
    prediction_snapshot_id bigint NOT NULL REFERENCES match_prediction_snapshots(snapshot_id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    captured_at timestamptz NOT NULL, -- decision timestamp
    effective_at timestamptz NOT NULL, -- known kickoff
    market text NOT NULL,
    market_id integer NOT NULL,
    selection text NOT NULL,
    bookmaker_id integer NOT NULL,
    model_probability double precision NOT NULL,
    raw_implied_probability double precision NOT NULL,
    fair_probability double precision,
    odds_taken numeric NOT NULL,
    market_margin double precision, -- chosen bookmaker's complete market overround
    edge double precision NOT NULL,
    edge_formula text NOT NULL,
    stake_units numeric NOT NULL,
    odds_observation_id bigint REFERENCES odds_observations(observation_id) ON DELETE RESTRICT,
    evidence jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_outcomes (
    outcome_id bigserial PRIMARY KEY,
    decision_id bigint NOT NULL REFERENCES paper_decisions(decision_id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    captured_at timestamptz NOT NULL,
    source text NOT NULL,
    closing_odds numeric,
    closing_fair_probability double precision,
    price_clv double precision,
    probability_movement double precision,
    result text NOT NULL,
    profit_units numeric NOT NULL,
    evidence jsonb NOT NULL,
    content_hash text NOT NULL,
    UNIQUE(decision_id,content_hash)
);
CREATE OR REPLACE FUNCTION guard_paper_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Odds and paper evidence are append only';
    END IF;
    NEW.created_at := clock_timestamp();
    IF TG_TABLE_NAME='paper_decisions' THEN
        IF NOT EXISTS (SELECT 1 FROM model_versions WHERE model_version_id=NEW.strategy_version_id AND model_type='betting') THEN
            RAISE EXCEPTION 'Paper decisions require a betting strategy version';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM match_prediction_snapshots s WHERE s.snapshot_id=NEW.prediction_snapshot_id
            AND s.model_version_id=NEW.model_version_id AND s.fixture_id=NEW.fixture_id
            AND s.source='prospective' AND s.captured_at<=NEW.captured_at AND s.created_at<=NEW.captured_at) THEN
            RAISE EXCEPTION 'Paper decision requires its actual prospective prediction snapshot';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['odds_observations','paper_decisions','paper_outcomes'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS evidence_guard ON %I',t);
        EXECUTE format('CREATE TRIGGER evidence_guard BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION guard_paper_evidence()',t);
        EXECUTE format('DROP TRIGGER IF EXISTS evidence_truncate_guard ON %I',t);
        EXECUTE format('CREATE TRIGGER evidence_truncate_guard BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION guard_paper_evidence()',t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
    END LOOP;
END;
$$;
