"""Build the bundled ZIP-to-county lookup from Census Bureau files.

    uv run python scripts/build_zip_county.py                # downloads from www2.census.gov
    uv run python scripts/build_zip_county.py --rel FILE --counties FILE \
        --cousub-rel FILE --ct-crosswalk FILE

Inputs
  * 2020 ZCTA-to-county relationship file (tab20_zcta520_county20_natl.txt)
  * 2020 national county list (national_county2020.txt)
  * 2020 ZCTA-to-county-subdivision relationship file (tab20_zcta520_cousub20_natl.txt)
  * Connecticut county-to-planning-region crosswalk (ct_cou_to_cousub_crosswalk.txt)

Outputs, in src/dealfinder/data/
  * zip_county.csv   zip, county_fips  (the county holding most of the ZIP's land area)
  * counties.csv     county_fips, state, name

ZCTAs are the Census Bureau's approximation of ZIP codes, so a few PO-box-only ZIPs are missing
and a ZIP that straddles a county line is assigned to one side.

Connecticut replaced its eight counties with nine planning regions in 2022. HUD, FHFA, FEMA and
the ACS all use the planning-region codes now, but the 2020 relationship file still has the old
counties. So each Connecticut ZIP is assigned through its main town: ZIP -> town holding most of
its land -> that town's planning region.
"""

from __future__ import annotations

import argparse
import csv
import io
import urllib.request
from pathlib import Path

BASE = "https://www2.census.gov/geo/docs"
REL_URL = f"{BASE}/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
COUSUB_REL_URL = f"{BASE}/maps-data/data/rel2020/zcta520/tab20_zcta520_cousub20_natl.txt"
COUNTIES_URL = f"{BASE}/reference/codes2020/national_county2020.txt"
CT_CROSSWALK_URL = f"{BASE}/reference/ct_change/ct_cou_to_cousub_crosswalk.txt"
OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "dealfinder" / "data"
CONNECTICUT = "09"


def read_text(path: str | None, url: str) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8-sig")
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read().decode("utf-8-sig")


def dominant(rel_text: str, geoid_column: str, *, state: str | None = None) -> dict[str, str]:
    """ZIP -> the area in ``geoid_column`` with the largest land overlap (lowest code on a tie).

    ``state`` keeps only areas whose code starts with that state's FIPS.
    """
    best: dict[str, tuple[int, str]] = {}
    for row in csv.DictReader(io.StringIO(rel_text), delimiter="|"):
        zcta, area_code = row["GEOID_ZCTA5_20"], row[geoid_column]
        if not zcta or not area_code or (state and not area_code.startswith(state)):
            continue
        land = int(row["AREALAND_PART"] or 0)
        current = best.get(zcta)
        if current is None or land > current[0] or (land == current[0] and area_code < current[1]):
            best[zcta] = (land, area_code)
    return {zcta: area_code for zcta, (_, area_code) in best.items()}


def counties(counties_text: str) -> dict[str, tuple[str, str]]:
    """County FIPS -> (state abbreviation, county name)."""
    result: dict[str, tuple[str, str]] = {}
    for row in csv.DictReader(io.StringIO(counties_text), delimiter="|"):
        result[row["STATEFP"] + row["COUNTYFP"]] = (row["STATE"], row["COUNTYNAME"])
    return result


def connecticut_regions(crosswalk_text: str) -> tuple[dict[str, str], dict[str, str]]:
    """(old town GEOID -> planning-region FIPS, planning-region FIPS -> name).

    The file's header cells contain line breaks, so columns are read by position:
    state, old county, old county name, new county, new county name, town code, old town GEOID.
    """
    town_to_region: dict[str, str] = {}
    names: dict[str, str] = {}
    rows = list(csv.reader(io.StringIO(crosswalk_text), delimiter="|"))
    for row in rows[1:]:
        if len(row) < 7 or not row[3].strip() or not row[6].strip():
            continue  # water-only areas and footnotes have no planning region
        fips = row[0].strip() + row[3].strip()
        town_to_region[row[6].strip()] = fips
        names[fips] = row[4].strip()
    return town_to_region, names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rel", help="local copy of the ZCTA-county relationship file")
    parser.add_argument("--counties", help="local copy of the national county list")
    parser.add_argument("--cousub-rel", help="local copy of the ZCTA-county-subdivision file")
    parser.add_argument("--ct-crosswalk", help="local copy of the Connecticut crosswalk")
    args = parser.parse_args()

    zip_to_county = dominant(read_text(args.rel, REL_URL), "GEOID_COUNTY_20")
    county_info = counties(read_text(args.counties, COUNTIES_URL))

    # Connecticut: swap the old counties for planning regions, ZIP by ZIP, through the main town.
    town_to_region, region_names = connecticut_regions(
        read_text(args.ct_crosswalk, CT_CROSSWALK_URL)
    )
    zip_to_town = dominant(
        read_text(args.cousub_rel, COUSUB_REL_URL), "GEOID_COUSUB_20", state=CONNECTICUT
    )
    connecticut_zips = [z for z, fips in zip_to_county.items() if fips.startswith(CONNECTICUT)]
    unplaced = []
    for zip_code in connecticut_zips:
        region = town_to_region.get(zip_to_town.get(zip_code, ""))
        if region is None:
            unplaced.append(zip_code)
            del zip_to_county[zip_code]
        else:
            zip_to_county[zip_code] = region
    county_info = {
        fips: info for fips, info in county_info.items() if not fips.startswith(CONNECTICUT)
    }
    county_info.update({fips: ("CT", name) for fips, name in region_names.items()})

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
    print(
        f"Connecticut: {len(connecticut_zips) - len(unplaced)} ZIPs placed in "
        f"{len(region_names)} planning regions, {len(unplaced)} left out {unplaced[:5]}"
    )


if __name__ == "__main__":
    main()
