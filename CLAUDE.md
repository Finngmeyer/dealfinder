# Dealfinder

Local-first Python app that finds small multifamily listings (fourplexes by default) across the
48 contiguous states plus DC, underwrites each one, and ranks them by cash flow.

## Current phase

**All six phases are built.** What remains needs the owner: keys to run the paid and keyed
sources live, a real MLS export for the precision test, and a private GitHub repository to
push to (this session cannot create one). Each phase ended with passing tests, a demo command
and a commit. The standing rule was to stop for the owner's OK after each phase; on
November 12 the owner said to keep working through the phases while they were away.

Phase 4's bar of 95% fourplex precision on 50 real labeled descriptions is not met yet, only
because there is no real data: `tests/test_classifier_labeled.py` skips until
`data/labeled/descriptions.csv` exists. The classifier scores 100% on the 98 made-up
descriptions in `tests/fixtures/descriptions_synthetic.csv`. When the owner's MLS export
arrives: `import-csv`, then `label-sheet`, fill in the blank `units` cells, run the tests.

Not yet checked against the live services, because they need the owner's keys: HUD rents
(`HUD_API_TOKEN`), Census ACS (`CENSUS_API_KEY`) and RentCast (`RENTCAST_API_KEY`). All three
are built from the published formats and tested against mocks only. FRED, Zillow and FEMA were
run live. First thing to do with a RentCast key: `dealfinder sweep --mode weekly --states OH`
(a state-only query, which the docs describe but which has not been tried), then check a few
stored rows against `raw_json`.

| Phase | Scope | State |
|---|---|---|
| 0 | Plan | approved |
| 1 | Underwriting engine, financing presets, `analyze` CLI, golden test, no network | built |
| 2 | FRED, HUD FMR/SAFMR, ACS, ZORI, FEMA NRI clients with disk cache; `market --zip` | built; HUD and ACS unverified live |
| 3 | RentCast adapter, SQLite + Alembic, dedupe, budget guard, `--dry-run`, CSV import, inventory probe | built; RentCast unverified live |
| 4 | Property-record step for unit counts, text classifier, type toggles, optional LLM extraction | built; real-data precision test waiting for the owner's export; Claude step unverified live |
| 5 | Two-stage funnel, stage-2 enrichment, scoring, buy box, `report`, Streamlit dashboard, CSV/XLSX export | built; RentCast rent estimates unverified live |
| 6 | Scheduler, alerts, exports, optional scraper adapters, README | built; alert channels and both scrapers unverified live |

## Commands

```bash
uv sync                                   # install (Python 3.12)
uv run pytest                             # all tests; tests/test_engine.py is the golden test
uv run ruff check . && uv run ruff format .
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215 --beds 2x4   # one deal
uv run dealfinder market --zip 43215      # rents, taxes, vacancy, rates, hazard, loan limits
uv run dealfinder settings                # settings in effect, files, which keys are set
uv run dealfinder sweep --mode weekly --states OH --dry-run   # expected requests and cost
uv run dealfinder sweep --mode weekly --states OH             # fetch, within the request cap
uv run dealfinder probe --states OH,IN    # one request per state: count the inventory
uv run dealfinder import-csv examples/sample_mls_export.csv --mapping sample
uv run dealfinder listings                # stored listings of the buy box's property types
uv run dealfinder listings --unverified   # might be a wanted type; unit count not confirmed
uv run dealfinder listings --types all --status all   # everything
uv run dealfinder classify "Unit 4 is vacant; units 1-3 leased at \$1,100" --beds 8   # read one description
uv run dealfinder classify-all            # reclassify what changed (--llm: Claude reads the unsure ones)
uv run dealfinder resolve-units --dry-run # RentCast property records for unconfirmed unit counts
uv run dealfinder label-sheet             # stored descriptions -> data/labeled/descriptions.csv
uv run dealfinder classify-eval data/labeled/descriptions.csv   # score the classifier on labeled text
uv run dealfinder underwrite              # stage 1 on everything, stage 2 on survivors, then scores
uv run dealfinder underwrite --stage1-only   # free data only
uv run dealfinder report --top 20         # ranked by cash flow per door (--buy-box, --unverified, --problems)
uv run dealfinder report --csv deals.csv --xlsx deals.xlsx   # the whole list as a spreadsheet
uv run dealfinder ui                      # the dashboard (Streamlit), http://localhost:8501
uv run dealfinder alerts                  # preview the digest; --send sends it once
uv run dealfinder run --mode daily        # sweep, classify, underwrite, alert (what the scheduler runs)
uv run dealfinder schedule                # show the cron lines / tasks; --install, --remove
uv run dealfinder fetch --source homeharvest --dry-run   # optional scrapers, off by default
uv run dealfinder usage                   # request budget and recent runs
uv run python scripts/build_zip_county.py        # rebuild the bundled ZIP-to-county data
uv run python scripts/build_loan_limits.py 2027  # county loan limits for a new year (add 2027.yaml first)
```

Schema change: edit `store/tables.py`, run `uv run alembic revision --autogenerate -m "..."`,
then in the new file replace `dealfinder.store.tables.UtcDateTime()` with `sa.DateTime()`.
The app upgrades its database on start. Until the first push to the owner's repository there
is only migration 0001, rebuilt with `scripts/regenerate_initial_migration.sh`; after that,
never rebuild it: add migrations.

`analyze` and `market` read market data from `data/cache` and the network. `--offline` uses the
cache only, `analyze --no-market` skips market data, `market --refresh` downloads again.

Golden deal, scenario A then B:

```bash
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215 --year-built 1978 \
  --tax-rate 1.2 --vacancy 6 --insurance 900 --management manager --scenario conv_investor --rate 7.0
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215 --year-built 1978 \
  --tax-rate 1.2 --vacancy 6 --insurance 900 --scenario fha_house_hack --rate 6.5
```

## Stack

Python 3.12, uv, ruff, pytest (HTTP mocked with respx). pydantic v2 + pydantic-settings,
PyYAML, Typer + rich, httpx + tenacity with an on-disk cache, SQLAlchemy 2 + Alembic on SQLite
(nothing SQLite-specific, so Postgres works). Added in the phase that first needs them:
Anthropic SDK with model `claude-haiku-5-5` for optional listing text extraction (4), pandas,
Streamlit + pydeck + plotly (5), cron or the OS scheduler (6).

## Layout

```
config/settings.yaml            every assumption: buy box, underwriting, presets, stand-ins
config/settings.local.yaml      personal overrides, merged over settings.yaml, git-ignored
config/loan_limits/<year>.yaml  national FHA and conforming limits by unit count
config/loan_limits/<year>_counties.csv  the same by county, built from HUD's files
config/insurance_by_state.csv   placeholder $/unit/year
config/utility_allowance.csv    placeholder $/month by bedrooms, netted off HUD rents
src/dealfinder/underwrite/      engine: pure functions over frozen pydantic inputs, no I/O
src/dealfinder/http.py          Fetcher: every request goes through it (cache, retries, pacing)
src/dealfinder/models.py        canonical Listing and the ListingSource interface
src/dealfinder/adapters/        listing sources: rentcast.py, csv_import.py, and the optional
                                scrapers homeharvest.py and apify_zillow.py (off by default)
src/dealfinder/alerts.py        what is due, the digest, Discord / Slack / SMTP, never twice
src/dealfinder/scheduler.py     crontab lines and Task Scheduler commands for `dealfinder run`
src/dealfinder/store/           tables.py, db.py, migrations/, repo.py, dedupe.py, usage.py
src/dealfinder/pipeline/        sweep.py (plan, run, resume, probe), importer.py, classify.py,
                                underwrite.py (the funnel), report.py (read stored results back),
                                ingest.py (store a scraper's results), run.py (everything in order)
src/dealfinder/score.py         deal score 0-100, pure; weights and scales come from settings
src/dealfinder/export.py        deals as CSV and XLSX
src/dealfinder/ui/              Streamlit dashboard: app.py (entry), views.py (five pages),
                                data.py (Streamlit-free reads and actions), theme.py (HTML, colors)
src/dealfinder/store/overrides.py  the user's own figures per property
src/dealfinder/classify/        patterns, classifier, unit_mix, rents, keywords, llm, evaluate
config/keywords.yaml            phrases that tag a listing (as-is, cash only, motivated, ...)
src/dealfinder/market/          one module per free source, plus context.py (all of them for a ZIP);
                                flood.py (FEMA flood zone at a point), geocode.py (Census geocoder)
src/dealfinder/analysis.py      builds engine inputs from a request + market data + settings
src/dealfinder/settings.py      loads config; Environment holds secrets and paths
src/dealfinder/geo.py           ZIP to county and state from bundled Census data
src/dealfinder/render.py        terminal output
src/dealfinder/cli.py           Typer app
src/dealfinder/data/            bundled Census reference data (built by scripts/)
config/csv_mappings/            saved column mappings for CSV imports
examples/sample_mls_export.csv  made-up listings to try the import with
tests/                          HTTP is always mocked; fixtures and factories are synthetic
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
- **All HTTP goes through `Fetcher`.** It caches every response on disk, retries with backoff,
  paces requests per host, falls back to a stale copy when a refresh fails, and never writes a
  key to disk: pass keys as `secret_params` or headers. A source problem raises
  `SourceUnavailable` with a reason a person can act on; nothing else should escape a source.
- **Metered requests pass the budget guard.** `BudgetGuard.check` runs as the fetcher's
  `before_send`, so it fires only for a real request, and `record` runs as `on_success` the
  moment an HTTP 200 arrives, in its own transaction, so a billed response is counted even if
  its body is unusable. Cached pages are free and uncounted. The cap is
  `budgets.rentcast.monthly_request_cap`, counted from `billing_cycle_day`.
- **Sweeps are idempotent and resumable.** One transaction per page stores the listings and
  the checkpoint together. `upsert_listing` only adds a price event when something changed.
  Listings go off-market only after a complete weekly sweep of their state.
- **Dedupe:** normalized address first, then within 30 m and 5% on price. Units are dropped
  from the address key except for condos.
- **The classifier owns kind and unit count.** `classify/` is pure: structured facts and a
  description in, a `Classification` out (kind, unit count, confidence, evidence quoted from
  the listing, tags, units with any stated rents). `pipeline/classify.py` stores it and sets
  `properties.property_kind / unit_count / unit_confidence`; `upsert_listing` only writes
  stand-ins there while `unit_confidence` is null. A fourplex is 4 units at medium or high
  confidence. Anything that might be a wanted type but is not confirmed is "unverified" and
  listed separately (`kind_matches`). Imports and sweeps classify on their own.
- **Classification is skipped when nothing changed.** `classifications.input_hash` covers the
  classifier's inputs, the keyword lists and `CLASSIFIER_VERSION`; bump the version when a
  rule changes so everything is read again. Units are updated in place and keep their market
  rent unless their layout changed.
- **Claude never adds an unquoted fact.** The optional step (`classify.llm`, off by default)
  asks for JSON plus the verbatim words for every number; `checked()` drops any number whose
  quote is not in the description or does not contain it. It fills gaps and never overrules
  a count the rules hold at medium or better. Each description is sent at most once
  (`method` becomes `rules+llm`), capped by `classify.llm.max_calls_per_run`.
- **The funnel.** `run_funnel` underwrites one listing per property (the most recently seen
  active one) for every wanted or possibly-wanted type. Each listing has one ranking loan,
  `Settings.ranking_for(unit_count)`: `buy_box.ranking_scenario`, or
  `buy_box.ranking_scenario_5plus` where that loan does not lend (5+ units). It is stored in
  `screens.ranking_scenario`, and `deal_rows(scenario=None)` returns every listing under its
  own ranking loan. Stage 1: free data, the ranking
  loan only, kept when cash flow per door or rent-to-price clears `funnel.stage1`.
  Stage 2: survivors inside the buy box's states and price, best cash flow first: property
  record, Claude, geocode, flood, a rent estimate per unit layout, then every loan preset.
  Each listing gets a `screens` row (stage, verdict, inputs with sources, flags, what stage 2
  did) and one `underwriting` row per scenario. A listing that cannot be underwritten is
  stored at stage 0 with the reason. Scores are computed last, against county medians of the
  run's own results. Without HUD rents a listing with no stated rent cannot be screened and
  nothing is spent on it: that is deliberate (no blind RentCast spending).
- **Commit before a metered call.** The request counter writes on its own connection, and
  SQLite allows one writer: `enrich` calls `session.commit()` before each RentCast request.
- **Provenance travels with the request.** `AnalyzeRequest.provenance` names the source of
  each figure that came from a listing or a lookup; anything without an entry reads as
  entered by hand. The user's own figures (`overrides`) win over everything and show as
  "your figure".
- **Estimates are italic, actuals upright, sources on hover.** Tables of underwriting figures
  in the dashboard are HTML built by `ui/theme.py` (Streamlit's own table cannot italicise a
  cell or give it a tooltip). Everything from a listing is escaped with `theme.esc` first.
  Grades use one validated blue ramp; status colors always come with a word.
- **Alerts go out once.** `alerts.find_alerts` works out what is due from the stored results;
  only alerts that were actually sent count as "already told", so a failed send is retried
  with the same key and a stale unsent row is dropped. A deal that leaves the buy box is
  marked lapsed, which makes its return news again. Starred price cuts count from
  `properties.starred_at`. A webhook address is a secret: `Fetcher.post_json` never puts a
  URL in an error.
- **Scrapers stop, they do not push.** Off by default, labeled in `source_label` and on the
  Settings page. One request at a time with `scrapers.min_seconds_between_requests` between,
  no proxy argument anywhere, and the first refusal raises `SourceUnavailable` and ends the
  run. `ingest` never marks anything off the market on the strength of a scrape.
- **Tests never touch the network.** An autouse fixture fails any request without a mocked
  route, and `DEALFINDER_OFFLINE=1` is set for every test. Fixtures are synthetic.
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
- Not decided yet: whether to switch a scraper on (both are built, both are off), alert
  channel (all three are built; suggested: Discord webhook), real insurance quotes and rate
  quotes (none given), and the laptop's operating system (the scheduler handles all three).

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
- Score scales (all in `scoring`, so the owner can change them): cash flow earns nothing at
  $0 and full marks at $300; CoC 0% to 12%; DSCR 1.00 to 1.50 (a cash purchase gets full
  marks); cap rate 0 to 2 points above the county median; price per door 0% to 30% below it;
  rent upside 0% to 20%. Seller motivation: up to 4 points for days on market (full at 120),
  up to 4 for the total price cut (full at 10%), 2 for each motivated-seller tag or distress
  listing type, capped at 10. A county needs 5 underwritten listings to have a median;
  otherwise those two parts earn nothing. Unknown hazard counts as high (as it does for
  insurance); an unchecked flood zone does not. The spec gave the weights and the full-marks
  points; the zero points and the motivation split are assumptions the owner has not seen.
- With no county tax rate loaded, a known last tax bill is used as the tax figure (flagged,
  since a sale can trigger reassessment) in place of the 2.5% stand-in.
- A deal is in the buy box when its type is confirmed, its state and price fit, and it meets
  every target under the scenario shown. Unconfirmed unit counts are never in the buy box.
- HUD rents are gross, so the utility allowance comes off unless the owner is known to pay
  utilities. Without bedroom counts no market rent is estimated, since a rent for the wrong
  size would be worse than none. Zillow's index is a cross-check only: rents more than 25%
  above it raise a flag.

## Verified facts about the data sources (checked November 2026)

- **RentCast** `GET /v1/listings/sale`: no description, unit count or tax data. State-only
  queries, `price` and `daysOld` ranges (`*:750000`), and `|` multi-values are documented.
  `limit` max 500, `X-Total-Count` with `includeTotalCount=true` (slow: first page only),
  results sorted by `lastSeenDate` descending, 20 requests/second, only HTTP 200 is billed,
  no hard usage cap. Listing `id` is address-based and matches the property-record id.
- RentCast `GET /v1/properties/{id}` has `features.unitCount`, `propertyTaxes[year].total`,
  `ownerOccupied`; a missing record is a 404 and is not billed. Fill rate of `unitCount` is
  unknown: sample 50 before relying on it. `resolve-units` stores each lookup under
  `_property_record` in the listing's `raw_json` and never repeats it.
- **Anthropic SDK** 2.8 (dev dependency; optional extra `llm` for users) is built on `httpx2`,
  so respx does not see it: tests pass `http_client=httpx2.Client(transport=MockTransport)`.
  The call is `client.messages.create(model, max_tokens, system, messages,
  output_config={"effort": ..., "format": {"type": "json_schema", "schema": ...}})`; read the
  block whose `type` is `text` (a thinking block may come first) and treat any `stop_reason`
  other than `end_turn` as no answer.
- RentCast `GET /v1/avm/rent/long-term` prices ONE unit for Multi-Family. Always send
  `lookupSubjectAttributes=false` with the unit's own beds, baths and sqft, or it fills
  missing attributes from the whole building.
- RentCast plans: Developer $0 / 50, Foundation $74 / 1,000, Growth $199 / 5,000,
  Scale $449 / 25,000; overage $0.20 / $0.06 / $0.03 / $0.015.
- **Census API** rejects keyless data requests (`CENSUS_API_KEY` is required), but variable
  metadata is readable without a key, which is how B25103, B25077, B25090, B25082 and
  DP04_0005E were confirmed. The newest 5-year vintage is 2024; 2025 returned 404. Median taxes
  (B25103) are capped at $10,000, so capped counties use aggregate taxes / aggregate value.
- **FRED** keyless CSV works: `fredgraph.csv?id=MORTGAGE30US` (7.11% on 2026-11-12).
- **HUD** FMR API needs a bearer token; `fmr/data/{entityid}?year=`, entity ids look like
  `3904999999` (county), `METRO10180M10180` (metro) or a 10-digit town code in New England
  (`5002175925`). `basicdata` is one object for an ordinary area and a list with one row per
  ZIP plus a `"zip_code": "MSA level"` row where Small Area FMRs apply. Values may be strings.
  Not built yet: the New England town lookup (the Census ZCTA-to-county-subdivision file gives
  ZIP to town) and HUD's ZIP crosswalk, both waiting for a token to test against.
- **FEMA NFHL** (`hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer`): find the
  "Flood Hazard Zones" layer by name in `MapServer?f=json` (currently 28); point queries
  return `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF` ("T" inside a Special Flood Hazard Area). No
  features means FEMA has no map there. Run live on November 12.
- **Census geocoder** `geocoding.geo.census.gov/geocoder/locations/address` (keyless, params
  `street, city, state, zip, benchmark=Public_AR_Current, format=json`): coordinates are in
  `result.addressMatches[0].coordinates` (`x` longitude, `y` latitude); no match is an empty
  list. Run live on November 12.
- **Streamlit 1.67**: `st.dataframe` shows Styler colors but not italics, and
  `Styler.set_tooltips` breaks the cells, hence the HTML tables. `AppTest.from_function` runs
  a page headlessly (see `tests/test_ui.py`). The default page lives at `/`.
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
- **Connecticut** county codes: HUD, FHFA, FEMA and ACS (2022 on) use planning regions
  (09110-09190). `scripts/build_zip_county.py` maps each Connecticut ZIP to its planning region
  through its main town, using the Census county-subdivision file and Connecticut crosswalk.
- **Zillow** also publishes a county ZORI file (`County_zori_uc_sfrcondomfr_sm_month.csv`,
  FIPS in `StateCodeFIPS` + `MunicipalCodeFIPS`), used when a ZIP has no index of its own.
- **HomeHarvest** 0.8.18 (optional extra `scrapers`; needs pandas < 3, which is why pandas is
  locked to 2.3): `scrape_property(location, listing_type, return_type="pandas",
  property_type, past_days, price_max, limit, extra_property_data, parallel, proxy, ...)`.
  The table has `property_id`, `status`, `text` (the description), `style` (MULTI_FAMILY,
  DUPLEX_TRIPLEX, ...), `list_price`, `tax`, `full_baths`, `half_baths`, `days_on_mls`,
  `fips_code`, coordinates and agent fields; gaps are NaN or NaT. Signature and columns were
  read from the installed package; Realtor.com itself was never contacted.
- **Apify** `maxcopell~zillow-scraper` ("Zillow Search Scraper"): input `searchUrls: [{url}]`
  (the URL must contain `searchQueryState`), `extractionMethod`, `resultsLimit`; items have
  `zpid`, `listingPrice.amount`, `listingAddress`, `coordinates`, `homeType`, `listingType`
  flags, `bedrooms`, `bathrooms`, `livingArea`, `daysOnZillow`, `propertyUrl`, and no
  description. $0.0023 a result on Apify's free tier (April 2026 pricing). Read from Apify's
  public actor API; no run was made. The synchronous run endpoint is
  `POST /v2/acts/{actor}/run-sync-get-dataset-items` with a bearer token.
- **Claude Haiku 5.5** (`claude-haiku-5-5`): $0.10 in, $0.50 out per million tokens for
  prompts up to 100K tokens (price list cached November 3, 2026); adaptive thinking is on by
  default and effort defaults to medium, so the extractor sets `effort: low`.
- This sandbox reaches the network through a proxy with its own CA; httpx honours
  `SSL_CERT_FILE` and `HTTPS_PROXY` from the environment, so no client changes are needed.
