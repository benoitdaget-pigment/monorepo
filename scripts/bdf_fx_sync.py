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

# Directory where the per-month daily-rate audit trails are stored (committed to
# the repository so statutory auditors can verify the MTD/YTD computations).
AUDIT_DIR = "data/fx_rates"


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
# Audit trail — daily rates used for the MTD/YTD computations
# ---------------------------------------------------------------------------

def write_audit_csv(
    all_daily: dict[str, dict[str, float]],
    year: int,
    month: int,
    computed: dict[str, dict[str, float | None]],
) -> str:
    """
    Write the daily reference rates that feed the MTD/YTD averages to
    data/fx_rates/<period>.csv, plus the resulting computed rates.

    Every daily rate from Jan 1 to the last day of the target month is listed
    (the YTD window). The "In MTD month" column flags the subset used for the
    MTD average. This lets auditors recompute both averages from the raw data.
    """
    last_day = monthrange(year, month)[1]
    month_prefix = f"{year}-{month:02d}-"
    ytd_start  = f"{year}-01-01"
    ytd_cutoff = f"{year}-{month:02d}-{last_day:02d}"
    codes = list(CURRENCIES.keys())

    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(AUDIT_DIR, f"{year}-{month:02d}.csv")

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            f"# EUR daily reference rates (ECB/BdF, via Frankfurter API) — "
            f"audit trail for {period_to_pigment_month(year, month)}"
        ])
        w.writerow([
            f"# YTD window: {ytd_start} .. {ytd_cutoff}   "
            f"MTD window: {year}-{month:02d}-01 .. {ytd_cutoff}"
        ])
        w.writerow(["# Rates expressed as 1 EUR = X foreign currency."])
        w.writerow([])

        # Daily detail
        w.writerow(["Date", "In MTD month"] + codes)
        for d in sorted(all_daily):
            if not (ytd_start <= d <= ytd_cutoff):
                continue
            row = [d, "yes" if d.startswith(month_prefix) else "no"]
            for c in codes:
                v = all_daily[d].get(c)
                row.append(f"{v:.6f}" if v is not None else "")
            w.writerow(row)

        # Computed results
        w.writerow([])
        w.writerow(["Computed rate"] + codes)
        for label in (RATE_MTD_AVG, RATE_YTD_AVG, RATE_CLOSING):
            row = [label]
            for c in codes:
                v = computed[c].get(label)
                row.append(f"{v:.6f}" if v is not None else "")
            w.writerow(row)

    print(f"[OK] Audit trail written to {path}")
    return path


def write_audit_html(
    all_daily: dict[str, dict[str, float]],
    year: int,
    month: int,
    computed: dict[str, dict[str, float | None]],
) -> str:
    """
    Write a human-friendly HTML version of the audit trail to
    data/fx_rates/<period>.html, with the MTD subset highlighted and the
    computed rates summarised at the top.
    """
    import html as _html

    last_day = monthrange(year, month)[1]
    month_prefix = f"{year}-{month:02d}-"
    ytd_start  = f"{year}-01-01"
    ytd_cutoff = f"{year}-{month:02d}-{last_day:02d}"
    codes = list(CURRENCIES.keys())
    label = period_to_pigment_month(year, month)

    def cell(v: float | None) -> str:
        return f"{v:.6f}" if v is not None else "—"

    # Summary table rows
    summary_rows = ""
    for rate_label in (RATE_MTD_AVG, RATE_YTD_AVG, RATE_CLOSING):
        cells = "".join(f"<td>{cell(computed[c].get(rate_label))}</td>" for c in codes)
        summary_rows += f"<tr><th>{_html.escape(rate_label)}</th>{cells}</tr>\n"

    # Daily detail rows
    daily_rows = ""
    for d in sorted(all_daily):
        if not (ytd_start <= d <= ytd_cutoff):
            continue
        in_month = d.startswith(month_prefix)
        cells = "".join(f"<td>{cell(all_daily[d].get(c))}</td>" for c in codes)
        flag = "✓" if in_month else ""
        cls = ' class="mtd"' if in_month else ""
        daily_rows += f"<tr{cls}><td>{d}</td><td class=\"flag\">{flag}</td>{cells}</tr>\n"

    code_headers = "".join(f"<th>{_html.escape(c)}</th>" for c in codes)

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EUR FX rates — {_html.escape(label)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 2rem; color: #1a1a2e; background: #f7f8fa; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.2rem; }}
  .meta {{ color: #555; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  .meta code {{ background: #eceef2; padding: 0.1rem 0.3rem; border-radius: 3px; }}
  table {{ border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          margin-bottom: 2rem; }}
  th, td {{ padding: 0.45rem 0.9rem; text-align: right; border-bottom: 1px solid #eceef2;
           font-variant-numeric: tabular-nums; }}
  thead th {{ background: #1a1a2e; color: #fff; text-align: right; position: sticky; top: 0; }}
  tbody th {{ text-align: left; font-weight: 600; }}
  td:first-child, th:first-child {{ text-align: left; }}
  .flag {{ text-align: center; color: #2a9d4a; font-weight: 700; }}
  tr.mtd {{ background: #f0f8f1; }}
  caption {{ caption-side: top; text-align: left; font-weight: 600; padding: 0.5rem 0; }}
  .legend {{ font-size: 0.8rem; color: #555; }}
  .legend .swatch {{ display: inline-block; width: 0.9rem; height: 0.9rem;
                    background: #f0f8f1; border: 1px solid #cde; vertical-align: middle; }}
</style>
</head>
<body>
<h1>EUR exchange rates — {_html.escape(label)}</h1>
<div class="meta">
  Source: ECB / Banque de France daily reference rates (via Frankfurter API).<br>
  Rates expressed as <strong>1 EUR = X foreign currency</strong>.<br>
  YTD window: <code>{ytd_start}</code> → <code>{ytd_cutoff}</code> &nbsp;·&nbsp;
  MTD window: <code>{year}-{month:02d}-01</code> → <code>{ytd_cutoff}</code>
</div>

<table>
  <caption>Computed rates pushed to Pigment</caption>
  <thead><tr><th>Rate</th>{code_headers}</tr></thead>
  <tbody>
{summary_rows}  </tbody>
</table>

<table>
  <caption>Daily reference rates used in the averages</caption>
  <thead><tr><th>Date</th><th>MTD</th>{code_headers}</tr></thead>
  <tbody>
{daily_rows}  </tbody>
</table>
<p class="legend"><span class="swatch"></span> Highlighted rows (MTD ✓) are the
days within {_html.escape(label)} used for the MTD average. All listed rows form
the YTD average window.</p>
</body>
</html>
"""

    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(AUDIT_DIR, f"{year}-{month:02d}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[OK] Audit trail (HTML) written to {path}")
    return path


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
    computed: dict[str, dict[str, float | None]] = {}

    for code, display_name in CURRENCIES.items():
        rates = compute_rates(all_daily, code, year, month)
        computed[code] = rates
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

    # Write the auditor-facing daily-rate trail (always, even in dry-run).
    write_audit_csv(all_daily, year, month, computed)
    write_audit_html(all_daily, year, month, computed)

    print(f"\nTotal: {len(rows)} values to push.")
    csv_body = build_csv(rows)
    push_to_pigment(csv_body, api_key, config_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
