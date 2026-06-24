#!/usr/bin/env python3
"""
Sync official EUR exchange rates into Pigment [AUDAX Financial Consolidation].

Source: Frankfurter API (api.frankfurter.app) which distributes the official
ECB/BdF EUR reference rates published each business day.

Three rate types are computed and pushed into the "INP_FX_Exchange rates"
input metric (Data Hub app):

  - MTD Average rate : mean of daily rates within the target month
  - YTD Average rate : mean of daily rates from Jan 1 of the year to
                       the last business day of the target month
  - Closing rate     : last available daily rate of the target month

Data is delivered to Pigment through the Import API (CSV push). Pigment has
no direct "write metric value" endpoint; instead a CSV is POSTed to a
pre-configured Import Configuration which routes the data to the target
metric. See https://kb.pigment.com/docs/trigger-import-apis

Usage:
    python bdf_fx_sync.py --period 2026-01
    python bdf_fx_sync.py --period 2026-01 --dry-run

Required environment variables:
    PIGMENT_API_KEY            Pigment API key (Integrations > API Keys)
    PIGMENT_IMPORT_CONFIG_ID   Import Configuration ID of the import block
                               feeding INP_FX_Exchange rates
"""

import argparse
import csv
import io
import os
import sys
import time
from calendar import monthrange
from datetime import date
from statistics import mean

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FRANKFURTER_API = "https://api.frankfurter.app"

# Pigment Import API (https://kb.pigment.com/docs/trigger-import-apis)
PIGMENT_API_BASE = "https://pigment.app/api/v1"

# Currencies to sync. The CSV "Currency" column carries the ISO code, which is
# the matching key of the Pigment import configuration (Currency.Code property).
CURRENCIES = {
    "GBP": "GBP",
    "JPY": "JPY",
    "USD": "USD",
}
EUR_DISPLAY_NAME = "EUR"

# CSV column headers — must match the import block's column mapping in Pigment.
COL_RATE_TYPE = "Currency translation rate"
COL_MONTH     = "Month"
COL_VERSION   = "Exchange rate version"
COL_CURRENCY  = "Currency"
COL_VALUE     = "Value"

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
    """Convert year/month to the Pigment month label, e.g. "Jan 26"."""
    return date(year, month, 1).strftime("%b %y")


# ---------------------------------------------------------------------------
# CSV building
# ---------------------------------------------------------------------------

def build_csv(rows: list[dict]) -> str:
    """Render the rows as a CSV string with a header line."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([COL_RATE_TYPE, COL_MONTH, COL_VERSION, COL_CURRENCY, COL_VALUE])
    for r in rows:
        rate_type, month, version, currency = r["dimension_values"]
        writer.writerow([rate_type, month, version, currency, r["metric_value"]])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pigment injection — Import API (CSV push)
# ---------------------------------------------------------------------------

def push_to_pigment(
    csv_body: str,
    api_key: str,
    config_id: str,
    dry_run: bool = False,
) -> None:
    """Push the CSV to Pigment via the Import API and poll for completion."""
    if dry_run:
        print("[DRY RUN] CSV that would be pushed to Pigment:")
        print(csv_body)
        return

    push_url = f"{PIGMENT_API_BASE}/import/push/csv?configurationId={config_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "text/csv",
    }
    resp = requests.post(
        push_url, data=csv_body.encode("utf-8"), headers=headers, timeout=60
    )
    resp.raise_for_status()
    import_id = resp.json().get("importId")
    print(f"[OK] CSV pushed. importId={import_id}")

    if not import_id:
        return

    # Poll import status until it leaves InProgress (max ~60s).
    status_url = (
        f"{PIGMENT_API_BASE}/import/{import_id}/status?includedDetailedReport=true"
    )
    for _ in range(20):
        time.sleep(3)
        s = requests.get(status_url, headers={"Authorization": f"Bearer {api_key}"},
                         timeout=30)
        s.raise_for_status()
        body = s.json()
        status = body.get("importStatus")
        if status == "InProgress":
            continue
        if status == "Completed":
            report = body.get("detailedReport", {})
            summary = report.get("summary", {})
            print(f"[OK] Import completed. {summary}")
            # Surface skipped-row diagnostics, if any.
            for block in report.get("impactedBlocks", []):
                for skip in block.get("skippedData", []):
                    print(
                        f"  SKIPPED {skip.get('count')} row(s) — "
                        f"reason={skip.get('reason')} "
                        f"column={skip.get('sourceColumnName')} "
                        f"samples={skip.get('samples')}",
                        file=sys.stderr,
                    )
            return
        # Failed
        print(f"[ERROR] Import failed: {body.get('errorsDetails')}", file=sys.stderr)
        sys.exit(1)
    print("[WARN] Import still InProgress after polling window.", file=sys.stderr)


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
        help="Print the CSV without writing to Pigment",
    )
    args = parser.parse_args()

    api_key = os.environ.get("PIGMENT_API_KEY")
    config_id = os.environ.get("PIGMENT_IMPORT_CONFIG_ID")
    if not args.dry_run:
        if not api_key:
            print("ERROR: PIGMENT_API_KEY environment variable is required.", file=sys.stderr)
            sys.exit(1)
        if not config_id:
            print("ERROR: PIGMENT_IMPORT_CONFIG_ID environment variable is required.", file=sys.stderr)
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
    csv_body = build_csv(rows)
    push_to_pigment(csv_body, api_key, config_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
