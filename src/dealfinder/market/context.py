"""Everything the free data sources know about one ZIP code, and the state of each source.

A source that is switched off, missing its key, or failing never stops the others: it is
recorded as off or unavailable with the reason, and whatever depends on it falls back to the
pessimistic stand-ins in settings.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from dealfinder.geo import Place, lookup_zip
from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.market import acs, fred, hud, nri, zori
from dealfinder.market.acs import CountyHousing
from dealfinder.market.fred import MortgageRate
from dealfinder.market.hud import FairMarketRents
from dealfinder.market.nri import CountyRisk
from dealfinder.market.zori import RentIndex
from dealfinder.settings import Config, Environment
from dealfinder.text import short_date

LABELS = {
    "fred": "Mortgage rate (FRED)",
    "hud_fmr": "Fair Market Rents (HUD)",
    "acs": "Taxes and vacancy (Census ACS)",
    "zori": "Rent index (Zillow)",
    "nri": "Hazard rating (FEMA)",
}


class SourceStatus(BaseModel):
    key: str
    label: str
    state: Literal["ok", "stale", "off", "unavailable"]
    detail: str


class MarketContext(BaseModel):
    zip_code: str
    place: Place | None
    mortgage_rate: MortgageRate | None = None
    fair_market_rents: FairMarketRents | None = None
    housing: CountyHousing | None = None
    rent_index: RentIndex | None = None
    risk: CountyRisk | None = None
    sources: list[SourceStatus] = Field(default_factory=list)

    @property
    def attributions(self) -> list[str]:
        """Notices a data provider requires whenever its data is shown."""
        return [hud.ATTRIBUTION] if self.fair_market_rents is not None else []

    def missing(self) -> list[SourceStatus]:
        return [status for status in self.sources if status.state in ("off", "unavailable")]


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


def load_market(
    zip_code: str, config: Config, environment: Environment, fetcher: Fetcher
) -> MarketContext:
    """Query every enabled source for one ZIP. Never raises for a source problem."""
    market = config.settings.market
    place = lookup_zip(zip_code)
    county = place.county_fips if place else None
    context = MarketContext(zip_code=place.zip_code if place else zip_code, place=place)

    def run[T](key: str, describe: Callable[[T], str], load: Callable[[timedelta], T]) -> T | None:
        label = LABELS[key]
        if not getattr(market.sources, key):
            context.sources.append(
                SourceStatus(key=key, label=label, state="off", detail="switched off in settings")
            )
            return None
        try:
            value = load(timedelta(days=getattr(market.cache_days, key)))
        except SourceUnavailable as error:
            state: Literal["off", "unavailable"] = "off" if error.missing_key else "unavailable"
            context.sources.append(
                SourceStatus(key=key, label=label, state=state, detail=error.reason)
            )
            return None
        stale = bool(getattr(value, "stale", False))
        context.sources.append(
            SourceStatus(
                key=key,
                label=label,
                state="stale" if stale else "ok",
                detail=describe(value) + (" (cached copy; refresh failed)" if stale else ""),
            )
        )
        return value

    context.mortgage_rate = run(
        "fred",
        lambda rate: f"{rate.rate:.2%} for the week of {short_date(rate.as_of)}",
        lambda max_age: fred.mortgage_rate(
            fetcher, api_key=_secret(environment.fred_api_key), max_age=max_age
        ),
    )
    context.fair_market_rents = run(
        "hud_fmr",
        lambda rents: rents.describe(),
        lambda max_age: hud.fair_market_rents(
            fetcher,
            token=_secret(environment.hud_api_token),
            zip_code=context.zip_code,
            county_fips=county,
            state=place.state if place else None,
            max_age=max_age,
        ),
    )
    context.housing = run(
        "acs",
        lambda housing: f"{housing.vintage}, {housing.name}",
        lambda max_age: acs.county_housing(
            fetcher,
            key=_secret(environment.census_api_key),
            county_fips=county,
            year=market.acs_year,
            max_age=max_age,
        ),
    )
    context.rent_index = run(
        "zori",
        lambda index: index.describe(),
        lambda max_age: zori.rent_index(
            fetcher, zip_code=context.zip_code, county_fips=county, max_age=max_age
        ),
    )
    context.risk = run(
        "nri",
        lambda risk: risk.describe(),
        lambda max_age: nri.county_risk(
            fetcher,
            county_fips=county,
            manual_csv=environment.dealfinder_data_dir / "nri_counties.csv",
            max_age=max_age,
        ),
    )
    return context
