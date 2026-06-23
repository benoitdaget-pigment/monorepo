#!/usr/bin/env python3
"""
Sync Banque de France monthly exchange rates into Pigment.

Fetches EUR-based exchange rates (average + closing) from the BdF WEBSTAT
SDMX API and pushes them into the "01. Exchange rates" metric of the
[AUDAX JUNE 26] Financial Consolidation application.

Usage:
    python bdf_fx_sync.py --period 2026-01
    python bdf_fx_sync.py --period 2026-01 --dry-run

Required environment variables:
    PIGMENT_API_KEY   Pigment API key (generate from Pigment Settings > API)
"""

import argparse
import os
import sys
from datetime import datetime, date
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

# EUR is always 1.0 — no BdF call needed
EUR_DISPLAY_NAME = "EUR - Euro"

# BdF rate types -> Pigment "Currency translation rate" dimension values
RATE_TYPE_MAP = {
    "A": "Average rate",   # Monthly average
    "E": "Closing rate",   # End-of-period (last trading day of the month)
}

# Pigment dimension names (order must match what set_metric_input expects)
DIM_RATE_TYPE = "Currency translation rate"
DIM_MONTH = "Month"
DIM_VERSION = "Exchange rate version"
DIM_CURRENCY = "Currency"

PIGMENT_VERSION = "Actual Current Year"

# ---------------------------------------------------------------------------
# BdF SDMX fetch
# ---------------------------------------------------------------------------

def fetch_bdf_rates(currency_codes: list[str], period: str, rate_type: str) -> dict[str, float]:
    """
    Fetch rates from BdF WEBSTAT SDMX API.

    Returns dict: { currency_code: rate_value }
    Rates are expressed as units of foreign currency per 1 EUR.
    """
    currencies_key = "+".join(currency_codes)
    url = f"{BDF_API_BASE}/M.{currencies_key}.EUR.SP00.{rate_type}"
    params = {
        "startPeriod": period,
        "endPeriod": period,
        "format": "jsondata",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Navigate the SDMX-JSON structure
    dataset = data["dataSets"][0]
    structure = data["structure"]
    series_dims = structure["dimensions"]["series"]
    obs_dims = structure["dimensions"]["observation"]

    currency_dim = next(d for d in series_dims if d["id"] == "CURRENCY")
    time_dim = obs_dims[0]

    results = {}
    for series_key, series_data in dataset["series"].items():
        parts = series_key.split(":")
        ccy_idx = int(parts[currency_dim["keyPosition"]])
        ccy_code = currency_dim["values"][ccy_idx]["id"]

        for obs_idx_str, obs_val in series_data["observations"].items():
            obs_period = time_dim["values"][int(obs_idx_str)]["id"]
            if obs_period == period and obs_val[0] is not None:
                results[ccy_code] = float(obs_val[0])

    return results


# ---------------------------------------------------------------------------
# Pigment month label resolution
# ---------------------------------------------------------------------------

def period_to_pigment_month(period: str) -> str:
    """
    Convert "2026-01" to the label Pigment uses for January 2026.
    Pigment typically labels months as "Jan-26".
    """
    dt = datetime.strptime(period, "%Y-%m")
    return dt.strftime("%b-%y")  # e.g. "Jan-26"


# ---------------------------------------------------------------------------
# Pigment injection
# ---------------------------------------------------------------------------

def push_to_pigment(rows: list[dict], api_key: str, dry_run: bool = False) -> None:
    """
    Push exchange rate rows to Pigment via the REST API.

    Each row is:
        {
            "dimension_values": [rate_type, month, version, currency],
            "metric_value": float
        }
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
        print("[DRY RUN] Would push the following payload to Pigment:")
        import json
        print(json.dumps(payload, indent=2))
        return

    url = f"{PIGMENT_API_BASE}/applications/{PIGMENT_APP_ID}/metrics/{PIGMENT_METRIC_ID}/inputs"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
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
        help="Month to sync in YYYY-MM format (default: current month)",
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

    period = args.period
    pigment_month = period_to_pigment_month(period)
    currency_codes = list(CURRENCIES.keys())

    print(f"Fetching BdF rates for period {period} ({pigment_month}) ...")

    rows = []

    for bdf_type, pigment_rate_type in RATE_TYPE_MAP.items():
        print(f"  → {pigment_rate_type} (BdF type {bdf_type}) ...")
        rates = fetch_bdf_rates(currency_codes, period, bdf_type)

        # EUR is always 1.0 vs itself
        rows.append({
            "dimension_values": [pigment_rate_type, pigment_month, PIGMENT_VERSION, EUR_DISPLAY_NAME],
            "metric_value": 1.0,
        })

        for code, display_name in CURRENCIES.items():
            if code not in rates:
                print(f"    WARNING: no rate found for {code}, skipping.", file=sys.stderr)
                continue
            value = rates[code]
            print(f"    EUR/{code} = {value}")
            rows.append({
                "dimension_values": [pigment_rate_type, pigment_month, PIGMENT_VERSION, display_name],
                "metric_value": value,
            })

    print(f"\nTotal: {len(rows)} values to push.")
    push_to_pigment(rows, api_key, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
