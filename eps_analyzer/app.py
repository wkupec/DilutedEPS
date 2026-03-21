"""
app.py — Streamlit dashboard for S&P 500 Diluted EPS Streak Analyzer.

Run with:
    streamlit run app.py
"""

import datetime
import os
import sys

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Ensure the package directory is on the path when run directly
sys.path.insert(0, os.path.dirname(__file__))

from analyzer import find_eps_streak_companies
from cache import bust_cache, get_last_updated, is_cache_stale, load_from_cache, save_to_cache
from data_fetcher import fetch_all_eps_data, fetch_sp500_tickers, get_failed_tickers

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="S&P 500 EPS Streak Analyzer",
    page_icon="📈",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------
if "eps_data" not in st.session_state:
    st.session_state.eps_data = {}
if "failed_tickers" not in st.session_state:
    st.session_state.failed_tickers = []
if "data_loaded" not in st.session_state:
    st.session_state.data_loaded = False


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _run_fetch(force: bool = False) -> None:
    """Fetch (or load from cache) all EPS data and store in session state."""
    if force:
        bust_cache()
        st.session_state.data_loaded = False

    # Try loading from cache first
    if not is_cache_stale() and not force:
        cached = load_from_cache()
        if cached:
            st.session_state.eps_data = cached
            st.session_state.failed_tickers = get_failed_tickers(cached)
            st.session_state.data_loaded = True
            return

    # --- Live fetch ---
    status_placeholder = st.empty()
    progress_bar = st.progress(0)
    status_text = st.empty()

    status_placeholder.info("Fetching S&P 500 constituent list from Wikipedia…")
    try:
        tickers_df = fetch_sp500_tickers()
    except Exception as exc:
        status_placeholder.error(f"Failed to fetch S&P 500 list: {exc}")
        return

    total = len(tickers_df)
    status_placeholder.info(f"Fetching EPS data for {total} tickers… (this may take a few minutes)")

    def _progress(idx: int, _total: int, ticker: str) -> None:
        frac = idx / _total
        progress_bar.progress(frac)
        status_text.text(f"Fetching {ticker} ({idx}/{_total})")

    data = fetch_all_eps_data(tickers_df, progress_callback=_progress)
    save_to_cache(data)

    progress_bar.progress(1.0)
    status_text.text("Done!")
    status_placeholder.success(f"Fetched data for {total} tickers.")

    st.session_state.eps_data = data
    st.session_state.failed_tickers = get_failed_tickers(data)
    st.session_state.data_loaded = True

    # Clean up progress widgets
    progress_bar.empty()
    status_text.empty()
    status_placeholder.empty()


# ---------------------------------------------------------------------------
# Cached computation (per Streamlit session)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _cached_analysis(
    eps_data_json: str,
    min_years: int,
    end_year: int,
) -> pd.DataFrame:
    """Wrap find_eps_streak_companies so Streamlit caches the result."""
    import json

    eps_data = json.loads(eps_data_json)
    # Restore integer keys
    for ticker in eps_data:
        eps_data[ticker]["eps_by_year"] = {
            int(k): v for k, v in eps_data[ticker]["eps_by_year"].items()
        }
    return find_eps_streak_companies(eps_data, min_years, end_year)


def _get_analysis(eps_data: dict, min_years: int, end_year: int) -> pd.DataFrame:
    import json

    eps_json = json.dumps(
        {
            t: {
                "name": v["name"],
                "sector": v["sector"],
                "eps_by_year": {str(k): val for k, val in v["eps_by_year"].items()},
            }
            for t, v in eps_data.items()
        }
    )
    return _cached_analysis(eps_json, min_years, end_year)


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------
SECTOR_COLORS = px.colors.qualitative.Plotly


def _sector_color_map(sectors: list[str]) -> dict[str, str]:
    unique = sorted(set(sectors))
    return {s: SECTOR_COLORS[i % len(SECTOR_COLORS)] for i, s in enumerate(unique)}


def _multi_line_chart(df: pd.DataFrame) -> go.Figure:
    """Multi-line Plotly chart — one line per company, colored by sector."""
    color_map = _sector_color_map(df["sector"].tolist())
    fig = go.Figure()

    for _, row in df.iterrows():
        history = row["eps_history"]
        if not history:
            continue
        years = [h[0] for h in history]
        eps_vals = [h[1] for h in history]
        fig.add_trace(
            go.Scatter(
                x=years,
                y=eps_vals,
                mode="lines+markers",
                name=row["ticker"],
                line=dict(color=color_map.get(row["sector"], "#888")),
                hovertemplate=(
                    f"<b>{row['ticker']}</b> ({row['sector']})<br>"
                    "Year: %{x}<br>EPS: $%{y:.2f}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        title="EPS Trend — Qualifying Companies",
        xaxis_title="Fiscal Year",
        yaxis_title="Diluted EPS ($)",
        legend_title="Ticker",
        hovermode="x unified",
        height=500,
        template="plotly_white",
    )
    return fig


def _company_bar_chart(row: pd.Series) -> go.Figure:
    """Bar chart for a single company with streak years highlighted in green."""
    history = row["eps_history"]
    if not history:
        return go.Figure()

    years = [h[0] for h in history]
    eps_vals = [h[1] for h in history]
    streak_years = set(
        range(int(row["streak_start_year"]), int(row["streak_end_year"]) + 1)
    )
    colors = ["#2ecc71" if y in streak_years else "#95a5a6" for y in years]

    fig = go.Figure(
        go.Bar(
            x=years,
            y=eps_vals,
            marker_color=colors,
            hovertemplate="Year: %{x}<br>EPS: $%{y:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        title=f"{row['ticker']} — {row['name']}: Annual Diluted EPS",
        xaxis_title="Fiscal Year",
        yaxis_title="Diluted EPS ($)",
        height=400,
        template="plotly_white",
    )
    # Add annotation for streak range
    fig.add_annotation(
        text=(
            f"Streak: {row['streak_start_year']}–{row['streak_end_year']} "
            f"({int(row['streak_length'])} yrs, highlighted in green)"
        ),
        xref="paper",
        yref="paper",
        x=0.01,
        y=0.97,
        showarrow=False,
        font=dict(size=12, color="#2ecc71"),
        align="left",
    )
    return fig


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def _render_sidebar() -> tuple[int, str, str, bool]:
    st.sidebar.title("Controls")

    min_years = st.sidebar.number_input(
        "Minimum Consecutive Years",
        min_value=1,
        max_value=30,
        value=10,
        step=1,
        help="Minimum number of consecutive years with positive AND increasing diluted EPS.",
    )

    sector_options = ["All Sectors"]
    if st.session_state.eps_data:
        sectors = sorted({v["sector"] for v in st.session_state.eps_data.values() if v["sector"]})
        sector_options += sectors

    sector_filter = st.sidebar.selectbox("Filter by GICS Sector", sector_options)

    sort_options = {
        "Streak Length": "streak_length",
        "EPS CAGR": "eps_cagr",
        "Latest EPS": "latest_eps",
        "Ticker A-Z": "ticker",
    }
    sort_label = st.sidebar.selectbox("Sort by", list(sort_options.keys()))
    sort_col = sort_options[sort_label]

    force_refresh = st.sidebar.button("Refresh Data", help="Bust cache and re-fetch from Yahoo Finance")

    # Last updated timestamp
    last_updated = get_last_updated()
    if last_updated:
        try:
            dt = datetime.datetime.fromisoformat(last_updated)
            dt_local = dt.astimezone(tz=None)
            st.sidebar.caption(f"Cache last updated: {dt_local.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        except Exception:
            st.sidebar.caption(f"Cache last updated: {last_updated}")
    else:
        st.sidebar.caption("Cache: not yet populated")

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**Data:** Yahoo Finance via yfinance  \n"
        "**Cache:** Local SQLite (24-hour TTL)"
    )

    return int(min_years), sector_filter, sort_col, force_refresh


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------
def _render_main(df: pd.DataFrame, min_years: int, sort_col: str) -> None:
    st.title("S&P 500 EPS Streak Analysis")

    if df.empty:
        st.warning(
            f"No companies found with {min_years}+ consecutive years of growing diluted EPS. "
            "Try lowering the 'Minimum Consecutive Years' parameter."
        )
        return

    n = len(df)
    end_yr = int(df["streak_end_year"].max()) if not df.empty else datetime.date.today().year - 1
    st.subheader(f"{n} companies with {min_years}+ consecutive years of growing diluted EPS (through {end_yr})")

    # --- Summary metrics ---
    med_streak = df["streak_length"].median()
    med_cagr = df["eps_cagr"].median()
    top_sector = df["sector"].value_counts().idxmax() if not df.empty else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Qualifying Companies", n)
    c2.metric("Median Streak", f"{med_streak:.0f} yrs")
    c3.metric("Median EPS CAGR", f"{med_cagr * 100:.1f}%")
    c4.metric("Top Sector", top_sector)

    st.markdown("---")

    # --- Sort ---
    ascending = sort_col == "ticker"
    df_sorted = df.sort_values(sort_col, ascending=ascending).reset_index(drop=True)

    # --- Display table ---
    display_df = df_sorted[
        ["ticker", "name", "sector", "streak_length", "eps_cagr", "latest_eps",
         "streak_start_year", "streak_end_year"]
    ].copy()
    display_df.columns = [
        "Ticker", "Company", "Sector", "Streak (yrs)", "EPS CAGR",
        "Latest EPS", "Streak Start", "Streak End",
    ]
    display_df["EPS CAGR"] = display_df["EPS CAGR"].apply(lambda x: f"{x * 100:.1f}%")
    display_df["Latest EPS"] = display_df["Latest EPS"].apply(lambda x: f"${x:.2f}")

    st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    # --- EPS Trend Charts ---
    with st.expander("EPS Trend Charts", expanded=False):
        fig_multi = _multi_line_chart(df_sorted)
        st.plotly_chart(fig_multi, use_container_width=True)

    # --- Individual company deep-dive ---
    st.subheader("Company Deep-Dive")
    ticker_options = df_sorted["ticker"].tolist()
    selected_ticker = st.selectbox("Select a company", ticker_options)

    if selected_ticker:
        row = df_sorted[df_sorted["ticker"] == selected_ticker].iloc[0]
        col_a, col_b = st.columns([2, 1])
        with col_a:
            fig_bar = _company_bar_chart(row)
            st.plotly_chart(fig_bar, use_container_width=True)
        with col_b:
            st.markdown(f"**{row['ticker']}** — {row['name']}")
            st.markdown(f"**Sector:** {row['sector']}")
            st.markdown(f"**Streak:** {int(row['streak_length'])} years ({int(row['streak_start_year'])}–{int(row['streak_end_year'])})")
            st.markdown(f"**Earliest Streak EPS:** ${float(row['earliest_streak_eps']):.2f}")
            st.markdown(f"**Latest EPS:** ${float(row['latest_eps']):.2f}")
            st.markdown(f"**EPS CAGR:** {float(row['eps_cagr']) * 100:.1f}%")

    st.markdown("---")

    # --- Failed tickers warning ---
    if st.session_state.failed_tickers:
        with st.expander(f"Tickers with missing EPS data ({len(st.session_state.failed_tickers)})", expanded=False):
            st.warning(
                "The following tickers had no EPS data available and were excluded:\n\n"
                + ", ".join(sorted(st.session_state.failed_tickers))
            )

    # --- About ---
    with st.expander("About this dashboard"):
        st.markdown(
            """
### Methodology

This dashboard identifies S&P 500 companies that have maintained a consecutive streak of:

1. **Positive diluted EPS** — earnings per share > $0 every year in the streak.
2. **Strictly increasing diluted EPS** — each year's EPS is higher than the prior year's.

The streak is measured backwards from the most recent complete fiscal year for which
data is available. Companies must meet **both** criteria every single year in the streak.

**EPS CAGR** is the compound annual growth rate of diluted EPS from the first to the
last year of the qualifying streak.

**Data source:** Yahoo Finance via the [yfinance](https://github.com/ranaroussi/yfinance)
library. Annual income statement data is used; figures may differ slightly from official
filings due to restatements and Yahoo Finance's data normalisation.

**Cache:** Data is cached locally in a SQLite database for 24 hours. Use the
"Refresh Data" button to force a re-fetch.
            """
        )


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------
def main() -> None:
    min_years, sector_filter, sort_col, force_refresh = _render_sidebar()

    # Handle refresh button
    if force_refresh:
        _run_fetch(force=True)
        st.rerun()

    # Auto-load on first run or stale cache
    if not st.session_state.data_loaded:
        _run_fetch(force=False)

    eps_data = st.session_state.eps_data

    if not eps_data:
        st.info("No data loaded yet. Use the Refresh Data button to fetch data.")
        return

    # Run analysis
    end_year = datetime.date.today().year - 1
    with st.spinner("Analyzing EPS streaks…"):
        df = _get_analysis(eps_data, min_years, end_year)

    # Apply sector filter
    if sector_filter != "All Sectors":
        df = df[df["sector"] == sector_filter].reset_index(drop=True)

    _render_main(df, min_years, sort_col)


if __name__ == "__main__":
    main()
