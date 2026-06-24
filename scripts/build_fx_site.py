#!/usr/bin/env python3
"""
Build the static GitHub Pages site for the FX audit trail.

Collects every monthly report in data/fx_rates/ (the per-month .html and .csv
files produced by bdf_fx_sync.py) into a self-contained site/ directory and
generates an index.html linking to each month.

Usage:
    python scripts/build_fx_site.py            # output to ./site
    python scripts/build_fx_site.py --out dist
"""

import argparse
import html
import os
import re
import shutil
from datetime import date

SOURCE_DIR = "data/fx_rates"
PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})\.html$")


def discover_periods(source_dir: str) -> list[tuple[int, int]]:
    periods = []
    for name in os.listdir(source_dir):
        m = PERIOD_RE.match(name)
        if m:
            periods.append((int(m.group(1)), int(m.group(2))))
    # Most recent first
    return sorted(periods, reverse=True)


def month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%B %Y")


def build_index(periods: list[tuple[int, int]]) -> str:
    cards = ""
    for year, month in periods:
        base = f"{year}-{month:02d}"
        label = html.escape(month_label(year, month))
        cards += f"""    <li>
      <a class="month" href="{base}.html">{label}</a>
      <a class="csv" href="{base}.csv" download>CSV</a>
    </li>
"""
    if not cards:
        cards = "    <li><em>No reports available yet.</em></li>\n"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EUR FX rates — audit trail</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 2rem auto; max-width: 720px; color: #1a1a2e; background: #f7f8fa; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.2rem; }}
  .intro {{ color: #555; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ display: flex; align-items: center; justify-content: space-between;
       background: #fff; padding: 0.8rem 1.1rem; margin-bottom: 0.5rem;
       border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  a.month {{ font-weight: 600; text-decoration: none; color: #1a1a2e; font-size: 1.05rem; }}
  a.month:hover {{ color: #2a6df4; }}
  a.csv {{ font-size: 0.8rem; text-decoration: none; color: #2a6df4;
          border: 1px solid #2a6df4; padding: 0.2rem 0.6rem; border-radius: 4px; }}
  a.csv:hover {{ background: #2a6df4; color: #fff; }}
</style>
</head>
<body>
<h1>EUR exchange rates — audit trail</h1>
<p class="intro">
  Official ECB / Banque de France daily reference rates and the resulting
  MTD / YTD average and closing rates synced to Pigment. Select a month to view
  the detailed daily rates, or download the raw CSV.
</p>
<ul>
{cards}</ul>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the FX audit Pages site")
    parser.add_argument("--out", default="site", help="Output directory (default: site)")
    args = parser.parse_args()

    periods = discover_periods(SOURCE_DIR)

    os.makedirs(args.out, exist_ok=True)
    # Copy every report file (html + csv) into the site root.
    for name in os.listdir(SOURCE_DIR):
        if name.endswith((".html", ".csv")):
            shutil.copy(os.path.join(SOURCE_DIR, name), os.path.join(args.out, name))

    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index(periods))

    print(f"[OK] Built site in {args.out}/ with {len(periods)} monthly report(s).")


if __name__ == "__main__":
    main()
