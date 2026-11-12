from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dealfinder.geo import CONTIGUOUS_STATES, lookup_zip, normalize_zip
from dealfinder.settings import Environment, SettingsError, deep_merge, load_config


class TestSettings:
    def test_defaults_load(self, config):
        settings = config.settings
        assert list(settings.financing.presets) == [
            "fha_house_hack",
            "conv_owner_5",
            "conv_investor",
            "dscr",
            "commercial",
            "cash",
        ]
        assert settings.buy_box.ranking_scenario == "conv_investor"
        assert settings.buy_box.max_price == 750_000
        assert settings.buy_box.targets.min_dscr == 1.20
        assert settings.underwriting.management.mode == "self"
        assert settings.sensitivity[0].name == "Rate +1 pt"
        assert len(settings.sensitivity) == 6

    def test_spreads_match_the_four_loan_presets(self, config):
        presets = config.settings.financing.presets
        spreads = [
            presets[name].rate_spread
            for name in ("fha_house_hack", "conv_owner_5", "conv_investor", "dscr")
        ]
        assert spreads == [0.0, 0.0025, 0.0075, 0.0125]

    @pytest.mark.parametrize(
        ("year", "share"),
        [
            (1949, 0.10),
            (1950, 0.08),
            (1979, 0.08),
            (1980, 0.07),
            (1999, 0.07),
            (2000, 0.05),
            (2024, 0.05),
        ],
    )
    def test_repairs_and_reserves_bands(self, config, year, share):
        band = config.settings.underwriting.age_band(year)
        assert band.repairs_pct == share
        assert band.reserves_pct == share

    def test_national_loan_limits(self, config):
        limits = config.loan_limits
        assert limits.year == 2026
        assert limits.limit("fha", 4) == 1_041_125
        assert limits.limit("conforming_baseline", 4) == 1_601_750
        assert limits.limit("conforming_county", 2) == 1_066_250
        assert limits.fha_ceiling[4] == 2_402_625
        assert limits.limit("none", 4) is None
        assert limits.limit("fha", 5) is None

    def test_county_loan_limits(self, config):
        limits = config.loan_limits
        assert len(limits.counties) == 3_235
        franklin_oh, los_angeles, new_york = "39049", "06037", "36061"
        # Columbus sits between the FHA floor and ceiling but at the conforming baseline.
        assert limits.limit("fha", 4, franklin_oh) == 1_136_750
        assert limits.limit("fha", 1, franklin_oh) == 591_100
        assert limits.limit("conforming_county", 4, franklin_oh) == 1_601_750
        # Los Angeles is at the ceiling for both.
        assert limits.limit("fha", 4, los_angeles) == 2_402_625
        assert limits.limit("conforming_county", 4, los_angeles) == 2_402_625
        # Manhattan's conforming limit stayed at its 2025 level while FHA moved to the ceiling.
        assert limits.limit("conforming_county", 1, new_york) == 1_209_750
        assert limits.limit("fha", 1, new_york) == 1_249_125
        # Loans barred from high-balance amounts get the national baseline in every county.
        assert limits.limit("conforming_baseline", 4, los_angeles) == 1_601_750

    def test_unknown_county_gets_the_national_minimum_and_says_so(self, config):
        limits = config.loan_limits
        for county in (None, "99999", "09001"):  # 09001 is a pre-2022 Connecticut county code
            fha = limits.lookup("fha", 4, county)
            assert (fha.amount, fha.county_specific) == (1_041_125, False)
            conforming = limits.lookup("conforming_county", 4, county)
            assert (conforming.amount, conforming.county_specific) == (1_601_750, False)
        assert limits.lookup("fha", 4, "39049").county_specific
        assert limits.lookup("conforming_baseline", 4, None).county_specific
        assert limits.lookup("none", 4, None).amount is None

    def test_county_table_is_optional(self, config_dir: Path):
        (config_dir / "loan_limits" / "2026_counties.csv").unlink()
        loaded = load_config(config_dir)
        assert loaded.loan_limits.counties == {}
        assert loaded.loan_limits.limit("fha", 4, "39049") == 1_041_125

    def test_broken_county_table_is_reported(self, config_dir: Path):
        (config_dir / "loan_limits" / "2026_counties.csv").write_text("county_fips,fha_1\n1,x\n")
        with pytest.raises(SettingsError, match="county limit table"):
            load_config(config_dir)

    def test_insurance_table_covers_the_48_states_and_dc(self, config):
        assert set(config.insurance_by_state) == set(CONTIGUOUS_STATES)
        assert len(config.insurance_by_state) == 49

    def test_local_file_overrides_only_what_it_names(self, config_dir: Path):
        (config_dir / "settings.local.yaml").write_text(
            yaml.safe_dump(
                {
                    "buy_box": {"max_price": 500_000, "targets": {"min_dscr": None}},
                    "underwriting": {"management": {"mode": "manager"}},
                    "financing": {"presets": {"dscr": {"enabled": False}}},
                    "sensitivity": [{"name": "Rents -20%", "rent_factor": 0.8}],
                }
            ),
            encoding="utf-8",
        )
        loaded = load_config(config_dir)
        settings = loaded.settings
        assert settings.buy_box.max_price == 500_000
        assert settings.buy_box.targets.min_dscr is None
        assert settings.buy_box.targets.min_coc == 0.06  # untouched
        assert settings.underwriting.management.mode == "manager"
        assert settings.underwriting.management.manager_pct == 0.09  # untouched
        assert settings.financing.presets["dscr"].enabled is False
        assert settings.financing.presets["dscr"].rate_spread == 0.0125  # untouched
        assert [shock.name for shock in settings.sensitivity] == ["Rents -20%"]  # lists replace
        assert config_dir / "settings.local.yaml" in loaded.files

    def test_a_typo_is_an_error_that_names_the_key(self, config_dir: Path):
        (config_dir / "settings.local.yaml").write_text("buy_box: {max_prise: 1}\n", "utf-8")
        with pytest.raises(SettingsError, match="max_prise"):
            load_config(config_dir)

    def test_a_missing_value_is_an_error_not_a_hidden_default(self, config_dir: Path):
        path = config_dir / "settings.yaml"
        data = yaml.safe_load(path.read_text("utf-8"))
        del data["underwriting"]["misc_per_unit_year"]
        path.write_text(yaml.safe_dump(data), "utf-8")
        with pytest.raises(SettingsError, match="misc_per_unit_year"):
            load_config(config_dir)

    def test_ranking_scenario_must_be_a_preset(self, config_dir: Path):
        (config_dir / "settings.local.yaml").write_text(
            "buy_box: {ranking_scenario: hard_money}\n", "utf-8"
        )
        with pytest.raises(SettingsError, match="hard_money"):
            load_config(config_dir)

    def test_missing_loan_limit_year_is_reported(self, config_dir: Path):
        (config_dir / "settings.local.yaml").write_text(
            "financing: {loan_limit_year: 2031}\n", "utf-8"
        )
        with pytest.raises(SettingsError, match="2031"):
            load_config(config_dir)

    def test_a_named_directory_that_is_wrong_is_an_error_not_a_fallback(self, tmp_path: Path):
        with pytest.raises(SettingsError, match="nowhere"):
            load_config(tmp_path / "nowhere")

    def test_environment_variable_names_the_directory(self, config_dir: Path, monkeypatch):
        (config_dir / "settings.local.yaml").write_text("buy_box: {max_price: 123456}\n", "utf-8")
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        assert load_config().settings.buy_box.max_price == 123_456
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir / "missing"))
        with pytest.raises(SettingsError, match="missing"):
            load_config()

    def test_without_a_named_directory_the_project_config_is_found(self):
        # The tests run from an empty temporary directory, so this is the packaged fallback.
        assert load_config().settings.buy_box.max_price == 750_000

    def test_deep_merge(self):
        base = {"a": {"x": 1, "y": 2}, "list": [1, 2], "keep": True}
        merged = deep_merge(base, {"a": {"y": 3}, "list": [9]})
        assert merged == {"a": {"x": 1, "y": 3}, "list": [9], "keep": True}
        assert base["a"]["y"] == 2  # the original is untouched


class TestEnvironment:
    def test_no_keys_is_fine(self):
        assert not any(Environment().keys_set().values())

    def test_reports_presence_without_values(self, monkeypatch):
        monkeypatch.setenv("RENTCAST_API_KEY", "super-secret")
        status = Environment().keys_set()
        assert status["RENTCAST_API_KEY"] is True
        assert status["HUD_API_TOKEN"] is False
        assert "super-secret" not in repr(Environment())


class TestGeo:
    def test_zip_to_county(self):
        place = lookup_zip("43215")
        assert place is not None
        assert (place.state, place.county_fips, place.county_name) == (
            "OH",
            "39049",
            "Franklin County",
        )
        assert place.in_contiguous_us is True
        assert not place.approximate
        assert place.describe() == "43215 Franklin County, OH"

    def test_zip_plus_four_and_spaces(self):
        assert normalize_zip(" 43215-1234 ") == "43215"
        place = lookup_zip("43215-1234")
        assert place is not None and place.county_fips == "39049"

    @pytest.mark.parametrize("text", ["", "4321", "432156", "abcde", "43215-12"])
    def test_not_a_zip(self, text):
        assert lookup_zip(text) is None

    @pytest.mark.parametrize(("zip_code", "state"), [("99501", "AK"), ("96813", "HI")])
    def test_outside_the_contiguous_states(self, zip_code, state):
        place = lookup_zip(zip_code)
        assert place is not None
        assert place.state == state
        assert place.in_contiguous_us is False

    def test_dc_counts_as_covered(self):
        place = lookup_zip("20001")
        assert place is not None and place.state == "DC" and place.in_contiguous_us is True

    def test_unmapped_zip_falls_back_to_the_prefix_state(self):
        # 43216 is a Columbus PO-box ZIP that the Census Bureau does not map.
        place = lookup_zip("43216")
        assert place is not None
        assert place.state == "OH"
        assert place.approximate
        assert place.county_fips is None
        assert "county unknown" in place.describe()

    def test_connecticut_zips_map_to_planning_regions(self):
        hartford = lookup_zip("06103")
        assert hartford is not None
        assert (hartford.county_fips, hartford.county_name) == ("09110", "Capitol Planning Region")
        assert hartford.in_contiguous_us is True

    def test_unknown_prefix(self):
        place = lookup_zip("00000")
        assert place is not None
        assert place.state is None
        assert place.in_contiguous_us is None
