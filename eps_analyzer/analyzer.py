"""
analyzer.py — Core streak analysis logic for diluted EPS data.
"""

import math
import datetime
from typing import Optional

import pandas as pd


def _compute_cagr(start_val: float, end_val: float, years: int) -> float:
    """Compound Annual Growth Rate over `years` periods."""
    if years <= 0 or start_val <= 0 or end_val <= 0:
        return 0.0
    return (end_val / start_val) ** (1.0 / years) - 1.0


def find_eps_streak_companies(
    eps_data: dict,
    min_consecutive_years: int = 10,
    end_year: Optional[int] = None,
) -> pd.DataFrame:
    """
    Find companies with N consecutive years of positive, strictly-increasing diluted EPS.

    Args:
        eps_data: { ticker: { name, sector, eps_by_year: {year: float} } }
        min_consecutive_years: Minimum streak length to qualify.
        end_year: Last fiscal year to consider (default: current year - 1).

    Returns:
        DataFrame with columns:
            ticker, name, sector, streak_length, streak_start_year, streak_end_year,
            eps_cagr, latest_eps, earliest_streak_eps, eps_history
    """
    if end_year is None:
        end_year = datetime.date.today().year - 1

    records = []

    for ticker, info in eps_data.items():
        name = info.get("name", ticker)
        sector = info.get("sector", "Unknown")
        raw_eps = info.get("eps_by_year", {})

        if not raw_eps:
            continue

        # Build sorted list of (year, eps) up to end_year
        sorted_eps = sorted(
            ((int(y), float(v)) for y, v in raw_eps.items() if int(y) <= end_year),
            key=lambda x: x[0],
        )

        if not sorted_eps:
            continue

        # Build a lookup for quick access
        eps_map: dict[int, float] = dict(sorted_eps)
        years_available = sorted([y for y in eps_map])

        # Walk backwards from end_year to find the current streak
        streak_length = 0
        current_year = end_year

        # Find the most recent year we actually have data for
        while current_year not in eps_map and current_year > years_available[0]:
            current_year -= 1

        if current_year not in eps_map:
            continue

        # The streak ends at `current_year`
        streak_end = current_year

        # Walk backwards: each prior year must be positive AND less than the next year
        prev_eps = eps_map[current_year]
        if prev_eps <= 0:
            # Latest known EPS is non-positive — streak is 0
            continue

        streak_length = 1

        for y in range(current_year - 1, years_available[0] - 1, -1):
            if y not in eps_map:
                break  # gap in data — streak ends
            cur_eps = eps_map[y]
            if cur_eps <= 0 or cur_eps >= prev_eps:
                break  # not positive or not strictly increasing toward the future
            streak_length += 1
            prev_eps = cur_eps

        if streak_length < min_consecutive_years:
            continue

        streak_start = streak_end - streak_length + 1
        earliest_eps = eps_map.get(streak_start, prev_eps)
        latest_eps = eps_map[streak_end]

        cagr = _compute_cagr(earliest_eps, latest_eps, streak_length - 1)

        # Full EPS history for charting (all available years)
        eps_history = sorted_eps  # list of (year, eps)

        records.append(
            {
                "ticker": ticker,
                "name": name,
                "sector": sector,
                "streak_length": streak_length,
                "streak_start_year": streak_start,
                "streak_end_year": streak_end,
                "eps_cagr": cagr,
                "latest_eps": latest_eps,
                "earliest_streak_eps": earliest_eps,
                "eps_history": eps_history,
            }
        )

    if not records:
        return pd.DataFrame(
            columns=[
                "ticker", "name", "sector", "streak_length",
                "streak_start_year", "streak_end_year",
                "eps_cagr", "latest_eps", "earliest_streak_eps", "eps_history",
            ]
        )

    df = pd.DataFrame(records)
    df = df.sort_values("streak_length", ascending=False).reset_index(drop=True)
    return df
