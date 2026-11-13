# Dealfinder

Finds small multifamily listings for sale (fourplexes by default) across the 48 contiguous
states and DC, underwrites every one, and ranks them by cash flow. It runs on your own
computer, keeps everything in one SQLite file, and shows where every number came from.

> Estimates only. Verify rents, taxes, insurance and financing before you offer.

## Try it in five minutes

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```bash
uv sync                                   # install
cp .env.example .env                      # keys go here; every one is optional
uv run dealfinder import-csv examples/sample_mls_export.csv --mapping sample
uv run dealfinder underwrite
uv run dealfinder report --top 20
uv run dealfinder ui                      # the dashboard, at http://localhost:8501
```

The sample file is made up. With no keys at all, a listing can be underwritten only when its
remarks state the rents; add the free HUD token below and every listing with bedroom counts
gets a rent estimate.

One deal by hand, without storing anything:

```bash
uv run dealfinder analyze --price 480000 --rents 1350x4 --zip 43215 --beds 2x4
```

## Keys

Put them in `.env`. A missing key switches that feature off and says so; nothing crashes.
In order of how much each one helps:

| Key | What it adds | Cost | Where to get it |
|---|---|---|---|
| `HUD_API_TOKEN` | Rent estimates by bedroom for every area: without it only listings that state their rents can be screened | Free | huduser.gov, "FMR API" page, create an account and a token |
| `CENSUS_API_KEY` | County tax rate and rental vacancy | Free | api.census.gov/data/key_signup.html |
| `RENTCAST_API_KEY` | Listings nationwide, property records (unit count, tax bill), rent estimates with comparables | Free for 50 requests a month, then paid | app.rentcast.io, API dashboard |
| `DISCORD_WEBHOOK_URL`, `SLACK_WEBHOOK_URL` or the `SMTP_*` settings | Alerts | Free | Discord: Server Settings, Integrations, Webhooks |
| `ANTHROPIC_API_KEY` | Claude reads descriptions the rules cannot settle (off until `classify.llm.enabled: true`) | About $0.30 per 1,000 descriptions | platform.claude.com |
| `FRED_API_KEY` | Nothing new: the mortgage rate works without it | Free | fred.stlouisfed.org |
| `APIFY_TOKEN` | The optional Zillow scraper | Paid per result | apify.com |

Mortgage rates (FRED), the rent index (Zillow Research), county hazard ratings and flood
zones (FEMA) and geocoding (Census) need no key.

## What it costs a month

| Setup | What you get | Monthly cost |
|---|---|---|
| CSV imports and free data | Your agent's MLS exports underwritten, scored, mapped and alerted on | $0 |
| The same, plus RentCast Developer | 50 requests: for example a weekly sweep of one or two states, or about 20 listings confirmed and priced with comparables | $0 |
| RentCast Foundation | 1,000 requests: weekly sweeps of several states plus enrichment of the best listings | $74 |
| RentCast Growth | 5,000 requests: weekly sweeps of most states | $199 |
| RentCast Scale | 25,000 requests: daily sweeps nationwide | $449 |

RentCast bills each request beyond the plan: $0.20 on Developer, $0.06, $0.03 and $0.015 on
the others. Dealfinder stops at `budgets.rentcast.monthly_request_cap` (50 as shipped), so
there is no overage unless you raise the cap. What a request buys:

- a sweep: one request per 500 listings in a state (`sweep --dry-run` shows the count);
- a property record: one request per listing, for the unit count and the last tax bill;
- a rent estimate: one request per unit layout, so four identical units cost one.

Extras, all optional: Claude Haiku 5.5 at $0.10 per million input tokens and $0.50 per
million output tokens (November 2026), which is about $0.30 per 1,000 descriptions at
roughly 700 tokens in and 400 out; the Apify Zillow actor at about $2.30 per 1,000 results
on Apify's free tier. The HomeHarvest scraper costs nothing.

## Day to day

```bash
uv run dealfinder import-csv ~/Downloads/export.csv --mapping my_mls   # an agent's export
uv run dealfinder sweep --mode weekly --states OH --dry-run            # what a sweep would cost
uv run dealfinder sweep --mode weekly --states OH                      # fetch, inside the cap
uv run dealfinder underwrite                                           # screen, enrich, score
uv run dealfinder report --top 20 --buy-box                            # or open the dashboard
uv run dealfinder alerts --send                                        # one digest, never repeated
```

Or all of it at once, which is what the scheduler runs:

```bash
uv run dealfinder run --mode daily        # new listings only
uv run dealfinder run --mode weekly       # every active listing; marks the gone ones off-market
uv run dealfinder schedule                # shows the schedule; --install adds it, --remove takes it out
```

`schedule --install` adds two tagged lines to your crontab (macOS, Linux) or two tasks to
Task Scheduler (Windows). A laptop that is asleep at the set time misses that run and the
next one catches up.

The first import of a new file layout guesses which column is which and saves the guess to
`config/csv_mappings/<name>.yaml`. Check it once; later imports reuse it.

## How a listing becomes a ranked deal

1. **Classify.** The source's own fields and the description decide what the property is and
   how many units it has, with a confidence. "Two fourplexes on one lot" is eight units; "lot
   zoned for a fourplex" is land. A fourplex is four units at medium or high confidence.
   Listings that might be a wanted type but are not confirmed go to their own list.
2. **Stage 1, on everything, free data only.** Each listing is underwritten under the ranking
   loan. It survives if cash flow is at least -$100 a door a month or rent is at least 0.7% of
   price.
3. **Stage 2, on the survivors, best first.** The paid lookups: RentCast property record and
   rent estimates, plus the free flood zone and geocode, and Claude if switched on. Then every
   loan preset is run.
4. **Score, 0 to 100.** Cash flow per door 25, cash-on-cash 20, DSCR 15, cap rate against the
   county 10, price per door against the county 10, seller motivation 10, rent upside 10; less
   5 in a flood zone and 5 in a high-hazard county; times 1.0, 0.85 or 0.7 for how sure the
   unit count is. A is 80 and up, B 65, C 50, D 35.

Unknown inputs take a pessimistic stand-in and raise a flag. Every figure is marked actual
(from the listing, or your own figure) or estimated, with its source. In the dashboard
estimates are in italics and the source shows when you rest the pointer on a number.

## Changing the assumptions

Everything is in `config/settings.yaml`, with a comment on each line: the buy box (property
types, states, price, targets), vacancy, management, repairs by building age, insurance,
closing costs, the loan presets, the stage 1 bars, the score, alerts, the schedule. Put your
own values in `config/settings.local.yaml`, which overrides it and stays out of git, or use
the Settings page of the dashboard, which writes that file for you.

Property types are a list: `fourplex` as shipped; add `duplex`, `triplex`, `sfr`, `condo`,
`townhouse` or `multifamily_5plus`. For one property, the Deal page's "Your figures" tab takes
your own rents, tax bill, insurance, rate quote or offer price and underwrites it again.

## Checking the fourplex detector on real listings

The detector scores 100% on 98 made-up descriptions. The real test needs your data, which
never leaves your computer:

```bash
uv run dealfinder import-csv export.csv --mapping my_mls
uv run dealfinder label-sheet            # writes data/labeled/descriptions.csv
# fill in the "units" column where it is blank: a number, land, or unknown
uv run dealfinder classify-eval data/labeled/descriptions.csv
uv run pytest tests/test_classifier_labeled.py   # needs 50 rows; passes at 95% precision
```

## The optional scrapers

Two listing sources are scrapers, not licensed feeds. Both are off as shipped and labeled as
scrapers wherever their data shows. Read the site's terms of use before switching one on;
that decision is yours.

- **HomeHarvest** reads Realtor.com. `uv sync --extra scrapers`, set
  `sources.homeharvest.enabled: true` and list cities or ZIP codes under
  `scrapers.homeharvest.locations`. It brings descriptions and tax bills.
- **Apify Zillow** runs a paid Apify actor on Zillow search URLs you supply. It brings no
  descriptions, so unit counts stay unconfirmed.

```bash
uv run dealfinder fetch --source homeharvest --dry-run
uv run dealfinder fetch --source homeharvest
```

They make one request at a time with a pause between, never use a proxy, never solve a
CAPTCHA, and stop at the first refusal. If Realtor.com answers 403, wait a day.

## Not yet run against the live services

Built from the published formats and tested against stand-ins, because they need your keys:
HUD rents, the Census ACS, RentCast (listings, property records, rent estimates), Claude, the
alert channels, and both scrapers. FRED, Zillow's rent index, FEMA's hazard ratings and flood
zones and the Census geocoder were run live. The first thing to try with a RentCast key is
`dealfinder sweep --mode weekly --states OH`, then compare a few stored rows with the site.
Two known gaps: HUD rents for New England towns, and HUD's own ZIP crosswalk.

## Development

```bash
uv run pytest                # about 840 tests, none of which touch the network
uv run ruff check . && uv run ruff format .
```

`CLAUDE.md` has the layout, the conventions and what was verified about each data source.

This product uses the HUD User Data API but is not endorsed or certified by HUD User.
