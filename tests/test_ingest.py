"""Tests for ingest.py.

Parsing tests use a small fixture written to tmp_path. Database tests run only when
TEST_DATABASE_URL points at a disposable database (they drop and recreate the schemas).
"""

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ingest  # noqa: E402

DATA_FILE = "API_TEST.IND_DS2_en_csv_v2_1.csv"

DATA_CSV = '''﻿"Data Source","World Development Indicators",

"Last Updated Date","2026-11-11",

"Country Name","Country Code","Indicator Name","Indicator Code","2019","2020","2021",
"Philippines","PHL","Test indicator (annual %)","TEST.IND","1.5","","2.5",
"Armenia","ARM","Test indicator (annual %)","TEST.IND","","","",
"World","WLD","Test indicator (annual %)","TEST.IND","3","4","5",
'''

COUNTRY_CSV = '''﻿"Country Code","Region","IncomeGroup","SpecialNotes","TableName",
"PHL","East Asia & Pacific","Lower middle income","","Philippines",
"ARM","Europe & Central Asia","Upper middle income","","Armenia",
"WLD","","","","World",
'''

INDICATOR_CSV = '''﻿"INDICATOR_CODE","INDICATOR_NAME","SOURCE_NOTE","SOURCE_ORGANIZATION",
"TEST.IND","Test indicator (annual %)","A note
spanning two lines.","Somebody",
'''


def write_fixture(root: Path, data: str = DATA_CSV) -> Path:
    folder = root / "test "  # trailing space, like the original GDP folder
    folder.mkdir(parents=True)
    (folder / DATA_FILE).write_text(data, encoding="utf-8")
    (folder / f"Metadata_Country_{DATA_FILE}").write_text(COUNTRY_CSV, encoding="utf-8")
    (folder / f"Metadata_Indicator_{DATA_FILE}").write_text(INDICATOR_CSV, encoding="utf-8")
    return root


@pytest.fixture
def source(tmp_path):
    [src] = ingest.discover(write_fixture(tmp_path / "datasources"))
    return src


def test_discover_reads_header(source):
    assert source.source_updated == date(2026, 11, 11)
    assert len(source.sha256) == 64


def test_discover_requires_metadata(tmp_path):
    root = write_fixture(tmp_path / "ds")
    next(root.glob("*/Metadata_Country_*")).unlink()
    with pytest.raises(FileNotFoundError):
        ingest.discover(root)


def test_observations_are_long_and_drop_blanks(source):
    obs = ingest.read_observations(source.data)
    assert list(obs.columns) == ["country_code", "country_name", "indicator_code", "year", "value"]
    # 9 cells in the fixture, 4 blank
    assert len(obs) == 5
    assert obs["value"].notna().all()
    phl = obs[obs.country_code == "PHL"].set_index("year")["value"].to_dict()
    assert phl == {2019: 1.5, 2021: 2.5}
    assert "ARM" not in set(obs.country_code)


def test_metadata(source):
    countries = ingest.read_country_meta(source.country_meta).set_index("country_code")
    assert countries.loc["WLD", "is_aggregate"]
    assert not countries.loc["PHL", "is_aggregate"]
    assert countries.loc["WLD", "region"] is None

    ind = ingest.read_indicator_meta(source.indicator_meta).iloc[0]
    assert ind.unit == "annual %"
    assert "two lines" in ind.source_note


def test_units():
    assert ingest.unit_for("SP.POP.TOTL", "Population, total") == "persons"
    assert ingest.unit_for("X", "Something (constant 2015 US$)") == "constant 2015 US$"
    assert ingest.unit_for("X", "No unit here") == "unknown"


def test_validate_rejects_duplicates(tmp_path):
    dup = DATA_CSV + '"Philippines","PHL","Test indicator (annual %)","TEST.IND","9","9","9",\n'
    [src] = ingest.discover(write_fixture(tmp_path / "ds", dup))
    with pytest.raises(ValueError, match="duplicate"):
        ingest.parse(src)


def test_select_filters_countries_and_years(source):
    obs = ingest.read_observations(source.data)
    sel = ingest.select(obs, ["PHL", "ARM"], 2020, 2021)
    assert sel[["country_code", "year"]].values.tolist() == [["PHL", 2021]]


def test_load_targets(tmp_path):
    f = tmp_path / "countries"
    f.write_text("# header\nphl  # Philippines\n\nARM\n")
    assert ingest.load_targets(f) == ["PHL", "ARM"]


# --------------------------------------------------------------------------- database

DB_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def conn():
    if not DB_URL:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    import psycopg

    with psycopg.connect(DB_URL) as c:
        c.execute("DROP SCHEMA IF EXISTS core CASCADE; DROP SCHEMA IF EXISTS raw CASCADE")
        ingest.init_db(c)
        yield c


def counts(conn):
    return {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("raw.files", "raw.observations", "core.observation", "core.observation_history")
    }


def test_load_is_idempotent_and_keeps_revisions(conn, tmp_path):
    [src] = ingest.discover(write_fixture(tmp_path / "v1"))
    p = ingest.parse(src)
    assert ingest.load(conn, p, ["PHL", "WLD"], 2019, 2021, force=False) == "loaded"
    first = counts(conn)
    assert first == {"raw.files": 1, "raw.observations": 5,
                     "core.observation": 5, "core.observation_history": 0}

    # Same file again: skipped, nothing changes.
    assert ingest.load(conn, p, ["PHL", "WLD"], 2019, 2021, force=False) == "skipped"
    assert counts(conn) == first

    # A newer download revises one value.
    [src2] = ingest.discover(write_fixture(tmp_path / "v2", DATA_CSV.replace('"1.5"', '"1.7"')))
    ingest.load(conn, ingest.parse(src2), ["PHL", "WLD"], 2019, 2021, force=False)
    after = counts(conn)
    assert after["raw.files"] == 2
    assert after["core.observation"] == 5
    assert after["core.observation_history"] == 1
    old, new = conn.execute(
        "SELECT old_value, new_value FROM core.observation_history"
    ).fetchone()
    assert (old, new) == (1.5, 1.7)


def drop_schemas(conn):
    conn.execute("DROP SCHEMA IF EXISTS core CASCADE; DROP SCHEMA IF EXISTS raw CASCADE")
    conn.commit()


def test_ensure_schema_creates_missing_tables(conn):
    drop_schemas(conn)
    assert len(ingest.missing_tables(conn)) == 6
    ingest.ensure_schema(conn)
    assert ingest.missing_tables(conn) == []

    # A single missing table is recreated without touching the others' data.
    conn.execute("INSERT INTO raw.files (source, path, sha256) VALUES ('t', 'p', repeat('a', 64))")
    conn.execute("DROP TABLE core.observation_history")
    conn.commit()
    assert ingest.missing_tables(conn) == ["core.observation_history"]
    ingest.ensure_schema(conn)
    assert ingest.missing_tables(conn) == []
    assert conn.execute("SELECT count(*) FROM raw.files").fetchone()[0] == 1


def test_main_creates_schema_without_init_db(conn, tmp_path, monkeypatch):
    drop_schemas(conn)
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    countries = tmp_path / "countries"
    countries.write_text("PHL\nWLD\n")
    sources = write_fixture(tmp_path / "ds")
    assert ingest.main(["--no-fetch", "--sources", str(sources), "--countries", str(countries)]) == 0
    assert counts(conn)["core.observation"] == 5


@pytest.mark.parametrize("argv, fetched", [([], True), (["--no-fetch"], False)])
def test_main_fetches_before_ingesting(tmp_path, monkeypatch, argv, fetched):
    import fetch

    calls = []

    def fake_fetch_all(sources):
        calls.append(sources)
        write_fixture(sources)  # the download is what puts the files there
        return 0

    monkeypatch.setattr(fetch, "fetch_all", fake_fetch_all)
    sources = tmp_path / "ds"
    if not fetched:
        write_fixture(sources)
    countries = tmp_path / "countries"
    countries.write_text("PHL\n")
    assert ingest.main([*argv, "--dry-run", "--sources", str(sources), "--countries", str(countries)]) == 0
    assert calls == ([sources] if fetched else [])


def test_main_uses_existing_files_when_fetch_fails(tmp_path, monkeypatch):
    import fetch

    monkeypatch.setattr(fetch, "fetch_all", lambda sources: 4)
    sources = write_fixture(tmp_path / "ds")
    countries = tmp_path / "countries"
    countries.write_text("PHL\n")
    assert ingest.main(["--dry-run", "--sources", str(sources), "--countries", str(countries)]) == 0


def test_all_countries_excludes_aggregates(source):
    assert ingest.all_countries([ingest.parse(source)]) == ["ARM", "PHL"]


def test_main_defaults_to_all_countries(tmp_path, caplog):
    sources = write_fixture(tmp_path / "ds")
    caplog.set_level("INFO")
    assert ingest.main(["--no-fetch", "--dry-run", "--sources", str(sources)]) == 0
    assert "loading all 2 countries" in caplog.text
    assert "2 target countries" in caplog.text


def test_widening_countries_reloads_an_already_loaded_file(conn, tmp_path):
    [src] = ingest.discover(write_fixture(tmp_path / "ds"))
    p = ingest.parse(src)
    assert ingest.load(conn, p, ["WLD"], 2019, 2021, force=False) == "loaded"
    assert counts(conn)["core.observation"] == 3

    # Same file, more countries: must load the new rows rather than skip.
    assert ingest.load(conn, p, ["WLD", "PHL"], 2019, 2021, force=False) == "loaded"
    after = counts(conn)
    assert (after["raw.files"], after["core.observation"], after["core.observation_history"]) == (1, 5, 0)

    # Now everything asked for is there: skip.
    assert ingest.load(conn, p, ["WLD", "PHL"], 2019, 2021, force=False) == "skipped"
    # Narrowing is a subset of what's loaded: also skip.
    assert ingest.load(conn, p, ["PHL"], 2021, 2021, force=False) == "skipped"
