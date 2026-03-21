"""
data_fetcher.py — Fetches S&P 500 constituent list and annual diluted EPS data.
"""

import time
import logging
from typing import Callable

import pandas as pd
import yfinance as yf
import requests

logger = logging.getLogger(__name__)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# yfinance uses hyphens; Wikipedia uses dots for some tickers
_TICKER_FIXES = {
    "BRK.B": "BRK-B",
    "BRK.A": "BRK-A",
    "BF.B": "BF-B",
    "BF.A": "BF-A",
}


def _fix_ticker(ticker: str) -> str:
    return _TICKER_FIXES.get(ticker, ticker.replace(".", "-"))


def fetch_sp500_tickers() -> pd.DataFrame:
    """
    Scrape the S&P 500 constituent table from Wikipedia.

    Returns a DataFrame with columns: ticker, name, sector.
    """
    try:
        tables = pd.read_html(SP500_WIKI_URL, attrs={"id": "constituents"})
        df = tables[0]
    except Exception:
        # Fallback: grab first table on the page
        tables = pd.read_html(SP500_WIKI_URL)
        df = tables[0]

    # Normalise column names across Wikipedia revisions
    col_map = {}
    for col in df.columns:
        low = col.lower()
        if "symbol" in low or "ticker" in low:
            col_map[col] = "ticker"
        elif "security" in low or "name" in low or "company" in low:
            col_map[col] = "name"
        elif "sector" in low or "gics sector" in low:
            col_map[col] = "sector"
    df = df.rename(columns=col_map)

    needed = {"ticker", "name", "sector"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"Could not find columns {missing} in Wikipedia table. "
                         f"Available: {list(df.columns)}")

    df = df[["ticker", "name", "sector"]].copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().apply(_fix_ticker)
    df["name"] = df["name"].astype(str).str.strip()
    df["sector"] = df["sector"].astype(str).str.strip()
    df = df.drop_duplicates(subset="ticker")
    return df.reset_index(drop=True)


def _extract_diluted_eps(ticker_obj: yf.Ticker) -> dict[int, float]:
    """
    Extract annual diluted EPS from a yfinance Ticker object.

    Tries multiple attribute paths for resilience across yfinance versions.
    Returns {fiscal_year: eps_float}.
    """
    eps: dict[int, float] = {}

    # --- Primary path: financials / income_stmt ---
    for attr in ("financials", "income_stmt"):
        df: pd.DataFrame | None = getattr(ticker_obj, attr, None)
        if df is None or df.empty:
            continue

        # Row labels vary by yfinance version
        for label in (
            "Diluted EPS",
            "Basic EPS",          # fallback
            "Diluted Eps",
            "Basic Eps",
        ):
            # yfinance sometimes has the label as index, sometimes transposed
            if label in df.index:
                row = df.loc[label]
                for col in row.index:
                    try:
                        year = pd.Timestamp(col).year
                        val = float(row[col])
                        if pd.notna(val):
                            eps[year] = val
                    except Exception:
                        pass
                if eps:
                    return eps

    # --- Fallback: earnings dict / earnings DataFrame ---
    try:
        earnings = ticker_obj.earnings
        if isinstance(earnings, pd.DataFrame) and not earnings.empty:
            if "Earnings" in earnings.columns:
                # Compute EPS proxy: Earnings / shares (not ideal but better than nothing)
                pass  # skip unreliable proxy
    except Exception:
        pass

    # --- Fallback: info dict earningsPerShare ---
    try:
        info = ticker_obj.info or {}
        eps_val = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
        if eps_val is not None:
            import datetime
            year = datetime.date.today().year - 1
            eps[year] = float(eps_val)
    except Exception:
        pass

    return eps


def fetch_all_eps_data(
    tickers_df: pd.DataFrame,
    progress_callback: Callable[[int, int, str], None] | None = None,
    sleep_seconds: float = 0.3,
) -> dict:
    """
    Fetch annual diluted EPS for every ticker in tickers_df.

    Args:
        tickers_df: DataFrame with columns [ticker, name, sector].
        progress_callback: Optional callable(current_idx, total, ticker).
        sleep_seconds: Pause between API calls to avoid rate-limiting.

    Returns:
        { ticker: { name, sector, eps_by_year: {year: float} } }
    """
    results: dict = {}
    failed: list[str] = []
    total = len(tickers_df)

    for idx, row in enumerate(tickers_df.itertuples(index=False), start=1):
        ticker: str = row.ticker
        name: str = row.name
        sector: str = row.sector

        if progress_callback:
            progress_callback(idx, total, ticker)

        try:
            yf_ticker = yf.Ticker(ticker)
            eps_by_year = _extract_diluted_eps(yf_ticker)
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", ticker, exc)
            eps_by_year = {}
            failed.append(ticker)

        results[ticker] = {
            "name": name,
            "sector": sector,
            "eps_by_year": eps_by_year,
        }

        if idx < total:
            time.sleep(sleep_seconds)

    if failed:
        logger.warning("Failed tickers (%d): %s", len(failed), failed)

    return results


def get_failed_tickers(data: dict) -> list[str]:
    """Return tickers that have no EPS data at all."""
    return [t for t, v in data.items() if not v.get("eps_by_year")]
