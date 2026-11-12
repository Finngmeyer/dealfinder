"""Build the bundled ZIP-to-county lookup from two Census Bureau files.

    uv run python scripts/build_zip_county.py                # downloads from www2.census.gov
    uv run python scripts/build_zip_county.py --rel FILE --counties FILE

Inputs
  * 2020 ZCTA-to-county relationship file (tab20_zcta520_county20_natl.txt)
  * 2020 national county list (national_county2020.txt)

Outputs, in src/dealfinder/data/
  * zip_county.csv   zip, county_fips  (the county holding most of the ZIP's land area)
  * counties.csv     county_fips, state, name

ZCTAs are the Census Bureau's approximation of ZIP codes, so a few PO-box-only ZIPs are missing
and a ZIP that straddles a county line is assigned to one side. Phase 2 prefers HUD's ZIP
crosswalk when a HUD token is set and falls back to this file.
"""

from __future__ import annotations

import argparse
import csv
import io
import urllib.request
from pathlib import Path

REL_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)
COUNTIES_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"
OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "dealfinder" / "data"


def read_text(path: str | None, url: str) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8-sig")
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read().decode("utf-8-sig")


def dominant_counties(rel_text: str) -> dict[str, str]:
    """ZIP -> county FIPS with the largest land-area overlap (lowest FIPS wins a tie)."""
    best: dict[str, tuple[int, str]] = {}
    for row in csv.DictReader(io.StringIO(rel_text), delimiter="|"):
        zcta, county = row["GEOID_ZCTA5_20"], row["GEOID_COUNTY_20"]
        if not zcta or not county:
            continue
        area = int(row["AREALAND_PART"] or 0)
        current = best.get(zcta)
        if current is None or area > current[0] or (area == current[0] and county < current[1]):
            best[zcta] = (area, county)
    return {zcta: county for zcta, (_, county) in best.items()}


def counties(counties_text: str) -> dict[str, tuple[str, str]]:
    """County FIPS -> (state abbreviation, county name)."""
    result: dict[str, tuple[str, str]] = {}
    for row in csv.DictReader(io.StringIO(counties_text), delimiter="|"):
        result[row["STATEFP"] + row["COUNTYFP"]] = (row["STATE"], row["COUNTYNAME"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rel", help="local copy of the ZCTA-county relationship file")
    parser.add_argument("--counties", help="local copy of the national county list")
    args = parser.parse_args()

    zip_to_county = dominant_counties(read_text(args.rel, REL_URL))
    county_info = counties(read_text(args.counties, COUNTIES_URL))
    missing = sorted({fips for fips in zip_to_county.values() if fips not in county_info})
    if missing:
        raise SystemExit(f"{len(missing)} county codes have no name, e.g. {missing[:5]}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "zip_county.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["zip", "county_fips"])
        writer.writerows(sorted(zip_to_county.items()))
    with (OUT_DIR / "counties.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["county_fips", "state", "name"])
        writer.writerows((fips, state, name) for fips, (state, name) in sorted(county_info.items()))
    print(f"wrote {len(zip_to_county):,} ZIPs and {len(county_info):,} counties to {OUT_DIR}")


if __name__ == "__main__":
    main()
