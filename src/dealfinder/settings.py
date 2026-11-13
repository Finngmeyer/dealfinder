"""Settings: business assumptions from config/*.yaml, secrets and paths from the environment.

The YAML file is the single source of truth for assumptions. The models below deliberately
carry no defaults for them, so a missing key is an error with a clear message, not a silent
fallback to a number buried in code. ``config/settings.local.yaml`` is merged over
``config/settings.yaml`` for personal overrides.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from dealfinder.underwrite.inputs import HoldAssumptions, MiTier, Shock, Targets

PropertyType = Literal[
    "sfr", "condo", "townhouse", "duplex", "triplex", "fourplex", "multifamily_5plus"
]
LoanLimitKind = Literal["fha", "conforming_baseline", "conforming_county", "none"]

SETTINGS_FILE = "settings.yaml"
LOCAL_SETTINGS_FILE = "settings.local.yaml"


class SettingsError(Exception):
    """The configuration is missing or invalid."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuyBox(Strict):
    property_types: list[PropertyType]
    states: Literal["all"] | list[str]
    max_price: float | None
    min_unit_confidence: Literal["high", "medium", "low"]
    ranking_scenario: str
    ranking_scenario_5plus: str | None
    targets: Targets


class Vacancy(Strict):
    floor: float = Field(ge=0, lt=1)
    county_add: float


class Management(Strict):
    mode: Literal["self", "manager"]
    self_pct: float = Field(ge=0, le=1)
    manager_pct: float = Field(ge=0, le=1)

    def pct_for(self, mode: Literal["self", "manager"]) -> float:
        return self.self_pct if mode == "self" else self.manager_pct


class AgeBand(Strict):
    built_before: int | None
    repairs_pct: float = Field(ge=0, le=1)
    reserves_pct: float = Field(ge=0, le=1)


class Utilities(Strict):
    paid_by: Literal["owner", "tenant", "unknown"]
    owner_cost_per_unit_month: float = Field(ge=0)


class Insurance(Strict):
    per_unit_year: float = Field(ge=0)
    high_risk_multiplier: float = Field(ge=1)


class Underwriting(Strict):
    vacancy: Vacancy
    management: Management
    repairs_and_reserves: list[AgeBand] = Field(min_length=1)
    utilities: Utilities
    insurance: Insurance
    flood_insurance_per_unit_year: float = Field(ge=0)
    misc_per_unit_year: float = Field(ge=0)
    closing_cost_pct: float = Field(ge=0, le=1)
    rehab_per_unit: dict[str, float]

    def age_band(self, year_built: int) -> AgeBand:
        """First band whose cutoff is after the year built; the open-ended band catches the rest."""
        for band in self.repairs_and_reserves:
            if band.built_before is None or year_built < band.built_before:
                return band
        return self.repairs_and_reserves[-1]


class WhenUnknown(Strict):
    tax_rate: float = Field(ge=0, le=1)
    vacancy_rate: float = Field(ge=0, lt=1)
    base_rate: float = Field(ge=0)
    year_built: int
    high_hazard_county: bool
    in_flood_zone: bool


class SelfSufficiencyRule(Strict):
    factor: float = Field(gt=0, le=1)
    units: list[int]


class LenderDscr(Strict):
    min: float
    best: float


class Preset(Strict):
    """One loan preset. Fields with a default describe a feature the loan may simply not have."""

    enabled: bool = True
    label: str = ""
    program: Literal["fha", "conventional", "dscr", "commercial", "cash"]
    owner_occupied: bool = False
    min_units: int = 1
    max_units: int | None = None
    down_payment_pct: float = Field(ge=0, le=1)
    term_years: int = Field(gt=0)
    rate_spread: float
    rate_quote: float | None = None
    points_pct: float = 0.0
    upfront_mi_pct: float = 0.0
    annual_mi: list[MiTier] = Field(default_factory=list)
    loan_limit: LoanLimitKind = "none"
    self_sufficiency: SelfSufficiencyRule | None = None
    lender_dscr: LenderDscr | None = None
    reserves_months: float = 0.0

    def fits(self, unit_count: int) -> bool:
        return unit_count >= self.min_units and (
            self.max_units is None or unit_count <= self.max_units
        )


class Financing(Strict):
    loan_limit_year: int
    base_rate_manual: float | None
    presets: dict[str, Preset]


class MarketSources(Strict):
    fred: bool
    hud_fmr: bool
    acs: bool
    zori: bool
    nri: bool


class CacheDays(Strict):
    fred: float = Field(ge=0)
    hud_fmr: float = Field(ge=0)
    acs: float = Field(ge=0)
    zori: float = Field(ge=0)
    nri: float = Field(ge=0)


class Market(Strict):
    sources: MarketSources
    acs_year: int
    high_risk_ratings: list[str]
    fmr_extra_bedroom_pct: float = Field(ge=0)
    rent_check_tolerance: float = Field(ge=0)
    cache_days: CacheDays


class SourceSwitch(Strict):
    enabled: bool


class ProviderBudget(Strict):
    plan: str
    monthly_request_cap: int | None = Field(ge=0)  # None removes the cap
    billing_cycle_day: int = Field(ge=1, le=31)


class Budgets(Strict):
    rentcast: ProviderBudget


class Plan(Strict):
    monthly_fee: float = Field(ge=0)
    included: int = Field(ge=0)
    overage: float = Field(ge=0)

    def cost(self, requests: int) -> float:
        """Monthly bill for a number of requests on this plan."""
        return self.monthly_fee + max(0, requests - self.included) * self.overage


class Sweep(Strict):
    page_size: int = Field(ge=1, le=500)
    filter_by_max_price: bool
    daily_days_old: int = Field(ge=1)
    page_cache_hours: float = Field(ge=0)
    completeness_tolerance: float = Field(ge=0, lt=1)
    missed_sweeps_before_off_market: int = Field(ge=1)


class Dedupe(Strict):
    radius_m: float = Field(gt=0)
    price_tolerance: float = Field(ge=0, lt=1)


class LlmSettings(Strict):
    enabled: bool
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_tokens: int = Field(ge=256)
    only_below_confidence: Literal["high", "medium"]
    max_calls_per_run: int = Field(ge=0)


class Classify(Strict):
    rehab_by_flag: dict[str, list[str]]  # rehab level -> flags that trigger it
    property_record_cache_days: float = Field(ge=0)
    llm: LlmSettings

    def rehab_level(self, flags: list[str]) -> str:
        """The costliest rehab level any of the flags calls for, else "none"."""
        triggered = [
            level for level, names in self.rehab_by_flag.items() if set(names) & set(flags)
        ]
        for level in ("heavy", "light"):
            if level in triggered:
                return level
        return triggered[0] if triggered else "none"


class Stage1(Strict):
    min_cash_flow_per_door_month: float | None
    min_rent_to_price: float | None = Field(ge=0)


class Stage2(Strict):
    rent_estimates: bool
    max_rent_requests_per_run: int = Field(ge=0)
    rent_comp_count: int = Field(ge=5, le=25)
    rent_cache_days: float = Field(ge=0)
    flood_zones: bool
    flood_cache_days: float = Field(ge=0)
    llm: bool


class Funnel(Strict):
    stage1: Stage1
    stage2: Stage2


class Scale(Strict):
    """Points rise in a straight line from none at ``zero_at`` to ``weight`` at ``full_at``."""

    weight: float = Field(ge=0)
    zero_at: float
    full_at: float


class SubScale(Strict):
    points: float = Field(ge=0)
    full_at: float = Field(gt=0)


class Motivation(Strict):
    weight: float = Field(ge=0)
    days_on_market: SubScale
    price_cut: SubScale
    tag_points: float = Field(ge=0)
    tags: list[str]
    listing_types: list[str]


class Penalties(Strict):
    flood_zone: float = Field(ge=0)
    high_hazard: float = Field(ge=0)


class Scoring(Strict):
    cash_flow_per_door: Scale
    coc: Scale
    dscr: Scale
    cap_rate_vs_county: Scale
    price_per_door_vs_county: Scale
    rent_upside: Scale
    seller_motivation: Motivation
    county_sample_min: int = Field(ge=1)
    penalties: Penalties
    confidence_multiplier: dict[Literal["high", "medium", "low"], float]
    grades: dict[str, float]


class AlertChannels(Strict):
    discord: bool
    slack: bool
    email: bool


class Alerts(Strict):
    enabled: bool
    channels: AlertChannels
    grades: list[str]
    starred_price_cut_pct: float = Field(gt=0, lt=1)
    max_deals_per_section: int = Field(ge=1)


Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Schedule(Strict):
    daily_at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekly_on: Weekday
    weekly_at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class HomeHarvestSettings(Strict):
    locations: list[str]
    max_results_per_location: int = Field(ge=1, le=10_000)
    extra_property_data: bool


class ApifyZillowSettings(Strict):
    actor: str
    search_urls: list[str]
    extraction_method: Literal["PAGINATION_WITH_ZOOM_IN", "PAGINATION", "MAP_MARKERS"]
    results_limit: int = Field(ge=1)
    price_per_result: float = Field(ge=0)
    monthly_result_cap: int | None = Field(ge=0)


class Scrapers(Strict):
    min_seconds_between_requests: float = Field(ge=1)
    homeharvest: HomeHarvestSettings
    apify_zillow: ApifyZillowSettings


class Settings(Strict):
    """Everything in settings.yaml."""

    def ranking_for(self, unit_count: int | None) -> str:
        """The loan a building is screened and ranked under: the buy box's ranking scenario,
        or the one named for bigger buildings when that loan is not offered at this size."""
        box, presets = self.buy_box, self.financing.presets
        main, other = presets.get(box.ranking_scenario), box.ranking_scenario_5plus
        if (
            unit_count
            and main is not None
            and not main.fits(unit_count)
            and other in presets
            and presets[other].fits(unit_count)
        ):
            return str(other)
        return box.ranking_scenario

    buy_box: BuyBox
    underwriting: Underwriting
    when_unknown: WhenUnknown
    hold: HoldAssumptions
    sensitivity: list[Shock]
    financing: Financing
    market: Market
    sources: dict[str, SourceSwitch]
    budgets: Budgets
    rentcast_plans: dict[str, Plan]
    sweep: Sweep
    dedupe: Dedupe
    classify: Classify
    funnel: Funnel
    scoring: Scoring
    alerts: Alerts
    schedule: Schedule
    scrapers: Scrapers


class CountyLimits(Strict):
    fha: dict[int, float]
    conforming: dict[int, float]


class LimitLookup(Strict):
    amount: float | None  # None when the loan has no limit
    county_specific: bool  # False when a national minimum stood in for an unknown county


class LoanLimits(Strict):
    """Loan limits by unit count: the national figures plus a table by county."""

    year: int
    sources: dict[str, str]
    conforming_baseline: dict[int, float]
    conforming_ceiling: dict[int, float]
    fha_floor: dict[int, float]
    fha_ceiling: dict[int, float]
    counties: dict[str, CountyLimits] = Field(default_factory=dict)

    def lookup(
        self, kind: LoanLimitKind, unit_count: int, county_fips: str | None = None
    ) -> LimitLookup:
        """Cap on the base loan for a preset's kind of limit.

        ``conforming_baseline`` is the national baseline everywhere, for loans that may not use
        high-balance amounts. ``fha`` and ``conforming_county`` use the county's own limit. When
        the county is unknown or missing from the table, the FHA floor or the conforming
        baseline stands in. Both are the lowest value any county can have, so the result is
        never too generous.
        """
        if kind == "none" or unit_count not in self.conforming_baseline:
            return LimitLookup(amount=None, county_specific=True)
        if kind == "conforming_baseline":
            return LimitLookup(amount=self.conforming_baseline[unit_count], county_specific=True)
        county = self.counties.get(county_fips) if county_fips else None
        if county is None:
            national = self.fha_floor if kind == "fha" else self.conforming_baseline
            return LimitLookup(amount=national[unit_count], county_specific=False)
        table = county.fha if kind == "fha" else county.conforming
        return LimitLookup(amount=table[unit_count], county_specific=True)

    def limit(
        self, kind: LoanLimitKind, unit_count: int, county_fips: str | None = None
    ) -> float | None:
        return self.lookup(kind, unit_count, county_fips).amount


class Environment(BaseSettings):
    """Secrets and paths, read from the process environment and ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    rentcast_api_key: SecretStr | None = None
    hud_api_token: SecretStr | None = None
    census_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    apify_token: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    slack_webhook_url: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    alert_email_to: str | None = None
    alert_email_from: str | None = None  # defaults to SMTP_USERNAME

    dealfinder_config_dir: Path | None = None
    dealfinder_data_dir: Path = Path("data")
    dealfinder_db_url: str = "sqlite:///data/dealfinder.db"
    dealfinder_offline: bool = False  # use cached data only; make no requests

    @property
    def cache_dir(self) -> Path:
        return self.dealfinder_data_dir / "cache"

    def keys_set(self) -> dict[str, bool]:
        """Which credentials are present. Values are never returned."""
        names = (
            "rentcast_api_key",
            "hud_api_token",
            "census_api_key",
            "fred_api_key",
            "anthropic_api_key",
            "apify_token",
            "discord_webhook_url",
            "slack_webhook_url",
            "smtp_password",
        )
        return {name.upper(): bool(getattr(self, name)) for name in names}


class Config(Strict):
    """Everything loaded from the config directory."""

    config_dir: Path
    files: list[Path]
    settings: Settings
    loan_limits: LoanLimits
    insurance_by_state: dict[str, float]
    utility_allowance: dict[int, float]  # bedrooms -> dollars a month the tenant pays
    keywords: dict[str, list[str]]  # flag -> phrases that raise it

    def utility_allowance_for(self, bedrooms: float) -> float:
        """Allowance for a bedroom count; sizes past the table use its largest row."""
        if not self.utility_allowance:
            return 0.0
        beds = max(0, int(bedrooms))
        return self.utility_allowance[min(beds, max(self.utility_allowance))]


def find_config_dir(explicit: Path | str | None = None) -> Path:
    """Where the configuration lives.

    A directory named explicitly or through $DEALFINDER_CONFIG_DIR must hold the settings file;
    pointing somewhere wrong is an error, never a quiet fall back to a different configuration.
    With nothing named, ./config is used, then the project's own config directory.
    """
    named = Path(explicit) if explicit else Environment().dealfinder_config_dir
    if named is not None:
        if (named / SETTINGS_FILE).is_file():
            return named
        raise SettingsError(f"No {SETTINGS_FILE} in {named}")
    candidates = [Path.cwd() / "config", Path(__file__).resolve().parents[2] / "config"]
    for candidate in candidates:
        if (candidate / SETTINGS_FILE).is_file():
            return candidate
    looked = ", ".join(str(candidate) for candidate in candidates)
    raise SettingsError(f"No {SETTINGS_FILE} found. Looked in: {looked}")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise SettingsError(f"{path} is not valid YAML: {error}") from error
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise SettingsError(f"{path} must contain a mapping at the top level")
    return loaded


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into ``base``. Mappings merge key by key; anything else, lists
    included, is replaced whole."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate[T: BaseModel](model: type[T], data: dict[str, Any], origin: str) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise SettingsError(f"{origin} is invalid: {problems}") from error


def _load_insurance(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            row["state"].strip().upper(): float(row["per_unit_year"])
            for row in csv.DictReader(handle)
        }


def _load_utility_allowance(path: Path) -> dict[int, float]:
    if not path.is_file():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {int(row["bedrooms"]): float(row["per_month"]) for row in csv.DictReader(handle)}
    except (KeyError, ValueError) as error:
        raise SettingsError(f"{path} is not a valid utility allowance table: {error}") from error


def _load_keywords(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    data = _read_yaml(path)
    if not all(isinstance(phrases, list) for phrases in data.values()):
        raise SettingsError(f"{path}: each flag needs a list of phrases")
    return {str(flag): [str(phrase) for phrase in phrases] for flag, phrases in data.items()}


def _load_county_limits(path: Path) -> dict[str, CountyLimits]:
    """County FIPS -> limits, from the table built by scripts/build_loan_limits.py."""
    unit_counts = (1, 2, 3, 4)
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                row["county_fips"]: CountyLimits.model_construct(
                    fha={units: float(row[f"fha_{units}"]) for units in unit_counts},
                    conforming={units: float(row[f"conforming_{units}"]) for units in unit_counts},
                )
                for row in csv.DictReader(handle)
            }
    except (KeyError, ValueError) as error:
        raise SettingsError(f"{path} is not a valid county limit table: {error}") from error


def load_config(config_dir: Path | str | None = None) -> Config:
    directory = find_config_dir(config_dir)
    files = [directory / SETTINGS_FILE]
    data = _read_yaml(files[0])
    local = directory / LOCAL_SETTINGS_FILE
    if local.is_file():
        data = deep_merge(data, _read_yaml(local))
        files.append(local)
    settings = _validate(Settings, data, " + ".join(str(f) for f in files))

    for field in ("ranking_scenario", "ranking_scenario_5plus"):
        name = getattr(settings.buy_box, field)
        if name is not None and name not in settings.financing.presets:
            raise SettingsError(f"buy_box.{field} '{name}' is not a financing preset")

    limits_file = directory / "loan_limits" / f"{settings.financing.loan_limit_year}.yaml"
    if not limits_file.is_file():
        raise SettingsError(
            f"No loan limits for {settings.financing.loan_limit_year}: {limits_file}"
        )
    files.append(limits_file)
    loan_limits = _validate(LoanLimits, _read_yaml(limits_file), str(limits_file))
    counties_file = limits_file.with_name(f"{loan_limits.year}_counties.csv")
    if counties_file.is_file():
        loan_limits.counties = _load_county_limits(counties_file)
        files.append(counties_file)
    return Config(
        config_dir=directory,
        files=files,
        settings=settings,
        loan_limits=loan_limits,
        insurance_by_state=_load_insurance(directory / "insurance_by_state.csv"),
        utility_allowance=_load_utility_allowance(directory / "utility_allowance.csv"),
        keywords=_load_keywords(directory / "keywords.yaml"),
    )
