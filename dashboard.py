"""Streamlit dashboard for the ingested World Bank indicators.

    streamlit run dashboard.py          # opens http://localhost:8501

Reads core.observation (joined to core.country, core.indicator and raw.files) from
DATABASE_URL, the same database ingest.py loads (default
postgresql://postgres@localhost:5432/geip). Run ingest.py first.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

import ingest

QUERY = """
SELECT o.country_code, c.country_name, c.is_aggregate,
       o.indicator_code, i.indicator_name, i.unit, i.frequency,
       o.year, o.value, f.source_updated
  FROM core.observation o
  JOIN core.country   c USING (country_code)
  JOIN core.indicator i USING (indicator_code)
  JOIN raw.files      f ON f.file_id = o.file_id
 ORDER BY i.indicator_name, c.country_name, o.year
"""

DEFAULT_COUNTRY = "PHL"  # shown on startup and whenever no country is selected

COLUMNS = ["country_code", "country_name", "is_aggregate", "indicator_code", "indicator_name",
           "unit", "frequency", "year", "value", "source_updated"]


# --------------------------------------------------------------------------- data


def load_data() -> pd.DataFrame:
    with ingest.connect() as conn:
        rows = conn.execute(QUERY).fetchall()
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["year"] = df["year"].astype(int)
    df["value"] = df["value"].astype(float)
    return df


@st.cache_data(ttl=300, show_spinner="Loading data…")
def cached_data() -> pd.DataFrame:
    return load_data()


def default_country(df: pd.DataFrame, code: str = DEFAULT_COUNTRY) -> str:
    """Name of the country shown by default: DEFAULT_COUNTRY if it has data, otherwise the
    most populous country in the latest year, otherwise the first alphabetically."""
    names = df.drop_duplicates("country_code").set_index("country_code")["country_name"]
    if code in names.index:
        return names[code]
    real = df[~df.is_aggregate.astype(bool)]
    pop = real[real.indicator_code == "SP.POP.TOTL"]
    if not pop.empty:
        return pop.sort_values("year").groupby("country_name").value.last().idxmax()
    return sorted((real if not real.empty else df).country_name.unique())[0]


def filter_data(df: pd.DataFrame, countries: list[str], years: tuple[int, int],
                indicator: str | None = None) -> pd.DataFrame:
    mask = df.country_name.isin(countries) & df.year.between(*years)
    if indicator is not None:
        mask &= df.indicator_name == indicator
    return df.loc[mask]


def indicator_info(df: pd.DataFrame, indicator: str) -> dict:
    rows = df[df.indicator_name == indicator]
    updated = rows.source_updated.dropna()
    return {
        "unit": rows.unit.iloc[0],
        "frequency": rows.frequency.iloc[0],
        "code": rows.indicator_code.iloc[0],
        "updated": updated.max() if len(updated) else None,
    }


def missing_points(sel: pd.DataFrame, countries: list[str], years: tuple[int, int]) -> int:
    expected = len(countries) * (years[1] - years[0] + 1)
    return expected - len(sel)


def chart_title(name: str, unit: str) -> str:
    """World Bank names usually already end in the unit, e.g. 'GDP (current US$)'."""
    return name if unit.lower() in name.lower() else f"{name} ({unit})"


def line_chart(sel: pd.DataFrame, title: str | None, unit: str, height: int = 450, log: bool = False,
               compact: bool = False):
    """One line per country. compact=True suits the small charts in the grid view."""
    fig = px.line(
        sel, x="year", y="value", color="country_name", markers=True, title=title, height=height,
        log_y=log and bool((sel.value > 0).all()),  # log axes can't show zero or negative values
        labels={"year": "Year", "value": unit, "country_name": "Country"},
    )
    if sel.year.nunique() <= 15:  # whole years only; every other one in the small charts
        fig.update_xaxes(dtick=2 if compact else 1, tick0=int(sel.year.min()))
    fig.update_layout(legend_title_text="", margin={"t": 50 if title else 10, "l": 10, "r": 10, "b": 10})
    if compact:  # legend below the plot so it doesn't take half the width
        fig.update_layout(legend={"orientation": "h", "yanchor": "top", "y": -0.25, "x": 0})
    return fig


# --------------------------------------------------------------------------- page


def main() -> None:
    st.set_page_config(page_title="GEIP · World Bank indicators", layout="wide")
    st.title("Global Economic Intelligence Platform")

    try:
        df = cached_data()
    except Exception as exc:  # no database, no schema, bad credentials…
        st.error(f"Could not read from the database: {exc}")
        st.info("Check DATABASE_URL, then run `python ingest.py` to create the tables and load data.")
        st.stop()
    if df.empty:
        st.warning("The database has no observations yet. Run `python ingest.py` to load data.")
        st.stop()

    indicators = sorted(df.indicator_name.unique())
    all_countries = sorted(df.country_name.unique())
    fallback = default_country(df)
    year_min, year_max = int(df.year.min()), int(df.year.max())

    with st.sidebar:
        st.header("Filters")
        view = st.radio("View", ["One indicator", "All indicators"], horizontal=True)
        indicator = st.selectbox("Indicator", indicators) if view == "One indicator" else None
        countries = st.multiselect("Countries", all_countries, default=[fallback],
                                   help=f"{len(all_countries)} available")
        years = st.slider("Years", year_min, year_max, (year_min, year_max)) \
            if year_min < year_max else (year_min, year_max)
        log = st.toggle("Log scale", help="Useful when countries differ by orders of magnitude "
                        "(GDP, population). Ignored for charts with zero or negative values.")
        if st.button("Reload data"):
            cached_data.clear()
            st.rerun()

    if not countries:
        countries = [fallback]
        st.caption(f"No countries selected, showing {fallback}.")

    if indicator is not None:
        info = indicator_info(df, indicator)
        sel = filter_data(df, countries, years, indicator)
        st.subheader(indicator)
        st.caption(
            f"Unit: **{info['unit']}** · Frequency: **{info['frequency']}** · Code: `{info['code']}`"
            + (f" · Source updated {info['updated']}" if info["updated"] else "")
        )
        if sel.empty:
            st.info("No data for this selection.")
            st.stop()
        st.plotly_chart(line_chart(sel, None, info["unit"], log=log),
                        width="stretch")
        gaps = missing_points(sel, countries, years)
        if gaps:
            st.caption(f"{gaps} country-years have no value in the source data and are not plotted.")

        table = sel.pivot(index="year", columns="country_name", values="value").sort_index(ascending=False)
        with st.expander("Data table"):
            st.dataframe(table, width="stretch")
            st.download_button("Download CSV", table.to_csv().encode(), file_name=f"{info['code']}.csv",
                               mime="text/csv")
    else:
        cols = st.columns(2)
        for i, name in enumerate(indicators):
            info = indicator_info(df, name)
            sel = filter_data(df, countries, years, name)
            with cols[i % 2]:
                if sel.empty:
                    st.info(f"{name}: no data for this selection.")
                else:
                    st.markdown(f"**{chart_title(name, info['unit'])}**")  # wraps, unlike a plot title
                    st.plotly_chart(line_chart(sel, None, info["unit"], height=420, log=log, compact=True),
                                    width="stretch")


if __name__ == "__main__":
    main()
