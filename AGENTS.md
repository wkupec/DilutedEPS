# DilutedEPS

## Architecture
- Language: Python 3.10+
- App type: Streamlit dashboard for analyzing S&P 500 diluted EPS growth streaks
- Key dependencies: `streamlit`, `pandas`, `plotly`, `requests`, `beautifulsoup4`, `lxml`, `yfinance`
- Data storage: local SQLite cache in `eps_analyzer/eps_cache.db`
- Directory layout:
  - `eps_analyzer/app.py` - Streamlit entry point and UI
  - `eps_analyzer/analyzer.py` - EPS streak analysis logic
  - `eps_analyzer/data_fetcher.py` - Wikipedia, SEC EDGAR, and yfinance data retrieval
  - `eps_analyzer/cache.py` - SQLite cache helpers
  - `eps_analyzer/README.md` - setup and product notes
  - `eps_analyzer/requirements.txt` - Python dependencies

## Conventions
- Keep changes small and match the existing straightforward module split: UI in `app.py`, business logic in `analyzer.py`, fetching in `data_fetcher.py`, caching in `cache.py`.
- Prefer simple functions over new abstractions unless the change clearly needs them.
- Run the app locally with `streamlit run eps_analyzer/app.py`.
- If you change data fetching behavior, preserve the SEC EDGAR first / yfinance fallback approach unless the task explicitly requires otherwise.

## Important
- Never commit `.env` files.
- Never commit local cache or generated data files such as `*.db` or `*.sqlite`.
- Do not hardcode secrets or API keys in source files.
- Keep repo instructions at the project root; keep product-facing setup notes in `eps_analyzer/README.md`.
