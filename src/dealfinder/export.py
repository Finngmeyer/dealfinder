"""Deals as a spreadsheet: one row per deal, with where the rents came from.

Used by the dashboard's download buttons and the report command. Every sheet ends with the
disclaimer, since a spreadsheet travels without the app around it.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

import pandas as pd

from dealfinder.analysis import DISCLAIMER
from dealfinder.pipeline.report import DealRow

# Column heading -> how to get it from a deal row.
COLUMNS = {
    "Rank": None,  # filled in by position
    "Grade": lambda row: row.grade,
    "Score": lambda row: row.score,
    "Cash flow per door per month": lambda row: row.cash_flow_door_month,
    "Cash-on-cash return": lambda row: row.coc,
    "DSCR": lambda row: row.dscr,
    "Cap rate": lambda row: row.cap_rate,
    "Price": lambda row: row.price,
    "Price per door": lambda row: row.price_per_door,
    "Maximum allowable offer": lambda row: row.mao,
    "Units": lambda row: row.units,
    "Unit count confidence": lambda row: row.unit_confidence,
    "Property type": lambda row: row.kind.replace("_", " "),
    "Rents": lambda row: rents_note(row),
    "In buy box": lambda row: "yes" if row.in_buy_box else "no",
    "Outside the buy box because": lambda row: "; ".join(row.buy_box_misses),
    "Stage": lambda row: row.stage,
    "Address": lambda row: row.address,
    "City": lambda row: row.city,
    "State": lambda row: row.state,
    "ZIP": lambda row: row.zip_code,
    "Year built": lambda row: row.year_built,
    "Days on market": lambda row: row.days_on_market,
    "In a flood zone": lambda row: {True: "yes", False: "no", None: "not checked"}[
        row.in_flood_zone
    ],
    "Warnings": lambda row: row.flags,
    "Loan scenario": lambda row: row.scenario,
    "Source": lambda row: row.source,
    "Link": lambda row: row.url,
}


def rents_note(row: DealRow) -> str:
    """Whether the cash flow rests on rents the listing states or on estimates."""
    total = row.rents_stated + row.rents_estimated
    if total and row.rents_stated == total:
        return "actual: stated in the listing"
    if row.rents_stated:
        return f"{row.rents_stated} of {total} stated in the listing, the rest estimated"
    return "estimated"


def deals_frame(rows: Sequence[DealRow]) -> pd.DataFrame:
    records = []
    for rank, row in enumerate(rows, start=1):
        record = {"Rank": rank}
        for heading, getter in COLUMNS.items():
            if getter is not None:
                record[heading] = getter(row)
        records.append(record)
    return pd.DataFrame(records, columns=list(COLUMNS))


def to_csv(rows: Sequence[DealRow]) -> bytes:
    """CSV with a byte-order mark, so Excel opens it as UTF-8, and the disclaimer last."""
    text = deals_frame(rows).to_csv(index=False, lineterminator="\n")
    return ("﻿" + text + f'"{DISCLAIMER}"\n').encode("utf-8")


def to_xlsx(rows: Sequence[DealRow]) -> bytes:
    frame = deals_frame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Deals", index=False)
        sheet = writer.sheets["Deals"]
        formats = {
            "Cash flow per door per month": '"$"#,##0;-"$"#,##0',
            "Price": '"$"#,##0',
            "Price per door": '"$"#,##0',
            "Maximum allowable offer": '"$"#,##0',
            "Cash-on-cash return": "0.0%",
            "Cap rate": "0.0%",
            "DSCR": "0.00",
            "Score": "0",
        }
        for index, heading in enumerate(frame.columns, start=1):
            letter = sheet.cell(row=1, column=index).column_letter
            longest = max([len(heading), *(len(str(v)) for v in frame[heading].head(200))])
            sheet.column_dimensions[letter].width = min(48, max(8, longest + 2))
            if heading in formats:
                for cell in sheet[letter][1:]:
                    cell.number_format = formats[heading]
        sheet.freeze_panes = "A2"
        sheet.cell(row=len(frame) + 3, column=1, value=DISCLAIMER)
    return buffer.getvalue()
