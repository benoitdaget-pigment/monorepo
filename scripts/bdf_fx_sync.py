#!/usr/bin/env python3
"""
Sync official EUR exchange rates into Pigment [AUDAX Financial Consolidation].

Source: Frankfurter API (api.frankfurter.app) which distributes the official
ECB/BdF EUR reference rates published each business day.

Three rate types are computed and pushed into the "01. Exchange rates" metric:

  - MTD Average rate : mean of daily rates within the target month
  - YTD Average rate : mean of daily rates from Jan 1 of the year to
                       the last business day of the target month
  - Closing rate     : last available daily rate of the target month

Usage:
    python bdf_fx_sync.py --period 2026-01
    python bdf_fx_sync.py --period 2026-01 --dry-run

Required environment variable:
    PIGMENT_API_KEY   Pigment API key (Settings > API Keys in Pigment)
"""

import argparse
import json
import os
import sys
from calendar import monthrange
from datetime import date
from statistics import mean

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FRANKFURTER_API = "https://api.frankfurter.app"

PIGMENT_API_BASE = "https://api.pigment.com/v1"
PIGMENT_APP_ID   = "d8d6ad88-359c-476a-b654-bafb1fe4c36f"
PIGMENT_METRIC_ID = "04f62501-fcc3-42c7-8fcf-1d308cae58c2"

# Currencies to sync (ISO code -> Pigment display name)
CURRENCIES = {
    "GBP": "GBP - Pound Sterling",
    "JPY": "JPY - Yen",
    "USD": "USD - US Dollar",
}
EUR_DISPLAY_NAME = "EUR - Euro"

# Pigment dimension names (order must match what the API expects)
DIM_RATE_TYPE = "Currency translation rate"
DIM_MONTH     = "Month"
DIM_VERSION   = "Exchange rate version"
DIM_CURRENCY  = "Currency"

PIGMENT_VERSION = "Actual Current Year"

# Pigment rate type labels
RATE_MTD_AVG = "MTD Average rate"
RATE_YTD_AVG = "YTD Average rate"
RATE_CLOSING = "Closing rate"


# ---------------------------------------------------------------------------
# Frankfurter fetch — daily rates
# ---------------------------------------------------------------------------

def fetch_daily_rates(start: date, end: date) -> dict[str, dict[str, float]]:
    """
    Fetch daily EUR-based rates from the Frankfurter API.

    Returns { "YYYY-MM-DD": { "GBP": x, "JPY": y, "USD": z } }
    Only business days are returned (weekends/holidays absent).
    """
    symbols = ",".join(CURRENCIES.keys())
    url = f"{FRANKFURTER_API}/{start}..{end}"
    params = {"from": "EUR", "to": symbols}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("rates", {})


# ---------------------------------------------------------------------------
# Rate computations
# ---------------------------------------------------------------------------

def compute_rates(
    all_daily: dict[str, dict[str, float]],
    currency: str,
    year: int,
    month: int,
) -> dict[str, float | None]:
    """
    Compute MTD average, YTD average, and closing rate for one currency.

    all_daily covers Jan 1 of the year through the last day of target month.
    """
    month_prefix = f"{year}-{month:02d}-"
    last_day = monthrange(year, month)[1]
    ytd_start  = f"{year}-01-01"
    ytd_cutoff = f"{year}-{month:02d}-{last_day:02d}"

    mtd_vals = [v[currency] for d, v in all_daily.items()
                if d.startswith(month_prefix) and currency in v]
    # YTD must be bounded on both sides: the data source can return a trailing
    # observation from the previous December, which must not leak into the average.
    ytd_vals = [v[currency] for d, v in all_daily.items()
                if ytd_start <= d <= ytd_cutoff and currency in v]

    month_days = sorted(d for d in all_daily if d.startswith(month_prefix))
    closing = all_daily[month_days[-1]].get(currency) if month_days else None

    return {
        RATE_MTD_AVG: round(mean(mtd_vals), 6) if mtd_vals else None,
        RATE_YTD_AVG: round(mean(ytd_vals), 6) if ytd_vals else None,
        RATE_CLOSING: round(closing, 6) if closing is not None else None,
    }


# ---------------------------------------------------------------------------
# Pigment month label
# ---------------------------------------------------------------------------

def period_to_pigment_month(year: int, month: int) -> str:
    """Convert year/month to the Pigment month label, e.g. "Jan-26"."""
    return date(year, month, 1).strftime("%b-%y")


# ---------------------------------------------------------------------------
# Pigment injection
# ---------------------------------------------------------------------------

def push_to_pigment(rows: list[dict], api_key: str, dry_run: bool = False) -> None:
    """
    Push exchange rate rows to Pigment via the REST API.

    Each row:
        { "dimension_values": [rate_type, month, version, currency],
          "metric_value": float }
    """
    payload = {
        "metricId": PIGMENT_METRIC_ID,
        "dimensionDisplayNames": [DIM_RATE_TYPE, DIM_MONTH, DIM_VERSION, DIM_CURRENCY],
        "rows": [
            {
                "dimensionValues": r["dimension_values"],
                "metricValue": {"type": "Decimal", "value": r["metric_value"]},
            }
            for r in rows
        ],
    }

    if dry_run:
        print("[DRY RUN] Payload that would be sent to Pigment:")
        print(json.dumps(payload, indent=2))
        return

    url = (
        f"{PIGMENT_API_BASE}/applications/{PIGMENT_APP_ID}"
        f"/metrics/{PIGMENT_METRIC_ID}/inputs"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    print(f"[OK] {len(rows)} values pushed to Pigment.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync EUR FX rates to Pigment")
    parser.add_argument(
        "--period",
        default=date.today().strftime("%Y-%m"),
        help="Target month in YYYY-MM format (default: current month)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload without writing to Pigment",
    )
    args = parser.parse_args()

    api_key = os.environ.get("PIGMENT_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: PIGMENT_API_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    year, month = map(int, args.period.split("-"))
    pigment_month = period_to_pigment_month(year, month)
    last_day = monthrange(year, month)[1]

    fetch_start = date(year, 1, 1)
    fetch_end   = date(year, month, last_day)

    print(f"Period  : {args.period} → Pigment label '{pigment_month}'")
    print(f"Fetching: {fetch_start} → {fetch_end}")

    all_daily = fetch_daily_rates(fetch_start, fetch_end)
    print(f"  → {len(all_daily)} business days retrieved")

    rows: list[dict] = []

    for code, display_name in CURRENCIES.items():
        rates = compute_rates(all_daily, code, year, month)
        for rate_label, value in rates.items():
            if value is None:
                print(f"  WARNING: could not compute {rate_label} for {code}", file=sys.stderr)
                continue
            print(f"  {code}  {rate_label:<20s}  {value:.6f}")
            rows.append({
                "dimension_values": [rate_label, pigment_month, PIGMENT_VERSION, display_name],
                "metric_value": value,
            })

    # EUR is always 1.0 for all rate types
    for rate_label in (RATE_MTD_AVG, RATE_YTD_AVG, RATE_CLOSING):
        print(f"  EUR  {rate_label:<20s}  1.000000")
        rows.append({
            "dimension_values": [rate_label, pigment_month, PIGMENT_VERSION, EUR_DISPLAY_NAME],
            "metric_value": 1.0,
        })

    print(f"\nTotal: {len(rows)} values to push.")
    push_to_pigment(rows, api_key, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
