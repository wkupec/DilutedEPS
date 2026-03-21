"""
data_fetcher.py — Fetches S&P 500 constituent list and annual diluted EPS data.

EPS source priority:
  1. SEC EDGAR XBRL API  — free, official, 20+ years of history
  2. yfinance            — fallback for any tickers not found in EDGAR
"""

import time
import logging
from typing import Callable

import pandas as pd
import yfinance as yf
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json"

# SEC requires a descriptive User-Agent with contact info
EDGAR_HEADERS = {"User-Agent": "DilutedEPS-Analyzer research@dilutedeps.local"}

# yfinance uses hyphens; Wikipedia uses dots for some tickers
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
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    html = session.get(SP500_WIKI_URL, timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "constituents"}) or soup.find("table")

    headers = [th.get_text(strip=True) for th in table.find("tr").find_all("th")]
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    df = pd.DataFrame(rows, columns=headers if headers else None)

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


# ---------------------------------------------------------------------------
# EDGAR — CIK lookup
# ---------------------------------------------------------------------------

_cik_map: dict[str, str] | None = None  # ticker -> zero-padded CIK string


def _load_cik_map() -> dict[str, str]:
    """Download the SEC ticker→CIK mapping (cached in memory)."""
    global _cik_map
    if _cik_map is not None:
        return _cik_map

    resp = requests.get(EDGAR_TICKERS_URL, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    raw = resp.json()  # { "0": {cik_str, ticker, title}, ... }

    _cik_map = {
        entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
        for entry in raw.values()
    }
    logger.info("Loaded CIK map: %d tickers", len(_cik_map))
    return _cik_map


def _get_cik(ticker: str) -> str | None:
    """Return zero-padded CIK for a ticker, or None if not found."""
    cik_map = _load_cik_map()
    # Try exact match first, then without exchange suffix (e.g. BRK-B → BRKB)
    return cik_map.get(ticker.upper()) or cik_map.get(ticker.upper().replace("-", ""))


# ---------------------------------------------------------------------------
# EDGAR — EPS fetch
# ---------------------------------------------------------------------------

def _fetch_eps_edgar(cik: str) -> dict[int, float]:
    """
    Fetch annual diluted EPS from SEC EDGAR XBRL API.

    Tries EarningsPerShareDiluted first, then EarningsPerShareBasic as fallback.
    Returns {fiscal_year: eps_float} filtered to annual 10-K filings only.
    """
    for concept in ("EarningsPerShareDiluted", "EarningsPerShareBasic"):
        url = EDGAR_CONCEPT_URL.format(cik=cik, concept=concept)
        try:
            resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("EDGAR concept fetch failed for CIK %s / %s: %s", cik, concept, exc)
            continue

        units = data.get("units", {})
        # EPS values are reported in "USD/shares"
        facts = units.get("USD/shares") or units.get("USD-per-shares") or []

        eps: dict[int, float] = {}
        for fact in facts:
            # Only annual filings; skip amended duplicates by keeping the latest filed
            if fact.get("form") != "10-K":
                continue
            fy = fact.get("fy")
            val = fact.get("val")
            filed = fact.get("filed", "")
            if fy is None or val is None:
                continue
            # Keep the most-recently filed value for each fiscal year
            if fy not in eps or filed > eps.get(f"_{fy}_filed", ""):
                eps[fy] = float(val)
                eps[f"_{fy}_filed"] = filed  # type: ignore[assignment]

        # Strip the sentinel keys
        eps = {k: v for k, v in eps.items() if isinstance(k, int)}

        if eps:
            logger.debug("EDGAR returned %d years for CIK %s via %s", len(eps), cik, concept)
            return eps

    return {}


# ---------------------------------------------------------------------------
# yfinance — EPS fetch (fallback)
# ---------------------------------------------------------------------------

def _fetch_eps_yfinance(ticker: str) -> dict[int, float]:
    """
    Extract annual diluted EPS from yfinance. Used as fallback when EDGAR
    has no data for a ticker.
    """
    eps: dict[int, float] = {}
    try:
        ticker_obj = yf.Ticker(ticker)
        for attr in ("financials", "income_stmt"):
            df: pd.DataFrame | None = getattr(ticker_obj, attr, None)
            if df is None or df.empty:
                continue
            for label in ("Diluted EPS", "Basic EPS", "Diluted Eps", "Basic Eps"):
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
# Public API
# ---------------------------------------------------------------------------

def fetch_all_eps_data(
    tickers_df: pd.DataFrame,
    progress_callback: Callable[[int, int, str], None] | None = None,
    sleep_seconds: float = 0.15,
) -> dict:
    """
    Fetch annual diluted EPS for every ticker in tickers_df.

    Uses EDGAR as the primary source (free, 20+ years), falling back to
    yfinance for any tickers not found in EDGAR.

    Args:
        tickers_df: DataFrame with columns [ticker, name, sector].
        progress_callback: Optional callable(current_idx, total, ticker).
        sleep_seconds: Pause between EDGAR API calls.

    Returns:
        { ticker: { name, sector, eps_by_year: {year: float} } }
    """
    results: dict = {}
    failed: list[str] = []
    edgar_hits = 0
    yf_hits = 0
    total = len(tickers_df)

    for idx, row in enumerate(tickers_df.itertuples(index=False), start=1):
        ticker: str = row.ticker
        name: str = row.name
        sector: str = row.sector

        if progress_callback:
            progress_callback(idx, total, ticker)

        eps_by_year: dict[int, float] = {}
        source = "none"

        # --- Primary: EDGAR ---
        try:
            cik = _get_cik(ticker)
            if cik:
                eps_by_year = _fetch_eps_edgar(cik)
                if eps_by_year:
                    source = "edgar"
                    edgar_hits += 1
        except Exception as exc:
            logger.warning("EDGAR fetch error for %s: %s", ticker, exc)

        # --- Fallback: yfinance ---
        if not eps_by_year:
            try:
                eps_by_year = _fetch_eps_yfinance(ticker)
                if eps_by_year:
                    source = "yfinance"
                    yf_hits += 1
            except Exception as exc:
                logger.warning("yfinance fetch error for %s: %s", ticker, exc)

        if not eps_by_year:
            failed.append(ticker)
            logger.warning("No EPS data found for %s", ticker)

        logger.debug("%s — source=%s years=%d", ticker, source, len(eps_by_year))

        results[ticker] = {
            "name": name,
            "sector": sector,
            "eps_by_year": eps_by_year,
        }

        if idx < total:
            time.sleep(sleep_seconds)

    logger.info(
        "EPS fetch complete: %d EDGAR, %d yfinance, %d failed",
        edgar_hits, yf_hits, len(failed),
    )
    if failed:
        logger.warning("Failed tickers (%d): %s", len(failed), failed)

    return results


def get_failed_tickers(data: dict) -> list[str]:
    """Return tickers that have no EPS data at all."""
    return [t for t, v in data.items() if not v.get("eps_by_year")]
