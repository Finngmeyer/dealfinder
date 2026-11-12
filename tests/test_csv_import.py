from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from dealfinder.adapters import csv_import
from dealfinder.adapters.csv_import import ColumnMapping, CsvSource, row_to_listing, suggest_mapping
from dealfinder.models import Geo
from dealfinder.pipeline.importer import CsvImportError, import_csv
from dealfinder.settings import load_config
from dealfinder.store import repo
from dealfinder.store.db import session_scope
from dealfinder.store.tables import ListingRow, PriceEventRow, PropertyRow, RunRow
from factories import listing

MLS_HEADERS = [
    "MLS #", "Status", "Address", "City", "State", "Zip Code", "List Price", "# Units",
    "Property Type", "Total Bedrooms", "Total Bathrooms", "Living Area", "Year Built", "Taxes",
    "List Date", "DOM", "Public Remarks", "Listing Agent", "Photo Count",
]  # fmt: skip

# A made-up MLS export: the columns an agent's spreadsheet typically has, with invented rows.
MLS_FILE = (Path(__file__).parent / "fixtures" / "mls_export.csv").read_text(encoding="utf-8")


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "export.csv"
    path.write_text(MLS_FILE, encoding="utf-8")
    return path


@pytest.fixture
def own_config(config_dir: Path):
    """A config whose mapping files live in a temporary folder."""
    return load_config(config_dir)


def listings(engine) -> dict[str, ListingRow]:
    with session_scope(engine) as session:
        return {row.source_listing_id: row for row in session.scalars(select(ListingRow))}


class TestMapping:
    def test_common_mls_headers_are_recognized(self):
        mapping = suggest_mapping(MLS_HEADERS, "my_mls")
        assert mapping.source == "my_mls"
        assert mapping.columns == {
            "source_listing_id": "MLS #",
            "address_line1": "Address",
            "city": "City",
            "state": "State",
            "zip_code": "Zip Code",
            "price": "List Price",
            "unit_count": "# Units",
            "property_type_raw": "Property Type",
            "bedrooms": "Total Bedrooms",
            "bathrooms": "Total Bathrooms",
            "building_sqft": "Living Area",
            "year_built": "Year Built",
            "description": "Public Remarks",
            "tax_annual": "Taxes",
            "listed_date": "List Date",
            "days_on_market": "DOM",
            "status": "Status",
            "agent_name": "Listing Agent",
        }
        assert mapping.unmapped(MLS_HEADERS) == ["Photo Count"]

    def test_a_column_is_used_for_only_one_field(self):
        mapping = suggest_mapping(["Address", "Type", "Price"], "x")
        assert list(mapping.columns.values()).count("Type") == 1

    def test_save_and_load(self, config_dir: Path):
        mapping = suggest_mapping(MLS_HEADERS, "my_mls")
        path = csv_import.save_mapping(config_dir, "my_mls", mapping)
        assert path == config_dir / "csv_mappings" / "my_mls.yaml"
        assert path.read_text().startswith("# Column mapping")
        assert csv_import.load_mapping(config_dir, "my_mls") == mapping
        assert csv_import.load_mapping(config_dir, "never_saved") is None

    @pytest.mark.parametrize("name", ["../escape", "has space", "", "a/b"])
    def test_mapping_names_cannot_leave_the_folder(self, config_dir: Path, name):
        with pytest.raises(ValueError, match="not a usable mapping name"):
            csv_import.mapping_path(config_dir, name)


class TestRows:
    MAPPING = suggest_mapping(MLS_HEADERS, "my_mls")

    def row(self, **cells) -> dict[str, str]:
        base = {header: "" for header in MLS_HEADERS}
        base.update({"Address": "101 Sample St", "City": "Columbus", "State": "OH",
                     "Zip Code": "43215"})  # fmt: skip
        base.update(cells)
        return base

    def test_values_are_cleaned(self):
        listing_ = row_to_listing(
            self.row(**{"MLS #": "A100", "List Price": "$480,000", "Living Area": "3,600",
                        "Taxes": "5,760.50", "List Date": "10/01/2026", "# Units": "4",
                        "DOM": "42", "Public Remarks": "  Fully rented 4-plex  "}),
            self.MAPPING,
        )  # fmt: skip
        assert listing_.source == "csv:my_mls"
        assert listing_.source_listing_id == "A100"
        assert (listing_.price, listing_.building_sqft, listing_.tax_annual) == (
            480_000, 3_600, 5_760.50,
        )  # fmt: skip
        assert listing_.listed_date == date(2026, 10, 1)
        assert (listing_.unit_count, listing_.property_kind, listing_.days_on_market) == (
            4, "fourplex", 42,
        )  # fmt: skip
        assert listing_.description == "Fully rented 4-plex"
        assert listing_.raw["List Price"] == "$480,000"  # the row as it came

    @pytest.mark.parametrize(
        ("zip_text", "expected"), [("2134", "02134"), ("43215-1234", "43215"), ("43215", "43215")]
    )
    def test_zip_codes(self, zip_text, expected):
        assert row_to_listing(self.row(**{"Zip Code": zip_text}), self.MAPPING).zip_code == expected

    @pytest.mark.parametrize(
        ("units", "type_text", "kind"),
        [
            ("4", "Multi-Family", "fourplex"),
            ("2", "", "duplex"),
            ("3", "Residential Income", "triplex"),
            ("12", "Multi-Family", "multifamily_5plus"),
            ("", "Quadplex", "fourplex"),
            ("", "4-Plex", "fourplex"),
            ("", "Triplex", "triplex"),
            ("", "Multi-Family", "multifamily_2_4"),
            ("", "Single Family Residence", "sfr"),
            ("", "Condominium", "condo"),
            ("", "Vacant Land", "land"),
            ("", "", "other"),
        ],
    )
    def test_property_kind_prefers_a_stated_unit_count(self, units, type_text, kind):
        listing_ = row_to_listing(
            self.row(**{"# Units": units, "Property Type": type_text}), self.MAPPING
        )
        assert listing_.property_kind == kind

    @pytest.mark.parametrize(
        ("text", "status"),
        [("Active", "active"), ("", "active"), ("Pending", "pending"),
         ("Active Under Contract", "pending"), ("Closed", "sold"), ("Expired", "off_market"),
         ("Mystery", "unknown")],
    )  # fmt: skip
    def test_status_words(self, text, status):
        assert row_to_listing(self.row(Status=text), self.MAPPING).status == status

    def test_rows_without_an_id_get_a_stable_one_from_the_address(self):
        first = row_to_listing(self.row(), self.MAPPING)
        second = row_to_listing(self.row(Address="101 SAMPLE STREET"), self.MAPPING)
        other = row_to_listing(self.row(Address="102 Sample St"), self.MAPPING)
        assert first.source_listing_id.startswith("addr-")
        assert first.source_listing_id == second.source_listing_id != other.source_listing_id

    def test_unusable_rows_say_why(self):
        with pytest.raises(ValueError, match="no street address"):
            row_to_listing(self.row(Address=""), self.MAPPING)
        with pytest.raises(ValueError, match="no ZIP code or city and state"):
            row_to_listing(self.row(City="", State="", **{"Zip Code": ""}), self.MAPPING)

    def test_defaults_fill_blank_cells(self):
        mapping = ColumnMapping(
            source="wholesaler", columns={"address_line1": "Address", "city": "City"},
            defaults={"state": "oh", "property_type_raw": "fourplex"},
        )  # fmt: skip
        listing_ = row_to_listing({"Address": "9 Elm St", "City": "Dayton"}, mapping)
        assert (listing_.state, listing_.property_kind) == ("OH", "fourplex")

    def test_unparseable_numbers_and_dates_become_unknown(self):
        listing_ = row_to_listing(
            self.row(**{"List Price": "call agent", "List Date": "soon", "# Units": "several"}),
            self.MAPPING,
        )
        assert listing_.price is None and listing_.listed_date is None
        assert listing_.unit_count is None

    def test_csv_source_fits_the_listing_source_interface(self, csv_file):
        source = CsvSource(csv_file, self.MAPPING)
        assert (source.name, source.is_scraper, source.env_vars) == ("csv:my_mls", False, ())
        assert source.estimate_calls(Geo(state="OH"), ["fourplex"]).calls == 0
        rows = list(source.search(Geo(state="OH"), ["fourplex"], None))
        assert source.to_listing(rows[0]).source_listing_id == "A100"


class TestImport:
    def test_a_first_import_guesses_and_saves_the_mapping(self, engine, own_config, csv_file):
        report = import_csv(engine, own_config, csv_file, "my_mls")
        assert report.mapping_created
        assert Path(report.mapping_file).is_file()
        assert (report.rows, report.new_listings, report.new_properties) == (5, 3, 3)
        assert report.skipped == [
            (5, "no street address"),
            (6, "no ZIP code or city and state to place the address"),
        ]
        assert (report.with_description, report.with_unit_count) == (3, 2)
        assert report.unmapped_columns == ["Photo Count"]
        assert "Imported 3 of 5 rows" in report.text()
        assert "row 5 skipped: no street address" in report.text()

        stored = listings(engine)
        assert set(stored) == {"A100", "A101", "A102"}
        assert stored["A100"].description == "Fully rented 4-plex, each unit 2bd/1ba"
        assert (stored["A100"].tax_annual, stored["A100"].unit_count_stated) == (5760.5, 4)
        assert stored["A101"].status == "pending"
        with session_scope(engine) as session:
            kinds = {p.address_line1: p.property_kind for p in session.scalars(select(PropertyRow))}
            assert kinds == {
                "101 Sample St": "fourplex",
                "202 Example Ave, Unit A-B": "duplex",
                "303 Demo Rd": "triplex",
            }
            boston = session.scalars(
                select(PropertyRow).where(PropertyRow.zip_code == "02134")
            ).one()
            assert boston.county_fips == "25025"  # filled in from the ZIP
            run = session.scalars(select(RunRow)).one()
            assert (run.mode, run.status) == ("import", "completed")

    def test_importing_the_same_file_again_adds_nothing(self, engine, own_config, csv_file):
        import_csv(engine, own_config, csv_file, "my_mls")
        again = import_csv(engine, own_config, csv_file, "my_mls")
        assert not again.mapping_created
        assert (again.new_listings, again.updated_listings, again.price_changes) == (0, 3, 0)
        assert len(listings(engine)) == 3

    def test_a_new_export_with_a_price_cut(self, engine, own_config, csv_file, tmp_path):
        import_csv(engine, own_config, csv_file, "my_mls")
        newer = tmp_path / "next_week.csv"
        newer.write_text(MLS_FILE.replace('"$480,000"', '"$459,900"'), encoding="utf-8")
        report = import_csv(engine, own_config, newer, "my_mls")
        assert report.price_changes == 1
        with session_scope(engine) as session:
            change = session.scalars(
                select(PriceEventRow).where(PriceEventRow.event == "price_change")
            ).one()
            assert (change.previous_price, change.price) == (480_000, 459_900)

    def test_a_dry_run_stores_and_saves_nothing(self, engine, own_config, csv_file):
        report = import_csv(engine, own_config, csv_file, "my_mls", dry_run=True)
        assert report.dry_run and report.run_id is None
        assert (report.rows, len(report.skipped)) == (5, 2)
        assert "Would import 3 of 5 rows" in report.text()
        assert listings(engine) == {}
        assert not Path(report.mapping_file).exists()

    def test_a_hand_edited_mapping_is_used_as_saved(self, engine, own_config, csv_file):
        import_csv(engine, own_config, csv_file, "my_mls", dry_run=False)
        path = csv_import.mapping_path(own_config.config_dir, "my_mls")
        data = yaml.safe_load(path.read_text())
        del data["columns"]["description"]  # the user decides remarks should not be imported
        data["source"] = "agent_export"
        path.write_text(yaml.safe_dump(data))

        with session_scope(engine) as session:
            for row in session.scalars(select(ListingRow)):
                session.delete(row)
        report = import_csv(engine, own_config, csv_file, "my_mls")
        assert report.with_description == 0
        assert {row.source for row in listings(engine).values()} == {"csv:agent_export"}

    def test_a_csv_listing_joins_the_property_rentcast_already_found(
        self, engine, own_config, csv_file
    ):
        rule = repo.DedupeRule(30, 0.05)
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(1, price=480_000), rule=rule)
        report = import_csv(engine, own_config, csv_file, "my_mls")
        assert (report.new_listings, report.new_properties) == (3, 2)
        with session_scope(engine) as session:
            place = session.scalars(
                select(PropertyRow).where(PropertyRow.address_key == "101 SAMPLE ST|43215")
            ).one()
            assert {row.source for row in place.listings} == {"rentcast", "csv:my_mls"}
            # RentCast only knew "2 to 4 units"; the MLS export settles it.
            assert (place.property_kind, place.unit_count) == ("fourplex", 4)

    def test_no_address_column(self, engine, own_config, tmp_path):
        path = tmp_path / "odd.csv"
        path.write_text("Parcel,Owner\n123,Somebody\n")
        with pytest.raises(CsvImportError, match="No address column was recognized") as caught:
            import_csv(engine, own_config, path, "odd")
        assert "address_line1: <your column>" in str(caught.value)
        # The guessed mapping is left on disk so it can be completed by hand.
        assert csv_import.mapping_path(own_config.config_dir, "odd").is_file()

    def test_a_mapping_for_a_different_layout_is_reported(
        self, engine, own_config, csv_file, tmp_path
    ):
        import_csv(engine, own_config, csv_file, "my_mls")
        other = tmp_path / "other.csv"
        other.write_text("Address,Zip\n1 Elm St,43215\n")
        with pytest.raises(CsvImportError, match="names columns this file does not have"):
            import_csv(engine, own_config, other, "my_mls")

    def test_missing_or_empty_file(self, engine, own_config, tmp_path):
        with pytest.raises(CsvImportError, match="No such file"):
            import_csv(engine, own_config, tmp_path / "nope.csv", "x")
        empty = tmp_path / "empty.csv"
        empty.write_text("")
        with pytest.raises(CsvImportError, match="no header row"):
            import_csv(engine, own_config, empty, "x")

    def test_bad_mapping_name(self, engine, own_config, csv_file):
        with pytest.raises(CsvImportError, match="not a usable mapping name"):
            import_csv(engine, own_config, csv_file, "../../etc/passwd")
