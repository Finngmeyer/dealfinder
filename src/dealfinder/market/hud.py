"""HUD Fair Market Rents, with Small Area FMRs by ZIP where HUD publishes them.

FMRs are gross rents: they include utilities the tenant pays. ``contract_rent`` in
``dealfinder.market.context`` nets a utility allowance off when tenants pay.

Built from HUD's published API documentation and not yet exercised against the live API,
which needs a token. Known gap: HUD publishes New England FMRs by town, not county, and that
lookup is not implemented.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "hud_fmr"
BASE_URL = "https://www.huduser.gov/hudapi/public"
ATTRIBUTION = (
    "This product uses the HUD User Data API but is not endorsed or certified by HUD User."
)
NEW_ENGLAND = frozenset({"CT", "ME", "MA", "NH", "RI", "VT"})
BEDROOM_KEYS = {
    0: "Efficiency",
    1: "One-Bedroom",
    2: "Two-Bedroom",
    3: "Three-Bedroom",
    4: "Four-Bedroom",
}
AREA_ROW = "MSA level"  # the metro-wide row HUD adds to a Small Area FMR list


class FairMarketRents(BaseModel):
    fiscal_year: int | None
    area_name: str
    zip_level: bool  # True when these are Small Area FMRs for the ZIP itself
    rents: dict[int, float]  # bedrooms 0-4 -> gross monthly rent
    stale: bool = False

    def gross_rent(self, bedrooms: float, extra_bedroom_pct: float) -> float:
        """Gross FMR for a bedroom count. Past four bedrooms HUD adds a fixed share of the
        four-bedroom FMR for each extra bedroom. Fractional counts round down."""
        beds = max(0, int(bedrooms))
        if beds <= 4:
            return self.rents[beds]
        return self.rents[4] * (1 + extra_bedroom_pct * (beds - 4))

    def describe(self) -> str:
        year = f"FY{self.fiscal_year} " if self.fiscal_year else ""
        kind = "Small Area FMR for the ZIP" if self.zip_level else "area FMR"
        return f"HUD {year}{kind}, {self.area_name}".rstrip(", ")


def _rents(row: dict[str, Any]) -> dict[int, float]:
    try:
        return {beds: float(row[key]) for beds, key in BEDROOM_KEYS.items()}
    except (KeyError, TypeError, ValueError) as error:
        raise SourceUnavailable(SOURCE, "the response is missing a bedroom size") from error


def _year(*candidates: Any) -> int | None:
    for candidate in candidates:
        try:
            return int(str(candidate))
        except (TypeError, ValueError):
            continue
    return None


def parse(body: bytes, zip_code: str) -> FairMarketRents:
    """Read ``fmr/data/{entityid}``.

    ``basicdata`` is a single object for an ordinary area, or a list with one row per ZIP plus a
    metro-wide row when the area uses Small Area FMRs. The ZIP's own row wins; otherwise the
    metro-wide row is used and the result is marked as area-level.
    """
    try:
        data = json.loads(body)["data"]
        basic = data["basicdata"]
    except (ValueError, KeyError, TypeError) as error:
        raise SourceUnavailable(SOURCE, "the response was not in the expected form") from error

    area = str(
        data.get("area_name") or data.get("metro_name") or data.get("county_name") or ""
    ).strip()
    if isinstance(basic, dict):
        return FairMarketRents(
            fiscal_year=_year(basic.get("year"), data.get("year")),
            area_name=area,
            zip_level=False,
            rents=_rents(basic),
        )
    if isinstance(basic, list) and basic:
        rows = {str(row.get("zip_code", "")).strip(): row for row in basic if isinstance(row, dict)}
        own = rows.get(zip_code)
        chosen = own or rows.get(AREA_ROW) or next(iter(rows.values()))
        return FairMarketRents(
            fiscal_year=_year(data.get("year"), chosen.get("year")),
            area_name=area,
            zip_level=own is not None,
            rents=_rents(chosen),
        )
    raise SourceUnavailable(SOURCE, "the response had no rent data")


def fair_market_rents(
    fetcher: Fetcher,
    *,
    token: str | None,
    zip_code: str,
    county_fips: str | None,
    state: str | None,
    max_age: timedelta = timedelta(days=30),
) -> FairMarketRents:
    if not token:
        raise SourceUnavailable(SOURCE, "HUD_API_TOKEN is not set", missing_key=True)
    if county_fips is None:
        raise SourceUnavailable(SOURCE, "the county is unknown, so there is no area to look up")
    if state in NEW_ENGLAND:
        raise SourceUnavailable(
            SOURCE, "HUD publishes New England rents by town, which is not supported yet"
        )
    fetched = fetcher.get(
        SOURCE,
        f"{BASE_URL}/fmr/data/{county_fips}99999",
        headers={"Authorization": f"Bearer {token}"},
        max_age=max_age,
        validate=lambda body: parse(body, zip_code),
    )
    result = parse(fetched.body, zip_code)
    return result.model_copy(update={"stale": fetched.stale})
