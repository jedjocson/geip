"""Tests for dashboard.py: pure helpers, plus a smoke run of the page against the test DB."""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dashboard  # noqa: E402
import ingest  # noqa: E402
from test_ingest import DB_URL, conn, write_fixture  # noqa: E402,F401  (database fixture)


def frame():
    rows = [
        ("PHL", "Philippines", False, "A", "Ind A", "annual %", "annual", 2019, 1.0, date(2026, 11, 11)),
        ("PHL", "Philippines", False, "A", "Ind A", "annual %", "annual", 2021, 3.0, date(2026, 11, 11)),
        ("ARM", "Armenia", False, "A", "Ind A", "annual %", "annual", 2020, 2.0, date(2026, 11, 11)),
        ("WLD", "World", True, "B", "Ind B", "persons", "annual", 2020, 9.0, None),
    ]
    return pd.DataFrame(rows, columns=dashboard.COLUMNS)


def test_filter_data():
    df = frame()
    sel = dashboard.filter_data(df, ["Philippines"], (2020, 2021), "Ind A")
    assert sel[["country_code", "year"]].values.tolist() == [["PHL", 2021]]
    assert len(dashboard.filter_data(df, ["Philippines", "World"], (2019, 2021))) == 3


def test_indicator_info_and_missing_points():
    df = frame()
    assert dashboard.indicator_info(df, "Ind A") == {
        "unit": "annual %", "frequency": "annual", "code": "A", "updated": date(2026, 11, 11)}
    assert dashboard.indicator_info(df, "Ind B")["updated"] is None
    sel = dashboard.filter_data(df, ["Philippines", "Armenia"], (2019, 2021), "Ind A")
    assert dashboard.missing_points(sel, ["Philippines", "Armenia"], (2019, 2021)) == 3


def test_line_chart_labels_unit():
    fig = dashboard.line_chart(frame().query("indicator_code == 'A'"), "Ind A (annual %)", "annual %")
    assert fig.layout.yaxis.title.text == "annual %"
    assert {t.name for t in fig.data} == {"Philippines", "Armenia"}
    assert fig.layout.yaxis.type != "log"


def test_chart_title_does_not_repeat_unit():
    assert dashboard.chart_title("GDP (current US$)", "current US$") == "GDP (current US$)"
    assert dashboard.chart_title("Population, total", "persons") == "Population, total (persons)"


def test_line_chart_log_scale_only_for_positive_values():
    sel = frame().query("indicator_code == 'A'")
    assert dashboard.line_chart(sel, "t", "u", log=True).layout.yaxis.type == "log"
    negative = sel.assign(value=[-1.0, 2.0, 3.0])  # e.g. deflation
    assert dashboard.line_chart(negative, "t", "u", log=True).layout.yaxis.type != "log"


# --------------------------------------------------------------------------- page smoke tests


@pytest.fixture
def app(conn, monkeypatch):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    import streamlit as st

    monkeypatch.setenv("DATABASE_URL", DB_URL)
    st.cache_data.clear()  # the cache outlives a single AppTest run
    return AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=30)


def test_page_with_data(app, conn, tmp_path):  # noqa: F811
    [src] = ingest.discover(write_fixture(tmp_path / "ds"))
    ingest.load(conn, ingest.parse(src), ["PHL", "WLD"], 2019, 2021, force=False)
    conn.commit()  # the page reads over its own connection

    app.run()
    assert not app.exception
    assert app.subheader[0].value == "Test indicator (annual %)"
    assert "annual %" in app.caption[0].value
    assert len(app.get("plotly_chart")) == 1
    # World is an aggregate, so it isn't selected by default.
    assert app.sidebar.multiselect[0].value == ["Philippines"]

    app.sidebar.toggle[0].set_value(True).run()
    assert not app.exception

    app.sidebar.radio[0].set_value("All indicators").run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 1  # one indicator in the fixture


def test_page_with_empty_database(app):
    app.run()
    assert not app.exception
    assert "no observations" in app.warning[0].value
