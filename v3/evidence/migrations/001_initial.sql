-- V3 Evidence Layer Schema
-- MisterMoney Resolution Intelligence

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Try to enable TimescaleDB extension (optional, will continue if not available)
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
    RAISE NOTICE 'TimescaleDB extension enabled';
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'TimescaleDB not available, continuing without it';
END;
$$;

-- Source Documents: raw data from APIs, articles, filings
CREATE TABLE IF NOT EXISTS source_documents (
    doc_id TEXT PRIMARY KEY,
    url TEXT,
    source_type TEXT NOT NULL,  -- 'article', 'api', 'filing', 'social'
    publisher TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash TEXT NOT NULL,
    title TEXT,
    text_path TEXT,  -- S3/local path to full text
    metadata JSONB DEFAULT '{}',
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_docs_source_type ON source_documents(source_type);
CREATE INDEX IF NOT EXISTS idx_source_docs_publisher ON source_documents(publisher);
CREATE INDEX IF NOT EXISTS idx_source_docs_content_hash ON source_documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_source_docs_fetched_at ON source_documents(fetched_at DESC);

-- Evidence Items: extracted claims, signals, data points
CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    doc_id TEXT REFERENCES source_documents(doc_id) ON DELETE CASCADE,
    ts_event TIMESTAMPTZ,  -- when the event actually occurred
    ts_observed TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- when we observed/extracted it
    polarity TEXT NOT NULL CHECK (polarity IN ('YES', 'NO', 'MIXED', 'NEUTRAL')),
    claim TEXT NOT NULL,
    reliability FLOAT CHECK (reliability >= 0 AND reliability <= 1),
    freshness_hours FLOAT,  -- how fresh is this evidence
    extracted_values JSONB DEFAULT '{}',  -- structured data extracted from claim
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_condition_id ON evidence_items(condition_id);
CREATE INDEX IF NOT EXISTS idx_evidence_doc_id ON evidence_items(doc_id);
CREATE INDEX IF NOT EXISTS idx_evidence_polarity ON evidence_items(polarity);
CREATE INDEX IF NOT EXISTS idx_evidence_ts_event ON evidence_items(ts_event DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_ts_observed ON evidence_items(ts_observed DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_reliability ON evidence_items(reliability DESC);

-- Rule Graphs: structured representation of market conditions
CREATE TABLE IF NOT EXISTS rule_graphs (
    condition_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,  -- human-readable name
    operator TEXT,  -- '>', '<', '>=', '<=', '==', 'contains', etc.
    threshold_num FLOAT,
    threshold_text TEXT,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    edge_cases JSONB DEFAULT '[]',  -- list of known edge cases/exceptions
    clarification_ids JSONB DEFAULT '[]',  -- references to clarifying evidence
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rule_graphs_source_name ON rule_graphs(source_name);
CREATE INDEX IF NOT EXISTS idx_rule_graphs_updated_at ON rule_graphs(updated_at DESC);

-- Fair Value Signals: calibrated probabilities with uncertainty
CREATE TABLE IF NOT EXISTS fair_value_signals (
    condition_id TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    p_calibrated FLOAT NOT NULL CHECK (p_calibrated >= 0 AND p_calibrated <= 1),
    p_low FLOAT CHECK (p_low >= 0 AND p_low <= 1),
    p_high FLOAT CHECK (p_high >= 0 AND p_high <= 1),
    uncertainty FLOAT CHECK (uncertainty >= 0),
    skew_cents FLOAT,  -- expected value skew in cents
    hurdle_cents FLOAT,  -- minimum edge needed to trade
    hurdle_met BOOLEAN,
    route TEXT NOT NULL,  -- 'numeric', 'simple', 'rule', 'dossier'
    evidence_ids JSONB DEFAULT '[]',  -- supporting evidence
    counterevidence_ids JSONB DEFAULT '[]',  -- contradicting evidence
    models_used JSONB DEFAULT '[]',  -- which LLM models were consulted
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (condition_id, generated_at)
);

CREATE INDEX IF NOT EXISTS idx_fv_signals_condition_id ON fair_value_signals(condition_id);
CREATE INDEX IF NOT EXISTS idx_fv_signals_generated_at ON fair_value_signals(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_fv_signals_expires_at ON fair_value_signals(expires_at);
CREATE INDEX IF NOT EXISTS idx_fv_signals_hurdle_met ON fair_value_signals(hurdle_met);

-- Try to create hypertable for fair_value_signals (TimescaleDB)
-- This will fail silently if TimescaleDB is not available
DO $$
BEGIN
    PERFORM create_hypertable('fair_value_signals', 'generated_at', 
        chunk_time_interval => INTERVAL '1 day',
        if_not_exists => TRUE
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'TimescaleDB not available, using regular table for fair_value_signals';
END;
$$;
