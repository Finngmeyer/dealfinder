"""Stage-2 data sources: FEMA flood zones, the Census geocoder and RentCast rent estimates."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from dealfinder.adapters.rentcast import RENT_URL, parse_rent_estimate
from dealfinder.http import SourceUnavailable
from dealfinder.market import flood, geocode
from dealfinder.store.usage import BudgetExceeded
from factories import rent_estimate
from test_rentcast_sweep import make_source

YEAR = timedelta(days=365)
LAYERS = {
    "layers": [{"id": 27, "name": "Flood Hazard Boundaries"}, {"id": 28, "name": flood.LAYER_NAME}]
}


def zone(code: str, sfha: str, subtype: str = "") -> dict:
    return {"attributes": {"FLD_ZONE": code, "ZONE_SUBTY": subtype, "SFHA_TF": sfha}}


def serve_fema(http_mock, features: list[dict] | None = None, layers: dict | None = None):
    """A stand-in for FEMA's flood map service. Returns the point-query route."""
    http_mock.get(flood.SERVICE, params={"f": "json"}).mock(
        return_value=httpx.Response(200, json=layers or LAYERS)
    )
    return http_mock.get(url__regex=rf"{flood.SERVICE}/\d+/query.*").mock(
        return_value=httpx.Response(200, json={"features": features or []})
    )


def serve_geocoder(http_mock, latitude: float | None = 39.9612, longitude: float = -83.0007):
    matches = (
        []
        if latitude is None
        else [
            {
                "coordinates": {"x": longitude, "y": latitude},
                "matchedAddress": "101 SAMPLE ST, COLUMBUS, OH, 43215",
            }
        ]
    )
    return http_mock.get(geocode.URL).mock(
        return_value=httpx.Response(200, json={"result": {"addressMatches": matches}})
    )


class TestFloodZones:
    def lookup(self, fetcher):
        return flood.flood_zone(fetcher, latitude=39.9612, longitude=-83.0007, max_age=YEAR)

    def test_outside_the_flood_zone(self, fetcher, http_mock):
        route = serve_fema(http_mock, [zone("X", "F", "AREA OF MINIMAL FLOOD HAZARD")])
        found = self.lookup(fetcher)
        assert (found.zone, found.in_flood_zone) == ("X", False)
        assert found.describe() == (
            "FEMA flood map: zone X (area of minimal flood hazard), outside the flood zone"
        )
        request = route.calls[0].request
        assert "/28/query" in request.url.path  # the layer was found by name
        assert request.url.params["geometry"] == "-83.00070,39.96120"
        assert request.url.params["outFields"] == "FLD_ZONE,ZONE_SUBTY,SFHA_TF"

    def test_in_a_special_flood_hazard_area(self, fetcher, http_mock):
        serve_fema(http_mock, [zone("X", "F"), zone("AE", "T", "FLOODWAY")])  # the riskier wins
        found = self.lookup(fetcher)
        assert (found.zone, found.in_flood_zone) == ("AE", True)
        assert "a Special Flood Hazard Area" in found.describe()

    def test_an_unmapped_spot_is_unknown_not_safe(self, fetcher, http_mock):
        serve_fema(http_mock, [])
        found = self.lookup(fetcher)
        assert found.zone is None and found.in_flood_zone is None
        assert found.describe() == "FEMA has no flood map for this spot"

    def test_the_layer_number_is_looked_up_not_assumed(self, fetcher, http_mock):
        route = serve_fema(
            http_mock, [zone("X", "F")], layers={"layers": [{"id": 31, "name": flood.LAYER_NAME}]}
        )
        self.lookup(fetcher)
        assert "/31/query" in route.calls[0].request.url.path

    def test_answers_are_cached(self, fetcher, http_mock):
        route = serve_fema(http_mock, [zone("X", "F")])
        self.lookup(fetcher)
        self.lookup(fetcher)
        assert route.call_count == 1

    def test_no_coordinates(self, fetcher):
        with pytest.raises(SourceUnavailable, match="no coordinates"):
            flood.flood_zone(fetcher, latitude=None, longitude=-83.0, max_age=YEAR)

    def test_a_missing_layer(self, fetcher, http_mock):
        serve_fema(http_mock, layers={"layers": [{"id": 1, "name": "LOMRs"}]})
        with pytest.raises(SourceUnavailable, match="no 'Flood Hazard Zones' layer"):
            self.lookup(fetcher)

    def test_an_error_inside_a_200(self, fetcher, http_mock):
        http_mock.get(flood.SERVICE, params={"f": "json"}).mock(
            return_value=httpx.Response(200, json=LAYERS)
        )
        http_mock.get(url__regex=rf"{flood.SERVICE}/\d+/query.*").mock(
            return_value=httpx.Response(200, json={"error": {"code": 400, "message": "Bad query"}})
        )
        with pytest.raises(SourceUnavailable, match="reported an error: Bad query"):
            self.lookup(fetcher)

    @pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"features": "none"}'])
    def test_parse_rejects_other_shapes(self, body):
        with pytest.raises(SourceUnavailable):
            flood.parse_zone(body)


class TestGeocoder:
    ADDRESS = {"street": "101 Sample St", "city": "Columbus", "state": "OH", "zip_code": "43215"}

    def test_found(self, fetcher, http_mock):
        route = serve_geocoder(http_mock)
        found = geocode.geocode(fetcher, **self.ADDRESS)
        assert (found.latitude, found.longitude) == (39.9612, -83.0007)
        params = route.calls[0].request.url.params
        assert params["street"] == "101 Sample St" and params["zip"] == "43215"
        assert params["benchmark"] == "Public_AR_Current" and params["format"] == "json"

    def test_not_found_is_an_answer_and_is_cached(self, fetcher, http_mock):
        route = serve_geocoder(http_mock, latitude=None)
        assert geocode.geocode(fetcher, **self.ADDRESS) is None
        assert geocode.geocode(fetcher, **self.ADDRESS) is None
        assert route.call_count == 1

    @pytest.mark.parametrize(
        "address",
        [
            {"street": None, "city": "Columbus", "state": "OH", "zip_code": "43215"},
            {"street": "101 Sample St", "city": "Columbus", "state": None, "zip_code": None},
        ],
    )
    def test_an_incomplete_address_is_not_sent(self, fetcher, address):
        with pytest.raises(SourceUnavailable, match="incomplete"):
            geocode.geocode(fetcher, **address)

    @pytest.mark.parametrize("body", [b"<html>", b"{}", b'{"result": {"addressMatches": 3}}'])
    def test_parse_rejects_other_shapes(self, body):
        with pytest.raises(SourceUnavailable):
            geocode.parse_match(body)

    def test_a_match_without_coordinates_is_skipped(self):
        body = json.dumps({"result": {"addressMatches": [{"matchedAddress": "x"}]}}).encode()
        assert geocode.parse_match(body) is None


class TestRentEstimate:
    ASK = {
        "address": "101 Sample St, Columbus, OH, 43215",
        "kind": "fourplex",
        "bedrooms": 2.0,
        "bathrooms": 1.0,
        "sqft": 900.0,
        "comp_count": 10,
        "cache_days": 60,
    }

    def serve(self, http_mock, **changes):
        return http_mock.get(RENT_URL).mock(
            return_value=httpx.Response(200, json=rent_estimate(**changes))
        )

    def test_one_unit_is_priced_with_its_own_layout(self, engine, config, fetcher, http_mock):
        route = self.serve(http_mock)
        source, guard = make_source(engine, config, fetcher)
        estimate = source.rent_estimate(**self.ASK)
        assert (estimate.rent, estimate.low, estimate.high) == (1400.0, 1300.0, 1500.0)
        assert len(estimate.comps) == 3 and estimate.comps[0].distance_miles == 0.3
        assert estimate.describe() == (
            "RentCast rent estimate from 3 comparable rentals (range $1,300 to $1,500)"
        )
        params = dict(route.calls[0].request.url.params)
        assert params == {
            "address": "101 Sample St, Columbus, OH, 43215",
            "propertyType": "Multi-Family",
            "bedrooms": "2",
            "bathrooms": "1",
            "squareFootage": "900",
            "compCount": "10",
            # Without this RentCast fills gaps with the whole building's totals.
            "lookupSubjectAttributes": "false",
        }
        assert guard.used() == 1 and source.requests_billed == 1

    def test_cached_estimates_are_free(self, engine, config, fetcher, http_mock):
        route = self.serve(http_mock)
        source, guard = make_source(engine, config, fetcher)
        source.rent_estimate(**self.ASK)
        source.rent_estimate(**self.ASK)
        assert route.call_count == 1 and guard.used() == 1

    def test_unknown_baths_and_size_are_left_out(self, engine, config, fetcher, http_mock):
        route = self.serve(http_mock)
        source, _ = make_source(engine, config, fetcher)
        source.rent_estimate(**{**self.ASK, "bathrooms": None, "sqft": None, "bedrooms": 0.0})
        params = route.calls[0].request.url.params
        assert "bathrooms" not in params and "squareFootage" not in params
        assert params["bedrooms"] == "0"  # a studio

    def test_a_five_plus_building_is_an_apartment(self, engine, config, fetcher, http_mock):
        route = self.serve(http_mock)
        source, _ = make_source(engine, config, fetcher)
        source.rent_estimate(**{**self.ASK, "kind": "multifamily_5plus"})
        assert route.calls[0].request.url.params["propertyType"] == "Apartment"

    def test_the_budget_applies(self, engine, config, fetcher, http_mock):
        route = self.serve(http_mock)
        source, _ = make_source(engine, config, fetcher, cap=0)
        with pytest.raises(BudgetExceeded):
            source.rent_estimate(**self.ASK)
        assert route.call_count == 0

    def test_no_key(self, engine, config, fetcher):
        source, _ = make_source(engine, config, fetcher, key=None)
        with pytest.raises(SourceUnavailable, match="RENTCAST_API_KEY is not set"):
            source.rent_estimate(**self.ASK)

    @pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"rent": null}', b'{"rent": 0}'])
    def test_parse_rejects_what_is_not_an_estimate(self, body):
        with pytest.raises(SourceUnavailable):
            parse_rent_estimate(body)

    def test_parse_without_comps(self):
        estimate = parse_rent_estimate(b'{"rent": 1250}')
        assert estimate.rent == 1250.0 and estimate.comps == [] and estimate.low is None
        assert estimate.describe() == "RentCast rent estimate from 0 comparable rentals"
