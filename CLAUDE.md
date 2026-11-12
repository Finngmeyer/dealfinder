# Dealfinder

Local-first Python app that finds small multifamily listings (fourplexes by default) across the
48 contiguous states plus DC, underwrites each one, and ranks them by cash flow.

## Current phase

**Phase 1 is built and waiting for the owner's OK.** Next is Phase 2 (free market data).
Work one phase at a time: each ends with passing tests, a demo command and a commit, then stop
and wait for approval before starting the next.

| Phase | Scope | State |
|---|---|---|
| 0 | Plan | approved |
| 1 | Underwriting engine, financing presets, `analyze` CLI, golden test, no network | built |
| 2 | FRED, HUD FMR/SAFMR, ACS, ZORI, FEMA NRI clients with disk cache; `market --zip` | next |
| 3 | RentCast adapter, SQLite + Alembic, dedupe, budget guard, `--dry-run`, CSV import, inventory probe | |
| 4 | Property-record step for unit counts, text classifier, type toggles, optional LLM extraction | |
| 5 | Stage-2 enrichment, scoring, buy box, Streamlit dashboard | |
| 6 | Scheduler, alerts, exports, optional scraper adapters, README | |

## Commands

```bash
uv sync                                   # install (Python 3.12)
uv run pytest                             # all tests; tests/test_engine.py is the golden test
uv run ruff check . && uv run ruff format .
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215   # one deal, no network
uv run dealfinder settings                # settings in effect, files, which keys are set
uv run python scripts/build_zip_county.py        # rebuild the bundled ZIP-to-county data
uv run python scripts/build_loan_limits.py 2027  # county loan limits for a new year (add 2027.yaml first)
```

Golden deal, scenario A then B:

```bash
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215 --year-built 1978 \
  --tax-rate 1.2 --vacancy 6 --insurance 900 --management manager --scenario conv_investor --rate 7.0
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215 --year-built 1978 \
  --tax-rate 1.2 --vacancy 6 --insurance 900 --scenario fha_house_hack --rate 6.5
```

## Stack

Python 3.12, uv, ruff, pytest. pydantic v2 + pydantic-settings, PyYAML, Typer + rich.
Added in the phase that first needs them: httpx + tenacity + on-disk cache (2), SQLAlchemy 2 +
Alembic on SQLite, pandas (3), Anthropic SDK with model `claude-haiku-5-5` for optional listing
text extraction (4), Streamlit + pydeck + plotly (5), cron or the OS scheduler (6).

## Layout

```
config/settings.yaml            every assumption: buy box, underwriting, presets, stand-ins
config/settings.local.yaml      personal overrides, merged over settings.yaml, git-ignored
config/loan_limits/<year>.yaml  national FHA and conforming limits by unit count
config/loan_limits/<year>_counties.csv  the same by county, built from HUD's files
config/insurance_by_state.csv   placeholder $/unit/year
src/dealfinder/underwrite/      engine: pure functions over frozen pydantic inputs, no I/O
src/dealfinder/analysis.py      builds engine inputs from a request + settings, tracks sources
src/dealfinder/settings.py      loads config; Environment holds secrets and paths
src/dealfinder/geo.py           ZIP to county and state from bundled Census data
src/dealfinder/render.py        terminal output
src/dealfinder/cli.py           Typer app
src/dealfinder/data/            bundled Census reference data (built by scripts/)
tests/                          HTTP is always mocked; fixtures are synthetic
data/                           runtime: database, cache, imports, labeled text (git-ignored)
```

## Conventions

- **Nothing is hard-coded.** Every business number lives in `config/settings.yaml`. Settings
  models carry no defaults for them, so a missing key is an error. Per-deal inputs override
  settings. The owner asked for toggles, not built-in choices.
- **The engine is pure.** `underwrite/` takes frozen inputs and returns frozen results. No file,
  network, clock or settings access. Bump `ENGINE_VERSION` when a formula changes.
- **Conservative by default.** An unknown input takes the pessimistic stand-in from
  `when_unknown` in settings and raises a flag. Never fill a gap with an optimistic guess.
- **Every number has a source.** Inputs are `actual` (from the listing or entered as fact),
  `estimated` (defaults, market data, placeholders) or `override` (the user's own figure).
- **Fractions inside, percent at the edges.** 0.07 is 7% in code and YAML. CLI options take 7.
- **The golden test is the contract.** `tests/test_engine.py` tolerances: $1, 0.01 percentage
  points, 0.001 on ratios, $50 on bisection prices. It pins these definitions: expense ratio is
  OpEx / EGI; FHA monthly MIP is the annual rate x base loan / 12, flat; house-hack repairs and
  reserves use whole-building rent; the moved-out view charges a manager.
- **Secrets** go in `.env` (git-ignored); `.env.example` lists every variable. A missing key
  turns that feature off with a message. Never print or log a key.
- **No real data in git.** No API responses, listing text, MLS remarks or personal buy-box
  values. The 50+ labeled descriptions for Phase 4 stay under `data/`.
- **Sources:** licensed APIs and government data first. No CAPTCHA solving, proxy rotation or
  other anti-bot evasion. Scraper adapters are optional, off by default, labeled in the UI.
- UI footer on every page: "Estimates only. Verify rents, taxes, insurance and financing before
  you offer." HUD data needs: "This product uses the HUD User Data API but is not endorsed or
  certified by HUD User."

## Owner's decisions

- Budget is $0: RentCast free Developer plan, 50 requests a month, overage $0.20 each. The
  budget guard must hard-stop at the cap. Nationwide sweeps do not fit; CSV imports of MLS
  exports are the main listing feed until the budget changes.
- Self-managed by default (`underwriting.management.mode: self`), with a toggle.
- Runs on a laptop for now. Leave room for 5+ unit deals (commercial preset exists).
- The app belongs in its own private repository.
- Approved: one RentCast property-record request per screened listing for unit count and last
  tax bill; listings whose unit count cannot be confirmed go in a separate "unverified" list.
- The owner will export 50 to 100 multifamily listings with remarks for the Phase 4 precision test.
- Not decided yet: scraper adapters (suggested: decide after Phase 3), alert channel
  (suggested: Discord webhook), real insurance quotes and rate quotes (none given).

## Assumptions made where the spec was silent (owner has seen them)

- Stage 1 screens on the buy box's ranking scenario. "Lower of in-place and market rent" is per
  unit; a vacant or unknown unit uses the market estimate.
- Year-1 total return = (cash flow + principal paydown) / cash invested, no appreciation.
- Owner-occupied presets report KPIs, hold, MAO and sensitivity for the moved-out view; the
  owner's own numbers are in `house_hack`. The owner takes the highest-rent unit unless told.
- Loan limits apply to the base loan; over the limit, the gap moves to the down payment.
  `fha` and `conforming_county` use the county's limit, `conv_owner_5` the national baseline
  (no high-balance). A county missing from the table gets the national minimum and a flag when
  it binds. Lender reserves are reported, not counted in cash invested.
- Unknown flood zone is flagged, not charged (`when_unknown.in_flood_zone: false`); unknown
  county hazard applies the 1.5x insurance multiplier. Both are settings.
- Utility allowance and flood premium are config placeholders with no data source.

## Verified facts about the data sources (checked November 2026)

- **RentCast** `GET /v1/listings/sale`: no description, unit count or tax data. State-only
  queries, `price` and `daysOld` ranges (`*:750000`), and `|` multi-values are documented.
  `limit` max 500, `X-Total-Count` with `includeTotalCount=true` (slow: first page only),
  results sorted by `lastSeenDate` descending, 20 requests/second, only HTTP 200 is billed,
  no hard usage cap. Listing `id` is address-based and matches the property-record id.
- RentCast `GET /v1/properties` has `features.unitCount`, `propertyTaxes[year].total`,
  `ownerOccupied`. Fill rate of `unitCount` is unknown: sample 50 before relying on it.
- RentCast `GET /v1/avm/rent/long-term` prices ONE unit for Multi-Family. Always send
  `lookupSubjectAttributes=false` with the unit's own beds, baths and sqft, or it fills
  missing attributes from the whole building.
- RentCast plans: Developer $0 / 50, Foundation $74 / 1,000, Growth $199 / 5,000,
  Scale $449 / 25,000; overage $0.20 / $0.06 / $0.03 / $0.015.
- **Census API** rejects keyless requests: `CENSUS_API_KEY` is required. ACS median taxes paid
  (B25103) is capped at $10,000; consider aggregate taxes / aggregate value (B25090 / B25082)
  where it is capped. Connecticut uses planning-region codes from ACS 2022 on, not the county
  codes in the bundled 2020 ZIP table.
- **FRED** keyless CSV works: `fredgraph.csv?id=MORTGAGE30US` (7.11% on 2026-11-12).
- **HUD** FMR API needs a bearer token; `fmr/data/{entityid}?year=`, entity ids look like
  `3904999999` (county) or `METRO10180M10180`.
- **FEMA NFHL**: find the "Flood Hazard Zones" layer by name in `MapServer?f=json` (currently
  28); point queries return `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`.
- **FEMA NRI**: the old download page redirects and blocks scripts. Use the public ArcGIS
  service `services.arcgis.com/XG15cJAlne2vxtgt/.../National_Risk_Index_Counties/FeatureServer/0`
  (fields `STCOFIPS`, `RISK_SCORE`, `RISK_RATNG`; 3,232 rows; 2,000 per page).
- **ZORI** ZIP file covers about 8,300 ZIPs:
  `files.zillowstatic.com/research/public_csvs/zori/Zip_zori_uc_sfrcondomfr_sm_month.csv`.
- **Loan limits 2026**: national figures from HUD Mortgagee Letter 2025-23 and FHFA. County
  files for both FHA and conforming loans are fixed-width text at
  `apps.hud.gov/pub/chums/cy<year>-forward-limits.txt` and `cy<year>-gse-limits.txt` (layout:
  `file_layouts.html` there). HUD's conforming file matched FHFA's own list for all 3,235
  counties. FHFA announces the next year's limits in late November.
- **Connecticut** county codes: HUD, FHFA and ACS (2022 on) use planning regions (09110-09190).
  The bundled 2020 ZIP table still returns the old counties (09001-09015), so Connecticut ZIPs
  miss the county limit table and fall back to national minimums. Phase 2's HUD ZIP crosswalk
  should fix this.
