-- Global Economic Intelligence Platform: storage for World Bank CSV ingestion.
-- Safe to run repeatedly.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;

-- One row per source file we have seen. sha256 makes reruns idempotent.
CREATE TABLE IF NOT EXISTS raw.files (
    file_id         serial PRIMARY KEY,
    source          text        NOT NULL,          -- e.g. 'worldbank'
    path            text        NOT NULL,
    sha256          char(64)    NOT NULL UNIQUE,
    source_updated  date,                          -- "Last Updated Date" from the file header
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'loaded', 'failed')),
    rows_parsed     integer,
    rows_loaded     integer,
    error           text
);

-- Every non-null value in every file, unfiltered, exactly as parsed.
-- This is the preserved raw copy; core tables are derived from it.
CREATE TABLE IF NOT EXISTS raw.observations (
    file_id         integer NOT NULL REFERENCES raw.files(file_id) ON DELETE CASCADE,
    country_code    text    NOT NULL,
    country_name    text    NOT NULL,
    indicator_code  text    NOT NULL,
    year            integer NOT NULL,
    value           double precision NOT NULL,
    PRIMARY KEY (file_id, country_code, indicator_code, year)
);

CREATE TABLE IF NOT EXISTS core.country (
    country_code    char(3) PRIMARY KEY,
    country_name    text    NOT NULL,
    region          text,
    income_group    text,
    is_aggregate    boolean NOT NULL,              -- regions/income groups have no Region
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.indicator (
    indicator_code  text PRIMARY KEY,
    indicator_name  text NOT NULL,
    unit            text NOT NULL,
    frequency       text NOT NULL DEFAULT 'annual',
    source          text NOT NULL,
    source_note     text,
    source_org      text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Current value per country / indicator / year (only the target countries).
CREATE TABLE IF NOT EXISTS core.observation (
    country_code    char(3) NOT NULL REFERENCES core.country(country_code),
    indicator_code  text    NOT NULL REFERENCES core.indicator(indicator_code),
    year            integer NOT NULL CHECK (year BETWEEN 1900 AND 2100),
    value           double precision NOT NULL,
    file_id         integer NOT NULL REFERENCES raw.files(file_id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, indicator_code, year)
);

-- Previous values, written whenever a source revises an observation.
CREATE TABLE IF NOT EXISTS core.observation_history (
    id              bigserial PRIMARY KEY,
    country_code    char(3) NOT NULL,
    indicator_code  text    NOT NULL,
    year            integer NOT NULL,
    old_value       double precision NOT NULL,
    new_value       double precision NOT NULL,
    old_file_id     integer NOT NULL,
    new_file_id     integer NOT NULL,
    changed_at      timestamptz NOT NULL DEFAULT now()
);
