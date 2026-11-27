"""Ingest World Bank bulk CSV downloads from datasources/ into PostgreSQL.

Each datasources/<folder>/ holds three files from a World Bank "Download CSV":
    API_<indicator>_DS2_en_csv_v2_<n>.csv                  wide data, one column per year
    Metadata_Country_API_<indicator>_...csv                country code, region, income group
    Metadata_Indicator_API_<indicator>_...csv              indicator name and definition

Every run first downloads the latest CSVs into --sources (fetch.py), unless --no-fetch.
If a download fails, the files already on disk for that indicator are used.

Usage:
    python ingest.py                           # fetch, create the schema if missing, load
    python ingest.py --no-fetch                # load the files already in datasources/
    python ingest.py --countries first10countries.txt   # only these ISO3 codes (default: all countries)
    python ingest.py --init-db                 # re-apply schema.sql even if the tables exist
    python ingest.py --dry-run                 # parse + validate only, no database
    python ingest.py --start-year 2000         # backfill a wider window
    python ingest.py --force                   # reload files already ingested

Connection comes from DATABASE_URL (default postgresql://postgres@localhost:5432/geip);
the password can come from PGPASSWORD or ~/.pgpass.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "datasources"
SCHEMA_FILE = ROOT / "schema.sql"
SOURCE = "worldbank"

# The unit isn't a separate field in the World Bank files, so known indicators are
# mapped explicitly. Anything else falls back to the text in parentheses in its name.
UNITS = {
    "NY.GDP.MKTP.CD": "current US$",
    "FP.CPI.TOTL.ZG": "annual %",
    "SL.UEM.TOTL.ZS": "% of total labor force",
    "SP.POP.TOTL": "persons",
}

log = logging.getLogger("ingest")


# --------------------------------------------------------------------------- parsing


@dataclass
class SourceFile:
    """One World Bank download: the data file plus its two metadata files."""

    data: Path
    country_meta: Path
    indicator_meta: Path
    sha256: str
    source_updated: date | None


@dataclass
class Parsed:
    source: SourceFile
    observations: pd.DataFrame  # every non-null value: country_code, country_name, indicator_code, year, value
    countries: pd.DataFrame  # country_code, country_name, region, income_group, is_aggregate
    indicators: pd.DataFrame  # indicator_code, indicator_name, unit, source_note, source_org
    warnings: list[str] = field(default_factory=list)


def discover(root: Path) -> list[SourceFile]:
    """Find every API_*.csv under root (folder names don't matter, e.g. 'GDP ')."""
    found = []
    for data in sorted(root.glob("*/API_*.csv")):
        folder = data.parent
        if folder.name.startswith("."):  # fetch.py's in-progress/backup folders
            continue
        stem = data.name  # Metadata files are named Metadata_<kind>_<data file name>
        country_meta = folder / f"Metadata_Country_{stem}"
        indicator_meta = folder / f"Metadata_Indicator_{stem}"
        missing = [p.name for p in (country_meta, indicator_meta) if not p.exists()]
        if missing:
            raise FileNotFoundError(f"{folder}: missing {', '.join(missing)}")
        found.append(
            SourceFile(
                data=data,
                country_meta=country_meta,
                indicator_meta=indicator_meta,
                sha256=hashlib.sha256(data.read_bytes()).hexdigest(),
                source_updated=read_last_updated(data),
            )
        )
    return found


def read_last_updated(path: Path) -> date | None:
    """Read the 'Last Updated Date' line from the 4-line preamble of a data file."""
    with path.open(encoding="utf-8-sig") as f:
        for _, line in zip(range(4), f):
            if line.startswith('"Last Updated Date"'):
                return date.fromisoformat(re.findall(r'"([^"]*)"', line)[1])
    return None


def read_observations(path: Path) -> pd.DataFrame:
    """Turn the wide data file (one column per year) into one row per value."""
    df = pd.read_csv(path, skiprows=4, encoding="utf-8-sig", dtype=str)
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]  # trailing comma on every row
    id_cols = ["Country Name", "Country Code", "Indicator Name", "Indicator Code"]
    year_cols = [c for c in df.columns if c not in id_cols]
    bad = [c for c in year_cols if not c.isdigit()]
    if bad:
        raise ValueError(f"{path.name}: unexpected non-year columns {bad}")

    long = df.melt(id_vars=id_cols, value_vars=year_cols, var_name="year", value_name="value")
    long = long.dropna(subset=["value"])
    long["value"] = pd.to_numeric(long["value"], errors="raise").astype(float)
    long["year"] = long["year"].astype(int)
    long = long.rename(
        columns={
            "Country Code": "country_code",
            "Country Name": "country_name",
            "Indicator Code": "indicator_code",
        }
    )[["country_code", "country_name", "indicator_code", "year", "value"]]
    return long.reset_index(drop=True)


def read_country_meta(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    out = pd.DataFrame(
        {
            "country_code": df["Country Code"].str.strip(),
            "country_name": df["TableName"].str.strip(),
            "region": df["Region"],
            "income_group": df["IncomeGroup"],
        }
    )
    # Aggregates (World, regions, income groups) have no region in the metadata.
    out["is_aggregate"] = out["region"].isna()
    return out.astype(object).where(out.notna(), None)


def read_indicator_meta(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    out = pd.DataFrame(
        {
            "indicator_code": df["INDICATOR_CODE"].str.strip(),
            "indicator_name": df["INDICATOR_NAME"].str.strip(),
            "source_note": df["SOURCE_NOTE"],
            "source_org": df["SOURCE_ORGANIZATION"],
        }
    )
    out["unit"] = [unit_for(c, n) for c, n in zip(out.indicator_code, out.indicator_name)]
    return out.astype(object).where(out.notna(), None)


def unit_for(code: str, name: str) -> str:
    if code in UNITS:
        return UNITS[code]
    m = re.search(r"\(([^)]*)\)", name)
    return m.group(1) if m else "unknown"


def parse(src: SourceFile) -> Parsed:
    obs = read_observations(src.data)
    countries = read_country_meta(src.country_meta)
    indicators = read_indicator_meta(src.indicator_meta)
    parsed = Parsed(src, obs, countries, indicators)
    validate(parsed)
    return parsed


def validate(p: Parsed) -> None:
    """Raise on problems that would corrupt the load; collect softer issues as warnings."""
    name = p.source.data.name
    obs = p.observations

    dupes = obs.duplicated(["country_code", "indicator_code", "year"])
    if dupes.any():
        raise ValueError(f"{name}: {dupes.sum()} duplicate country/indicator/year rows")

    unknown_ind = set(obs.indicator_code) - set(p.indicators.indicator_code)
    if unknown_ind:
        raise ValueError(f"{name}: indicators missing from metadata: {sorted(unknown_ind)}")

    bad_codes = obs.loc[~obs.country_code.str.fullmatch(r"[A-Z0-9]{3}"), "country_code"].unique()
    if len(bad_codes):
        raise ValueError(f"{name}: malformed country codes {list(bad_codes)}")

    if not obs.year.between(1900, 2100).all():
        raise ValueError(f"{name}: years outside 1900-2100")

    # Aggregates like "Africa Eastern and Southern" appear in the data but not always in
    # the country metadata; they're harmless but worth knowing about.
    unknown_c = set(obs.country_code) - set(p.countries.country_code)
    if unknown_c:
        p.warnings.append(f"{len(unknown_c)} country codes not in metadata: {sorted(unknown_c)[:5]}...")

    for code, unit in zip(p.indicators.indicator_code, p.indicators.unit):
        if unit == "unknown":
            p.warnings.append(f"no unit known for {code}")


def load_targets(path: Path) -> list[str]:
    codes = []
    for line in path.read_text().splitlines():
        code = line.split("#", 1)[0].strip().upper()
        if code:
            codes.append(code)
    return codes


def all_countries(parsed: list[Parsed]) -> list[str]:
    """Every real country (not World/region/income-group aggregates) in any file's metadata."""
    codes: set[str] = set()
    for p in parsed:
        codes |= set(p.countries.loc[~p.countries.is_aggregate.astype(bool), "country_code"])
    return sorted(codes)


def select(obs: pd.DataFrame, countries: list[str], start: int, end: int) -> pd.DataFrame:
    """The subset of observations that goes into core.observation."""
    mask = obs.country_code.isin(countries) & obs.year.between(start, end)
    return obs.loc[mask].reset_index(drop=True)


def year_window(parsed: list[Parsed], start: int | None, end: int | None, years: int) -> tuple[int, int]:
    if end is None:
        end = max(int(p.observations.year.max()) for p in parsed)
    if start is None:
        start = end - years + 1
    if start > end:
        raise SystemExit(f"--start-year {start} is after --end-year {end}")
    return start, end


# --------------------------------------------------------------------------- loading


def connect():
    import psycopg  # imported here so --dry-run works without the driver

    url = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/geip")
    return psycopg.connect(url)


def init_db(conn) -> None:
    conn.execute(SCHEMA_FILE.read_text())
    conn.commit()
    log.info("schema applied from %s", SCHEMA_FILE.name)


def missing_tables(conn) -> list[str]:
    """Tables declared in schema.sql that don't exist in the database yet."""
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS\s+([\w.]+)", SCHEMA_FILE.read_text(), re.I)
    missing = [t for t in tables if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is None]
    conn.rollback()  # end the read-only transaction psycopg opened
    return missing


def ensure_schema(conn) -> None:
    """Create the raw/core schemas and tables if any are missing (schema.sql is idempotent)."""
    missing = missing_tables(conn)
    if missing:
        log.info("database is missing %s; creating schema", ", ".join(missing))
        init_db(conn)


def load(conn, p: Parsed, targets: list[str], start: int, end: int, force: bool) -> str:
    """Load one file in a single transaction. Returns 'loaded' or 'skipped'."""
    src = p.source
    rel = str(src.data.relative_to(ROOT)) if src.data.is_relative_to(ROOT) else str(src.data)

    row = conn.execute(
        "SELECT file_id, status FROM raw.files WHERE sha256 = %s", (src.sha256,)
    ).fetchone()
    selected = select(p.observations, targets, start, end)
    if row and row[1] == "loaded" and not force:
        # Same file as before, but the countries or years asked for may have changed.
        _stage(conn, selected)
        if _matched(conn) == len(selected):
            log.info("%s: already loaded as file_id=%s, skipping", rel, row[0])
            return "skipped"
        log.info("%s: already loaded, but the selected countries/years aren't all in core.observation; "
                 "reloading", rel)
    try:
        with conn.transaction():
            file_id = _register_file(conn, src, rel, row)
            _copy_raw(conn, file_id, p.observations)
            _upsert_dimensions(conn, p)
            counts = _upsert_observations(conn, file_id, selected)

            # Reconcile: every selected row must now be present with its new value.
            present = _matched(conn)
            if present != len(selected):
                raise RuntimeError(f"reconciliation failed: staged {len(selected)}, matched {present}")

            conn.execute(
                """UPDATE raw.files SET status = 'loaded', rows_parsed = %s, rows_loaded = %s,
                          error = NULL, ingested_at = now() WHERE file_id = %s""",
                (len(p.observations), len(selected), file_id),
            )
    except Exception as exc:
        _mark_failed(conn, src, rel, exc)
        raise

    log.info(
        "%s: file_id=%s raw=%d selected=%d (new=%d revised=%d unchanged=%d)",
        rel, file_id, len(p.observations), len(selected),
        counts["new"], counts["revised"], len(selected) - counts["new"] - counts["revised"],
    )
    return "loaded"


def _register_file(conn, src: SourceFile, rel: str, existing) -> int:
    if existing:
        file_id = existing[0]
        conn.execute("DELETE FROM raw.observations WHERE file_id = %s", (file_id,))
        conn.execute(
            "UPDATE raw.files SET status = 'pending', path = %s WHERE file_id = %s", (rel, file_id)
        )
        return file_id
    return conn.execute(
        """INSERT INTO raw.files (source, path, sha256, source_updated)
           VALUES (%s, %s, %s, %s) RETURNING file_id""",
        (SOURCE, rel, src.sha256, src.source_updated),
    ).fetchone()[0]


def _copy_raw(conn, file_id: int, obs: pd.DataFrame) -> None:
    with conn.cursor().copy(
        "COPY raw.observations (file_id, country_code, country_name, indicator_code, year, value) FROM STDIN"
    ) as cp:
        for r in obs.itertuples(index=False):
            cp.write_row((file_id, r.country_code, r.country_name, r.indicator_code, r.year, r.value))


def _upsert_dimensions(conn, p: Parsed) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO core.country (country_code, country_name, region, income_group, is_aggregate)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (country_code) DO UPDATE SET
                 country_name = EXCLUDED.country_name, region = EXCLUDED.region,
                 income_group = EXCLUDED.income_group, is_aggregate = EXCLUDED.is_aggregate,
                 updated_at = now()
               WHERE (core.country.country_name, core.country.region, core.country.income_group,
                      core.country.is_aggregate)
                     IS DISTINCT FROM
                     (EXCLUDED.country_name, EXCLUDED.region, EXCLUDED.income_group, EXCLUDED.is_aggregate)""",
            list(p.countries[["country_code", "country_name", "region", "income_group", "is_aggregate"]]
                 .itertuples(index=False, name=None)),
        )
        cur.executemany(
            """INSERT INTO core.indicator (indicator_code, indicator_name, unit, source, source_note, source_org)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (indicator_code) DO UPDATE SET
                 indicator_name = EXCLUDED.indicator_name, unit = EXCLUDED.unit,
                 source_note = EXCLUDED.source_note, source_org = EXCLUDED.source_org,
                 updated_at = now()""",
            [(r.indicator_code, r.indicator_name, r.unit, SOURCE, r.source_note, r.source_org)
             for r in p.indicators.itertuples(index=False)],
        )


def _stage(conn, selected: pd.DataFrame) -> None:
    """Put the selected observations in the pg_temp.stage table."""
    conn.execute(
        """CREATE TEMP TABLE IF NOT EXISTS stage (
             country_code char(3), indicator_code text, year integer, value double precision
           ) ON COMMIT DELETE ROWS"""
    )
    conn.execute("TRUNCATE pg_temp.stage")
    with conn.cursor().copy("COPY pg_temp.stage FROM STDIN") as cp:
        for r in selected.itertuples(index=False):
            cp.write_row((r.country_code, r.indicator_code, r.year, r.value))


def _matched(conn) -> int:
    """How many staged rows are in core.observation with the same value."""
    return conn.execute(
        """SELECT count(*) FROM core.observation o JOIN pg_temp.stage s
             USING (country_code, indicator_code, year)
            WHERE o.value = s.value"""
    ).fetchone()[0]


def _upsert_observations(conn, file_id: int, selected: pd.DataFrame) -> dict[str, int]:
    _stage(conn, selected)

    # Keep the old value before overwriting a revised observation.
    revised = conn.execute(
        """INSERT INTO core.observation_history
             (country_code, indicator_code, year, old_value, new_value, old_file_id, new_file_id)
           SELECT o.country_code, o.indicator_code, o.year, o.value, s.value, o.file_id, %s
             FROM core.observation o JOIN pg_temp.stage s USING (country_code, indicator_code, year)
            WHERE o.value IS DISTINCT FROM s.value""",
        (file_id,),
    ).rowcount

    upserted = conn.execute(
        """INSERT INTO core.observation (country_code, indicator_code, year, value, file_id)
           SELECT country_code, indicator_code, year, value, %s FROM pg_temp.stage
           ON CONFLICT (country_code, indicator_code, year) DO UPDATE SET
             value = EXCLUDED.value, file_id = EXCLUDED.file_id, updated_at = now()
           WHERE core.observation.value IS DISTINCT FROM EXCLUDED.value""",
        (file_id,),
    ).rowcount
    return {"new": upserted - revised, "revised": revised}


def _mark_failed(conn, src: SourceFile, rel: str, exc: Exception) -> None:
    try:
        with conn.transaction():
            conn.execute(
                """INSERT INTO raw.files (source, path, sha256, source_updated, status, error)
                   VALUES (%s, %s, %s, %s, 'failed', %s)
                   ON CONFLICT (sha256) DO UPDATE SET status = 'failed', error = EXCLUDED.error""",
                (SOURCE, rel, src.sha256, src.source_updated, f"{type(exc).__name__}: {exc}"),
            )
    except Exception:  # don't hide the original error
        log.exception("could not record failure for %s", rel)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    ap.add_argument("--countries", type=Path,
                    help="file of ISO3 codes to load (e.g. first10countries.txt); "
                         "default: every country in the World Bank metadata, excluding aggregates")
    ap.add_argument("--start-year", type=int)
    ap.add_argument("--end-year", type=int, help="default: latest year in the files")
    ap.add_argument("--years", type=int, default=10, help="window size when --start-year is omitted")
    ap.add_argument("--init-db", action="store_true", help="re-apply schema.sql (missing tables are created automatically)")
    ap.add_argument("--dry-run", action="store_true", help="parse and validate only")
    ap.add_argument("--force", action="store_true", help="reload files already ingested")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip downloading fresh World Bank CSVs; use the files already in --sources")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.no_fetch:
        import fetch

        if fetch.fetch_all(args.sources):
            log.warning("some downloads failed; ingesting the files already on disk for those")

    sources = discover(args.sources)
    if not sources:
        log.error("no API_*.csv files under %s", args.sources)
        return 1

    parsed = []
    for src in sources:
        p = parse(src)
        for w in p.warnings:
            log.warning("%s: %s", src.data.name, w)
        parsed.append(p)

    if args.countries:
        targets = load_targets(args.countries)
    else:
        targets = all_countries(parsed)
        log.info("no --countries given: loading all %d countries in the metadata", len(targets))
    if not targets:
        log.error("no target countries")
        return 1

    start, end = year_window(parsed, args.start_year, args.end_year, args.years)
    log.info("%d files, %d target countries, years %d-%d", len(parsed), len(targets), start, end)

    known = set().union(*(set(p.observations.country_code) for p in parsed))
    for code in targets:
        if code not in known:
            log.warning("target country %s has no data in any file", code)

    if args.dry_run:
        for p in parsed:
            sel = select(p.observations, targets, start, end)
            expected = len(targets) * (end - start + 1)
            log.info(
                "%s: raw=%d selected=%d/%d (%d missing values) updated=%s",
                p.source.data.parent.name.strip(), len(p.observations), len(sel),
                expected, expected - len(sel), p.source.source_updated,
            )
        return 0

    failures = 0
    with connect() as conn:
        if args.init_db:
            init_db(conn)
        else:
            ensure_schema(conn)
        for p in parsed:
            try:
                load(conn, p, targets, start, end, args.force)
            except Exception:
                failures += 1
                log.exception("failed to load %s", p.source.data.name)
    log.info("done: %d loaded or skipped, %d failed", len(parsed) - failures, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
