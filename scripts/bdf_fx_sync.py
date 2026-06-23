#!/usr/bin/env python3
"""
Sync Banque de France daily exchange rates into Pigment.

Fetches daily EUR-based exchange rates from the BdF WEBSTAT SDMX API,
then computes and pushes three rate types into the "01. Exchange rates"
metric of the [AUDAX JUNE 26] Financial Consolidation application:

  - MTD Average rate : mean of daily rates within the target month
  - YTD Average rate : mean of daily rates from Jan 1 of the year to
                       the last day of the target month
  - Closing rate     : last available daily rate of the target month

Usage:
    python bdf_fx_sync.py --period 2026-01
    python bdf_fx_sync.py --period 2026-01 --dry-run

Required environment variable:
    PIGMENT_API_KEY   Pigment API key (Settings > API Keys in Pigment)
"""

import argparse
import os
import sys
import json
from datetime import date, datetime
from calendar import monthrange
from statistics import mean

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BDF_API_BASE = "https://webstat.banque-france.fr/api/data/EXR"

PIGMENT_API_BASE = "https://api.pigment.com/v1"
PIGMENT_APP_ID = "d8d6ad88-359c-476a-b654-bafb1fe4c36f"
PIGMENT_METRIC_ID = "04f62501-fcc3-42c7-8fcf-1d308cae58c2"

# Currencies to sync (ISO code -> Pigment display name)
CURRENCIES = {
    "GBP": "GBP - Pound Sterling",
    "JPY": "JPY - Yen",
    "USD": "USD - US Dollar",
}
EUR_DISPLAY_NAME = "EUR - Euro"

# Pigment dimension names
DIM_RATE_TYPE = "Currency translation rate"
DIM_MONTH     = "Month"
DIM_VERSION   = "Exchange rate version"
DIM_CURRENCY  = "Currency"

PIGMENT_VERSION = "Actual Current Year"

# Pigment rate type labels (must match exactly what's in the dimension list)
RATE_MTD_AVG  = "MTD Average rate"
RATE_YTD_AVG  = "YTD Average rate"
RATE_CLOSING  = "Closing rate"


# ---------------------------------------------------------------------------
# BdF SDMX fetch — daily rates
# ---------------------------------------------------------------------------

def fetch_daily_rates(currency_codes: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """
    Fetch daily spot rates from BdF WEBSTAT (SDMX-JSON).

    Returns { currency_code: { "YYYY-MM-DD": rate_value, ... } }
    Rates are units of foreign currency per 1 EUR.
    Only business days are returned by BdF (weekends/holidays are absent).
    """
    currencies_key = "+".join(currency_codes)
    url = f"{BDF_API_BASE}/D.{currencies_key}.EUR.SP00.A"
    params = {
        "startPeriod": start.isoformat(),
        "endPeriod":   end.isoformat(),
        "format":      "jsondata",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    dataset   = data["dataSets"][0]
    structure = data["structure"]
    series_dims = structure["dimensions"]["series"]
    obs_dims    = structure["dimensions"]["observation"]

    currency_dim = next(d for d in series_dims if d["id"] == "CURRENCY")
    time_dim     = obs_dims[0]

    results: dict[str, dict[str, float]] = {c: {} for c in currency_codes}

    for series_key, series_data in dataset["series"].items():
        parts   = series_key.split(":")
        ccy_idx = int(parts[currency_dim["keyPosition"]])
        ccy     = currency_dim["values"][ccy_idx]["id"]
        if ccy not in results:
            continue
        for obs_idx_str, obs_val in series_data["observations"].items():
            if obs_val[0] is None:
                continue
            obs_date = time_dim["values"][int(obs_idx_str)]["id"]   # "YYYY-MM-DD"
            results[ccy][obs_date] = float(obs_val[0])

    return results


# ---------------------------------------------------------------------------
# Rate computations
# ---------------------------------------------------------------------------

def compute_rates(
    daily: dict[str, float],  # { "YYYY-MM-DD": rate }
    year: int,
    month: int,
) -> dict[str, float | None]:
    """
    From a dict of daily rates (covering Jan 1 → last day of target month),
    compute MTD average, YTD average, and closing rate.
    """
    last_day = monthrange(year, month)[1]
    month_prefix = f"{year}-{month:02d}-"

    # MTD: days within the target month
    mtd_values = [v for k, v in daily.items() if k.startswith(month_prefix)]

    # YTD: all days from Jan 1 through end of target month
    ytd_cutoff = f"{year}-{month:02d}-{last_day:02d}"
    ytd_values = [v for k, v in daily.items() if k <= ytd_cutoff]

    # Closing: last available business day in the target month
    month_days = sorted(k for k in daily if k.startswith(month_prefix))
    closing = daily[month_days[-1]] if month_days else None

    return {
        RATE_MTD_AVG: mean(mtd_values) if mtd_values else None,
        RATE_YTD_AVG: mean(ytd_values) if ytd_values else None,
        RATE_CLOSING: closing,
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
    Push exchange rate rows to Pigment.

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
    parser = argparse.ArgumentParser(description="Sync BdF FX rates to Pigment")
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

    # Fetch daily rates from Jan 1 of the year to the last day of the target month
    # (needed for YTD calculation)
    fetch_start = date(year, 1, 1)
    fetch_end   = date(year, month, last_day)

    print(f"Period  : {args.period} → Pigment label '{pigment_month}'")
    print(f"Fetching: {fetch_start} → {fetch_end} ({len(CURRENCIES)} currencies)")

    currency_codes = list(CURRENCIES.keys())
    all_daily = fetch_daily_rates(currency_codes, fetch_start, fetch_end)

    rows: list[dict] = []

    for code, display_name in CURRENCIES.items():
        daily = all_daily.get(code, {})
        if not daily:
            print(f"  WARNING: no daily data found for {code}, skipping.", file=sys.stderr)
            continue

        rates = compute_rates(daily, year, month)
        for rate_label, value in rates.items():
            if value is None:
                print(f"  WARNING: could not compute {rate_label} for {code}", file=sys.stderr)
                continue
            print(f"  {code:3s}  {rate_label:<20s}  {value:.6f}")
            rows.append({
                "dimension_values": [rate_label, pigment_month, PIGMENT_VERSION, display_name],
                "metric_value": round(value, 6),
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
