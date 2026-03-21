# S&P 500 Diluted EPS Streak Analyzer

An interactive Streamlit dashboard that identifies S&P 500 companies with N or more
consecutive years of **positive and strictly increasing** diluted EPS.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> Python 3.10+ is required.

### 2. Run the app

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`.

---

## How it works

### Streak logic

A company qualifies if, for every year in a consecutive window ending at the most
recent complete fiscal year:

1. **Diluted EPS > 0** (profitable every single year in the streak).
2. **Diluted EPS strictly greater than the prior year** (year-over-year growth every year).

The dashboard lets you set the minimum streak length (default: 10 years).

### EPS CAGR

The compound annual growth rate of diluted EPS from the first to the last year of
the qualifying streak:

```
CAGR = (EPS_end / EPS_start) ^ (1 / (streak_length - 1)) - 1
```

---

## Data sources

- **S&P 500 constituent list** — scraped from the
  [Wikipedia S&P 500 article](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)
  using `pandas.read_html`.
- **Annual diluted EPS (primary)** — [SEC EDGAR XBRL API](https://data.sec.gov).
  Uses the `EarningsPerShareDiluted` concept from each company's annual 10-K filing.
  Free, no API key needed, typically 20+ years of history. No scraping — structured
  XBRL data directly from official filings.
- **Annual diluted EPS (fallback)** — [Yahoo Finance via yfinance](https://github.com/ranaroussi/yfinance).
  Used only when EDGAR returns fewer than 2 years of data for a ticker (~4 years max).

The sidebar displays a source breakdown (EDGAR / yfinance / missing) after each fetch.

> **Disclaimer:** EDGAR data reflects what companies reported in their official filings;
> however, restatements, fiscal year shifts, and XBRL tagging differences can
> occasionally cause discrepancies. Do not use this tool for investment decisions
> without independently verifying the underlying data.

---

## Caching

EPS data is stored locally in a SQLite database (`eps_analyzer/eps_cache.db`).

- **TTL:** 24 hours. If the cache is less than 24 hours old the app loads instantly
  without any network calls.
- **Force refresh:** Click the **"Refresh Data"** button in the sidebar to delete the
  cache and re-fetch all data from Yahoo Finance. This typically takes several minutes
  for all ~500 tickers.

---

## Dashboard controls

| Control | Description |
|---|---|
| Minimum Consecutive Years | Minimum streak length (default: 10, range: 1–30) |
| Filter by GICS Sector | Narrow results to one sector |
| Sort by | Streak Length / EPS CAGR / Latest EPS / Ticker A-Z |
| Refresh Data | Force cache bust and live re-fetch |

---

## Project structure

```
eps_analyzer/
├── app.py              # Streamlit dashboard entry point
├── data_fetcher.py     # S&P 500 list + yfinance EPS data
├── analyzer.py         # Streak analysis logic
├── cache.py            # SQLite caching layer
├── requirements.txt
└── README.md
```
