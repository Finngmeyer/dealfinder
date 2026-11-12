"""Build config/loan_limits/<year>_counties.csv from HUD's county loan-limit files.

    uv run python scripts/build_loan_limits.py 2026                    # downloads from apps.hud.gov
    uv run python scripts/build_loan_limits.py 2026 --fha FILE --gse FILE

Inputs, fixed-width text described at https://apps.hud.gov/pub/chums/file_layouts.html
  * cy<year>-forward-limits.txt   FHA forward mortgage limits by county
  * cy<year>-gse-limits.txt       Fannie Mae / Freddie Mac conforming limits by county

For 2026 the GSE file was compared with FHFA's own county list: all 3,235 counties agree.

Each file starts with two national rows, the ceiling and the floor. The script checks them
against config/loan_limits/<year>.yaml and stops if they disagree, so the two files cannot
drift apart.
"""

from __future__ import annotations

import argparse
import csv
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://apps.hud.gov/pub/chums"
UNIT_COLUMNS = ((73, 80), (80, 87), (87, 94), (94, 101))  # limits for 1, 2, 3 and 4 units
STATE_COLUMNS = (101, 103)
COUNTY_COLUMNS = (103, 106)
NAME_COLUMNS = (132, 147)


def read_lines(path: str | None, url: str) -> list[str]:
    if path:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "dealfinder-build"})
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if len(line) >= COUNTY_COLUMNS[1]]


def parse(lines: list[str]) -> tuple[list[list[int]], dict[tuple[str, str], tuple[str, list[int]]]]:
    """National rows (no state) and county rows keyed by (state abbreviation, county code)."""
    national: list[list[int]] = []
    counties: dict[tuple[str, str], tuple[str, list[int]]] = {}
    for line in lines:
        limits = [int(line[start:end]) for start, end in UNIT_COLUMNS]
        state = line[slice(*STATE_COLUMNS)].strip()
        if not state:
            national.append(limits)
            continue
        key = (state, line[slice(*COUNTY_COLUMNS)])
        if key in counties:
            raise SystemExit(f"duplicate county row {key}")
        counties[key] = (line[slice(*NAME_COLUMNS)].strip().title(), limits)
    return national, counties


def state_fips() -> dict[str, str]:
    """State abbreviation -> two-digit FIPS, from the bundled Census county list."""
    with (ROOT / "src/dealfinder/data/counties.csv").open(encoding="utf-8") as handle:
        return {row["state"]: row["county_fips"][:2] for row in csv.DictReader(handle)}


def county_names() -> dict[str, str]:
    with (ROOT / "src/dealfinder/data/counties.csv").open(encoding="utf-8") as handle:
        return {row["county_fips"]: row["name"] for row in csv.DictReader(handle)}


def check_national(year: int, fha: list[list[int]], gse: list[list[int]]) -> None:
    """The first national row is the ceiling and the second the floor (or baseline)."""
    path = ROOT / "config" / "loan_limits" / f"{year}.yaml"
    declared = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = {
        "fha_ceiling": fha[0],
        "fha_floor": fha[1],
        "conforming_ceiling": gse[0],
        "conforming_baseline": gse[1],
    }
    for key, values in expected.items():
        in_yaml = [declared[key][units] for units in (1, 2, 3, 4)]
        if in_yaml != values:
            raise SystemExit(f"{path.name} {key} is {in_yaml} but HUD's file says {values}")
    print(f"national limits in {path.name} match HUD's files")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("year", type=int)
    parser.add_argument("--fha", help="local copy of cy<year>-forward-limits.txt")
    parser.add_argument("--gse", help="local copy of cy<year>-gse-limits.txt")
    args = parser.parse_args()

    fha_national, fha = parse(read_lines(args.fha, f"{BASE_URL}/cy{args.year}-forward-limits.txt"))
    gse_national, gse = parse(read_lines(args.gse, f"{BASE_URL}/cy{args.year}-gse-limits.txt"))
    if len(fha_national) != 2 or len(gse_national) != 2:
        raise SystemExit("expected two national rows (ceiling, floor) at the top of each file")
    if set(fha) != set(gse):
        raise SystemExit(
            f"the two files list different counties: {sorted(set(fha) ^ set(gse))[:5]}"
        )
    check_national(args.year, fha_national, gse_national)

    fips_by_state = state_fips()
    names = county_names()
    rows = []
    for (state, county), (hud_name, fha_limits) in fha.items():
        if state not in fips_by_state:
            raise SystemExit(f"no FIPS code for state {state}")
        fips = fips_by_state[state] + county
        rows.append([fips, state, names.get(fips, hud_name), *fha_limits, *gse[(state, county)][1]])
    rows.sort()

    out = ROOT / "config" / "loan_limits" / f"{args.year}_counties.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["county_fips", "state", "name"]
            + [f"fha_{units}" for units in (1, 2, 3, 4)]
            + [f"conforming_{units}" for units in (1, 2, 3, 4)]
        )
        writer.writerows(rows)
    above_floor = sum(1 for row in rows if row[3:7] != fha_national[1])
    above_baseline = sum(1 for row in rows if row[7:11] != gse_national[1])
    print(
        f"wrote {len(rows):,} counties to {out}: {above_floor} above the FHA floor, "
        f"{above_baseline} above the conforming baseline"
    )


if __name__ == "__main__":
    main()
