"""
data_fetcher.py — Fetches S&P 500 constituent list and annual diluted EPS data.

Primary source: SEC EDGAR XBRL API (https://data.sec.gov) — free, no API key,
20+ years of history for most S&P 500 companies.
Fallback source: yfinance (Yahoo Finance) — used when EDGAR returns no data.
"""

import time
import logging
from typing import Callable

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# SEC requires a descriptive User-Agent header
_EDGAR_HEADERS = {
    "User-Agent": "EPS-Streak-Analyzer/1.0 (research tool; contact@example.com)",
    "Accept-Encoding": "gzip, deflate",
}

_EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Wikipedia uses dots; yfinance and EDGAR lookups use hyphens
_TICKER_FIXES = {
    "BRK.B": "BRK-B",
    "BRK.A": "BRK-A",
    "BF.B": "BF-B",
    "BF.A": "BF-A",
}


def _fix_ticker(ticker: str) -> str:
    return _TICKER_FIXES.get(ticker, ticker.replace(".", "-"))


# ---------------------------------------------------------------------------
# S&P 500 constituent list
# ---------------------------------------------------------------------------

def fetch_sp500_tickers() -> pd.DataFrame:
    """
    Scrape the S&P 500 constituent table from Wikipedia.

    Returns a DataFrame with columns: ticker, name, sector.
    """
    try:
        tables = pd.read_html(SP500_WIKI_URL, attrs={"id": "constituents"})
        df = tables[0]
    except Exception:
        tables = pd.read_html(SP500_WIKI_URL)
        df = tables[0]

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
        raise ValueError(
            f"Could not find columns {missing} in Wikipedia table. "
            f"Available: {list(df.columns)}"
        )

    df = df[["ticker", "name", "sector"]].copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().apply(_fix_ticker)
    df["name"] = df["name"].astype(str).str.strip()
    df["sector"] = df["sector"].astype(str).str.strip()
    df = df.drop_duplicates(subset="ticker")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# SEC EDGAR helpers
# ---------------------------------------------------------------------------

def _build_cik_map() -> dict[str, str]:
    """
    Download SEC's full ticker→CIK mapping.

    Returns { "AAPL": "0000320193", ... } with zero-padded 10-digit CIKs.
    """
    resp = requests.get(_EDGAR_TICKERS_URL, headers=_EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    raw: dict = resp.json()
    # Structure: { "0": { "cik_str": 320193, "ticker": "AAPL", "title": "..." }, ... }
    mapping: dict[str, str] = {}
    for entry in raw.values():
        ticker = str(entry.get("ticker", "")).upper().strip()
        cik = str(entry.get("cik_str", "")).strip()
        if ticker and cik:
            mapping[ticker] = cik.zfill(10)
    return mapping


def _fetch_edgar_eps(cik: str) -> dict[int, float]:
    """
    Fetch annual diluted EPS from SEC EDGAR XBRL company facts.

    Looks for us-gaap/EarningsPerShareDiluted in annual 10-K filings.
    Returns {fiscal_year: eps_float}.
    """
    url = _EDGAR_FACTS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=_EDGAR_HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return {}
        raise

    facts = resp.json().get("facts", {})

    # Try us-gaap first, then dei as a last resort
    for taxonomy in ("us-gaap", "ifrs-full"):
        taxonomy_facts = facts.get(taxonomy, {})
        eps_concept = taxonomy_facts.get("EarningsPerShareDiluted", {})
        if not eps_concept:
            continue

        # Units are typically "USD/shares"
        units = eps_concept.get("units", {})
        filings_list = units.get("USD/shares") or units.get("USD") or []

        if not filings_list:
            continue

        eps: dict[int, float] = {}
        for filing in filings_list:
            form = filing.get("form", "")
            # Accept 10-K annual filings only
            if form not in ("10-K", "10-K405", "10-KT"):
                continue
            # Prefer the fiscal-year field; fall back to end-date year
            fy = filing.get("fy")
            if fy is None:
                try:
                    fy = int(str(filing.get("end", ""))[:4])
                except (ValueError, TypeError):
                    continue
            try:
                val = float(filing["val"])
            except (KeyError, TypeError, ValueError):
                continue

            # If multiple filings for the same FY (e.g. amendments), keep the
            # latest-filed one
            if fy not in eps or filing.get("filed", "") > eps.get(f"_filed_{fy}", ""):
                eps[int(fy)] = val
                eps[f"_filed_{fy}"] = filing.get("filed", "")  # type: ignore[assignment]

        # Strip the helper keys
        return {k: v for k, v in eps.items() if isinstance(k, int)}

    return {}


# ---------------------------------------------------------------------------
# yfinance fallback
# ---------------------------------------------------------------------------

def _fetch_yfinance_eps(ticker: str) -> dict[int, float]:
    """
    Extract annual diluted EPS from yfinance as a fallback source.
    Returns {fiscal_year: eps_float}.
    """
    eps: dict[int, float] = {}
    try:
        ticker_obj = yf.Ticker(ticker)
        for attr in ("financials", "income_stmt"):
            df = getattr(ticker_obj, attr, None)
            if df is None or df.empty:
                continue
            for label in ("Diluted EPS", "Diluted Eps", "Basic EPS", "Basic Eps"):
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
    except Exception as exc:
        logger.debug("yfinance fallback failed for %s: %s", ticker, exc)
    return eps


# ---------------------------------------------------------------------------
# Main fetch orchestrator
# ---------------------------------------------------------------------------

def fetch_all_eps_data(
    tickers_df: pd.DataFrame,
    progress_callback: Callable[[int, int, str], None] | None = None,
    sleep_seconds: float = 0.15,
) -> dict:
    """
    Fetch annual diluted EPS for every ticker.

    Strategy:
      1. Try SEC EDGAR XBRL API (20+ years of history).
      2. Fall back to yfinance if EDGAR returns fewer than 2 years.

    Args:
        tickers_df: DataFrame with columns [ticker, name, sector].
        progress_callback: Optional callable(current_idx, total, ticker).
        sleep_seconds: Pause between EDGAR API calls (SEC asks ≤10 req/s).

    Returns:
        { ticker: { name, sector, eps_by_year: {year: float} } }
    """
    # Build CIK map once up front
    cik_map: dict[str, str] = {}
    try:
        logger.info("Downloading SEC ticker→CIK map…")
        cik_map = _build_cik_map()
        logger.info("CIK map loaded: %d entries", len(cik_map))
    except Exception as exc:
        logger.warning("Could not load EDGAR CIK map, will use yfinance only: %s", exc)

    results: dict = {}
    failed: list[str] = []
    total = len(tickers_df)

    for idx, row in enumerate(tickers_df.itertuples(index=False), start=1):
        ticker: str = row.ticker
        name: str = row.name
        sector: str = row.sector

        if progress_callback:
            progress_callback(idx, total, ticker)

        eps_by_year: dict[int, float] = {}
        source = "none"

        # --- Primary: SEC EDGAR ---
        cik = cik_map.get(ticker) or cik_map.get(ticker.replace("-", "."))
        if cik:
            try:
                edgar_eps = _fetch_edgar_eps(cik)
                if len(edgar_eps) >= 2:
                    eps_by_year = edgar_eps
                    source = "edgar"
            except Exception as exc:
                logger.debug("EDGAR failed for %s (CIK %s): %s", ticker, cik, exc)

        # --- Fallback: yfinance ---
        if not eps_by_year:
            try:
                eps_by_year = _fetch_yfinance_eps(ticker)
                if eps_by_year:
                    source = "yfinance"
            except Exception as exc:
                logger.warning("yfinance also failed for %s: %s", ticker, exc)

        if not eps_by_year:
            failed.append(ticker)
            logger.debug("No EPS data for %s", ticker)

        results[ticker] = {
            "name": name,
            "sector": sector,
            "eps_by_year": eps_by_year,
            "source": source,
        }

        if idx < total:
            time.sleep(sleep_seconds)

    if failed:
        logger.warning("No EPS data for %d tickers: %s", len(failed), failed)

    return results


def get_failed_tickers(data: dict) -> list[str]:
    """Return tickers that have no EPS data at all."""
    return [t for t, v in data.items() if not v.get("eps_by_year")]
