"""Each market data source against synthetic responses shaped like the real ones."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from dealfinder.http import SourceUnavailable
from dealfinder.market import acs, fred, hud, nri, zori

FRED_CSV = """observation_date,MORTGAGE30US
2026-10-22,6.72
2026-10-29,6.81
2026-11-05,6.88
2026-11-12,.
"""

HUD_AREA = {
    "data": {
        "county_name": "Sample County, OH",
        "counties_msa": "",
        "town_name": "",
        "metro_status": "0",
        "metro_name": "",
        "area_name": "Sample County, OH",
        "smallarea_status": "0",
        "basicdata": {
            "Efficiency": "700.0",
            "One-Bedroom": "800.0",
            "Two-Bedroom": "1000.0",
            "Three-Bedroom": "1300.0",
            "Four-Bedroom": "1500.0",
            "year": "2027",
        },
    }
}

HUD_SMALL_AREA = {
    "data": {
        "county_name": "",
        "metro_status": "1",
        "metro_name": "Sampleville, OH",
        "area_name": "Sampleville, OH HUD Metro FMR Area",
        "smallarea_status": "1",
        "year": "2027",
        "basicdata": [
            {
                "zip_code": "MSA level",
                "Efficiency": 900,
                "One-Bedroom": 1000,
                "Two-Bedroom": 1200,
                "Three-Bedroom": 1500,
                "Four-Bedroom": 1700,
            },
            {
                "zip_code": "43215",
                "Efficiency": 1100,
                "One-Bedroom": 1250,
                "Two-Bedroom": 1500,
                "Three-Bedroom": 1900,
                "Four-Bedroom": 2200,
            },
        ],
    }
}

ACS_DETAIL = [
    ["NAME", "B25103_001E", "B25077_001E", "B25090_001E", "B25082_001E", "state", "county"],
    ["Franklin County, Ohio", "3600", "250000", "900000000", "60000000000", "39", "049"],
    ["Capped County, Ohio", "10001", "700000", "1500000000", "50000000000", "39", "999"],
    ["Empty County, Ohio", "-666666666", None, "-666666666", "-666666666", "39", "001"],
]
ACS_PROFILE = [
    ["NAME", "DP04_0005E", "state", "county"],
    ["Franklin County, Ohio", "5.4", "39", "049"],
    ["Capped County, Ohio", "-888888888", "39", "999"],
]

ZORI_ZIP_COLUMNS = "RegionID,SizeRank,RegionName,RegionType,StateName,State,City,Metro,CountyName"
ZORI_ZIP = f"""{ZORI_ZIP_COLUMNS},2025-08-31,2025-09-30,2026-08-31,2026-09-30
1,10,43215,zip,OH,OH,Columbus,"Columbus, OH",Franklin County,1400.0,1500.0,1540.0,1560.0
2,20,2134,zip,MA,MA,Boston,"Boston, MA",Suffolk County,,2400.0,2500.0,
3,30,43004,zip,OH,OH,Blacklick,"Columbus, OH",Franklin County,,,,
"""
ZORI_COUNTY_COLUMNS = (
    "RegionID,SizeRank,RegionName,RegionType,StateName,State,Metro,StateCodeFIPS,MunicipalCodeFIPS"
)
ZORI_COUNTY = f"""{ZORI_COUNTY_COLUMNS},2025-09-30,2026-09-30
9,1,Franklin County,county,OH,OH,"Columbus, OH",39,49,1300.0,1339.0
"""


def nri_page(rows):
    return {"features": [{"attributes": row} for row in rows]}


class TestFred:
    def test_keyless_csv_takes_the_latest_usable_week(self, fetcher, http_mock):
        route = http_mock.get(fred.CSV_URL).mock(return_value=httpx.Response(200, text=FRED_CSV))
        rate = fred.mortgage_rate(fetcher)
        assert rate.rate == pytest.approx(0.0688)
        assert rate.as_of == date(2026, 11, 5)  # the last row has no value yet
        assert rate.source == "FRED MORTGAGE30US"
        assert route.calls.last.request.url.params["id"] == "MORTGAGE30US"

    def test_api_is_used_when_a_key_is_set(self, fetcher, http_mock):
        body = {
            "observations": [
                {"date": "2026-11-12", "value": "."},
                {"date": "2026-11-05", "value": "6.88"},
                {"date": "2026-10-29", "value": "6.81"},
            ]
        }
        route = http_mock.get(fred.API_URL).mock(return_value=httpx.Response(200, json=body))
        rate = fred.mortgage_rate(fetcher, api_key="fred-key")
        assert (rate.rate, rate.as_of) == (pytest.approx(0.0688), date(2026, 11, 5))
        assert rate.source == "FRED API MORTGAGE30US"
        assert route.calls.last.request.url.params["api_key"] == "fred-key"

    @pytest.mark.parametrize(
        "text", ["", "observation_date,MORTGAGE30US\n", "<html>blocked</html>"]
    )
    def test_an_unusable_file_is_rejected_and_not_cached(self, fetcher, http_mock, text):
        http_mock.get(fred.CSV_URL).mock(return_value=httpx.Response(200, text=text))
        with pytest.raises(SourceUnavailable, match="no usable rows"):
            fred.mortgage_rate(fetcher)
        assert not list(fetcher.cache_dir.rglob("*.body"))

    def test_bad_api_shape(self, fetcher, http_mock):
        http_mock.get(fred.API_URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
        with pytest.raises(SourceUnavailable, match="expected form"):
            fred.mortgage_rate(fetcher, api_key="k")


class TestHud:
    def call(self, fetcher, **changes):
        arguments = {
            "token": "hud-token",
            "zip_code": "43215",
            "county_fips": "39049",
            "state": "OH",
        }
        arguments.update(changes)
        return hud.fair_market_rents(fetcher, **arguments)

    def test_area_wide_rents(self, fetcher, http_mock):
        route = http_mock.get(f"{hud.BASE_URL}/fmr/data/3904999999").mock(
            return_value=httpx.Response(200, json=HUD_AREA)
        )
        rents = self.call(fetcher)
        assert rents.rents == {0: 700, 1: 800, 2: 1000, 3: 1300, 4: 1500}
        assert rents.fiscal_year == 2027
        assert not rents.zip_level
        assert rents.describe() == "HUD FY2027 area FMR, Sample County, OH"
        assert route.calls.last.request.headers["Authorization"] == "Bearer hud-token"

    def test_small_area_rents_use_the_zip_s_own_row(self, fetcher, http_mock):
        http_mock.get(f"{hud.BASE_URL}/fmr/data/3904999999").mock(
            return_value=httpx.Response(200, json=HUD_SMALL_AREA)
        )
        rents = self.call(fetcher)
        assert rents.zip_level
        assert rents.rents[2] == 1500
        assert "Small Area FMR for the ZIP" in rents.describe()

    def test_a_zip_missing_from_the_list_gets_the_metro_wide_row(self, fetcher, http_mock):
        http_mock.get(f"{hud.BASE_URL}/fmr/data/3904999999").mock(
            return_value=httpx.Response(200, json=HUD_SMALL_AREA)
        )
        rents = self.call(fetcher, zip_code="43999")
        assert not rents.zip_level
        assert rents.rents[2] == 1200

    def test_bedrooms_past_four_follow_hud_s_rule(self):
        rents = hud.parse(json.dumps(HUD_AREA).encode(), "43215")
        assert rents.gross_rent(2, 0.15) == 1000
        assert rents.gross_rent(2.9, 0.15) == 1000  # fractions round down
        assert rents.gross_rent(5, 0.15) == pytest.approx(1500 * 1.15)
        assert rents.gross_rent(6, 0.15) == pytest.approx(1500 * 1.30)

    def test_no_token_is_a_missing_key_not_a_failure(self, fetcher):
        with pytest.raises(SourceUnavailable, match="HUD_API_TOKEN is not set") as caught:
            self.call(fetcher, token=None)
        assert caught.value.missing_key

    def test_unknown_county(self, fetcher):
        with pytest.raises(SourceUnavailable, match="county is unknown"):
            self.call(fetcher, county_fips=None)

    def test_new_england_is_reported_as_unsupported(self, fetcher):
        with pytest.raises(SourceUnavailable, match="by town"):
            self.call(fetcher, state="MA", county_fips="25025", zip_code="02134")

    @pytest.mark.parametrize(
        "body", [{}, {"data": {}}, {"data": {"basicdata": []}}, {"data": {"basicdata": {"x": 1}}}]
    )
    def test_unexpected_shapes_are_rejected(self, fetcher, http_mock, body):
        http_mock.get(f"{hud.BASE_URL}/fmr/data/3904999999").mock(
            return_value=httpx.Response(200, json=body)
        )
        with pytest.raises(SourceUnavailable):
            self.call(fetcher)

    def test_attribution_text_is_exact(self):
        assert hud.ATTRIBUTION == (
            "This product uses the HUD User Data API but is not endorsed or certified by HUD User."
        )


class TestAcs:
    def mock(self, http_mock):
        detail = http_mock.get(f"{acs.BASE_URL}/2024/acs/acs5").mock(
            return_value=httpx.Response(200, json=ACS_DETAIL)
        )
        profile = http_mock.get(f"{acs.BASE_URL}/2024/acs/acs5/profile").mock(
            return_value=httpx.Response(200, json=ACS_PROFILE)
        )
        return detail, profile

    def test_median_tax_rate_and_vacancy(self, fetcher, http_mock):
        detail, _ = self.mock(http_mock)
        housing = acs.county_housing(fetcher, key="census-key", county_fips="39049", year=2024)
        assert housing.effective_tax_rate() == (
            pytest.approx(0.0144),
            "median taxes / median value",
        )
        assert housing.rental_vacancy == pytest.approx(0.054)
        assert housing.vintage == "ACS 2020-2024"
        assert housing.name == "Franklin County, Ohio"
        assert not housing.top_coded
        sent = detail.calls.last.request.url.params
        assert sent["for"] == "county:*" and sent["in"] == "state:39"
        assert sent["key"] == "census-key"

    def test_one_request_per_state_serves_every_county(self, fetcher, http_mock):
        detail, profile = self.mock(http_mock)
        acs.county_housing(fetcher, key="k", county_fips="39049", year=2024)
        acs.county_housing(fetcher, key="k", county_fips="39999", year=2024)
        assert (detail.call_count, profile.call_count) == (1, 1)

    def test_top_coded_county_uses_the_aggregate_ratio(self, fetcher, http_mock):
        self.mock(http_mock)
        housing = acs.county_housing(fetcher, key="k", county_fips="39999", year=2024)
        assert housing.top_coded
        rate, method = housing.effective_tax_rate()
        assert rate == pytest.approx(0.03)
        assert method == "aggregate taxes / aggregate value"
        assert housing.rental_vacancy is None  # the ACS "not available" code

    def test_not_available_codes_become_unknown(self, fetcher, http_mock):
        self.mock(http_mock)
        housing = acs.county_housing(fetcher, key="k", county_fips="39001", year=2024)
        assert housing.median_taxes is None and housing.median_value is None
        assert housing.effective_tax_rate() is None

    def test_missing_key_and_county(self, fetcher):
        with pytest.raises(SourceUnavailable, match="CENSUS_API_KEY is not set") as caught:
            acs.county_housing(fetcher, key=None, county_fips="39049", year=2024)
        assert caught.value.missing_key
        with pytest.raises(SourceUnavailable, match="county is unknown"):
            acs.county_housing(fetcher, key="k", county_fips=None, year=2024)

    def test_county_not_in_the_tables(self, fetcher, http_mock):
        self.mock(http_mock)
        with pytest.raises(SourceUnavailable, match="39555 is not in the 2024"):
            acs.county_housing(fetcher, key="k", county_fips="39555", year=2024)

    def test_an_error_page_is_rejected(self, fetcher, http_mock):
        http_mock.get(f"{acs.BASE_URL}/2024/acs/acs5").mock(
            return_value=httpx.Response(200, text="<html><title>Invalid Key</title></html>")
        )
        with pytest.raises(SourceUnavailable, match="check CENSUS_API_KEY"):
            acs.county_housing(fetcher, key="bad", county_fips="39049", year=2024)

    def test_keyless_redirect_is_reported(self, fetcher, http_mock):
        http_mock.get(f"{acs.BASE_URL}/2024/acs/acs5").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://api.census.gov/data/missing_key.html"}
            )
        )
        with pytest.raises(SourceUnavailable, match="missing_key"):
            acs.county_housing(fetcher, key="k", county_fips="39049", year=2024)


class TestZori:
    def mock(self, http_mock):
        zips = http_mock.get(zori.ZIP_URL).mock(return_value=httpx.Response(200, text=ZORI_ZIP))
        counties = http_mock.get(zori.COUNTY_URL).mock(
            return_value=httpx.Response(200, text=ZORI_COUNTY)
        )
        return zips, counties

    def test_zip_level_and_growth_over_twelve_months(self, fetcher, http_mock):
        _, counties = self.mock(http_mock)
        index = zori.rent_index(fetcher, zip_code="43215", county_fips="39049")
        assert (index.geography, index.level, index.as_of) == ("zip", 1560.0, date(2026, 9, 30))
        assert index.yoy_growth == pytest.approx(1560 / 1500 - 1)
        assert counties.call_count == 0
        assert index.describe() == "Zillow ZORI, zip 43215, Sep 2026"

    def test_latest_month_may_be_blank_and_zips_are_zero_padded(self, fetcher, http_mock):
        self.mock(http_mock)
        index = zori.rent_index(fetcher, zip_code="02134", county_fips=None)
        assert (index.level, index.as_of) == (2500.0, date(2026, 8, 31))
        assert index.yoy_growth is None  # no value for August 2025

    def test_falls_back_to_the_county_file(self, fetcher, http_mock):
        _, counties = self.mock(http_mock)
        index = zori.rent_index(fetcher, zip_code="43999", county_fips="39049")
        assert index.geography == "county"
        assert index.region == "Franklin County, OH"
        assert index.yoy_growth == pytest.approx(0.03)
        assert counties.call_count == 1

    def test_a_zip_with_no_values_falls_back_too(self, fetcher, http_mock):
        self.mock(http_mock)
        assert zori.rent_index(fetcher, zip_code="43004", county_fips="39049").geography == "county"

    def test_nothing_for_the_area(self, fetcher, http_mock):
        self.mock(http_mock)
        with pytest.raises(SourceUnavailable, match="no rent index"):
            zori.rent_index(fetcher, zip_code="43999", county_fips="01001")
        with pytest.raises(SourceUnavailable, match="no rent index"):
            zori.rent_index(fetcher, zip_code="43999", county_fips=None)

    def test_a_non_table_is_rejected(self, fetcher, http_mock):
        http_mock.get(zori.ZIP_URL).mock(return_value=httpx.Response(200, text="<html>nope"))
        with pytest.raises(SourceUnavailable, match="not a ZORI table"):
            zori.rent_index(fetcher, zip_code="43215", county_fips=None)


class TestNri:
    def test_pages_are_read_until_a_short_one(self, fetcher, http_mock, monkeypatch):
        monkeypatch.setattr(nri, "PAGE_SIZE", 2)
        pages = [
            nri_page(
                [
                    {"STCOFIPS": "39049", "RISK_SCORE": 98.1, "RISK_RATNG": "Relatively High"},
                    {"STCOFIPS": "1001", "RISK_SCORE": 40.0, "RISK_RATNG": "Relatively Low"},
                ]
            ),
            nri_page([{"STCOFIPS": "12086", "RISK_SCORE": 99.6, "RISK_RATNG": "Very High"}]),
        ]
        route = http_mock.get(nri.SERVICE_URL).mock(
            side_effect=[httpx.Response(200, json=page) for page in pages]
        )
        risk = nri.county_risk(fetcher, county_fips="12086")
        assert (risk.rating, risk.score) == ("Very High", 99.6)
        assert route.call_count == 2
        assert [call.request.url.params["resultOffset"] for call in route.calls] == ["0", "2"]
        # Codes are zero-padded, and the cached pages answer the next county with no request.
        assert nri.county_risk(fetcher, county_fips="01001").rating == "Relatively Low"
        assert route.call_count == 2

    def test_no_rating(self, fetcher, http_mock):
        http_mock.get(nri.SERVICE_URL).mock(
            return_value=httpx.Response(
                200,
                json=nri_page(
                    [{"STCOFIPS": "39049", "RISK_SCORE": None, "RISK_RATNG": "Insufficient Data"}]
                ),
            )
        )
        with pytest.raises(SourceUnavailable, match="no rating"):
            nri.county_risk(fetcher, county_fips="39049")
        with pytest.raises(SourceUnavailable, match="no rating"):
            nri.county_risk(fetcher, county_fips="01001")
        with pytest.raises(SourceUnavailable, match="county is unknown"):
            nri.county_risk(fetcher, county_fips=None)

    def test_service_error_inside_a_200(self, fetcher, http_mock):
        http_mock.get(nri.SERVICE_URL).mock(
            return_value=httpx.Response(200, json={"error": {"code": 400, "message": "Bad where"}})
        )
        with pytest.raises(SourceUnavailable, match="Bad where"):
            nri.county_risk(fetcher, county_fips="39049")

    def test_a_hand_downloaded_csv_takes_precedence(self, fetcher, tmp_path):
        manual = tmp_path / "nri_counties.csv"
        manual.write_text("STCOFIPS,RISK_SCORE,RISK_RATNG\n39049,12.5,Very Low\n")
        risk = nri.county_risk(fetcher, county_fips="39049", manual_csv=manual)
        assert (risk.rating, risk.score) == ("Very Low", 12.5)  # and no request was made
