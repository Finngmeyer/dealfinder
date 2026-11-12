"""Regular expressions and small parsers shared by the classifier.

All matching runs on a normalized copy of the description that has the same length as the
original, so a match's position can be used to quote the original wording.
"""

from __future__ import annotations

import re

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
ORDINALS = {"second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}
NUM = r"(?:\d{1,3}|" + "|".join(NUMBER_WORDS) + r")"

# Words that name a building by its unit count.
PLEX_WORDS = {
    "duplex": 2,
    "du-plex": 2,
    "triplex": 3,
    "tri-plex": 3,
    "tri plex": 3,
    "triple decker": 3,
    "triple-decker": 3,
    "three decker": 3,
    "three-decker": 3,
    "fourplex": 4,
    "four-plex": 4,
    "four plex": 4,
    "quadplex": 4,
    "quad-plex": 4,
    "quadruplex": 4,
}
# "sixplex", "five-plex", "eight plex"
PLEX_WORDS.update(
    {
        f"{word}{joiner}plex": count
        for word, count in NUMBER_WORDS.items()
        if count >= 2
        for joiner in ("", "-", " ")
    }
)
_PLEX_SINGULAR = "|".join(re.escape(word) for word in sorted(PLEX_WORDS, key=len, reverse=True))
_PLEX_PLURAL = "|".join(
    re.escape(word) + "(?:es|s)" for word in sorted(PLEX_WORDS, key=len, reverse=True)
)

# Bedrooms and bathrooms of one unit: "2bd/1ba", "2 bed 1 bath", "2br, 1.5 bath", "2/1".
BED = r"(?:bd|br|bdr|bdrm|beds?|bedrooms?)"
BATH = r"(?:ba|bth|baths?|bathrooms?)"
# A bedroom count ("2 bd", "2-bedroom") or the word studio, which needs no "bed" after it.
BEDS = rf"(?P<beds>\d(?=\s*-?\s*{BED}\b)|studio)(?:\s*-?\s*{BED}\b)?"
_JOIN = r"[\s,/&+-]*(?:(?:and|with)\s+)?"
BED_BATH = re.compile(rf"{BEDS}{_JOIN}(?P<baths>\d(?:\.5)?)\s*-?\s*{BATH}\b")
SLASH_MIX = re.compile(r"(?<![\d/$.])(?P<beds>[0-5])\s*/\s*(?P<baths>[1-4](?:\.5)?)(?![\d/])")

MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.\d{2})?"


def normalize(text: str) -> str:
    """Lower-case with typographic dashes and quotes made plain. Same length as the input."""
    table = str.maketrans(
        {
            "\u2013": "-",  # en dash
            "\u2014": "-",  # em dash
            "\u2018": "'",  # curly single quotes
            "\u2019": "'",
            "\u201c": '"',  # curly double quotes
            "\u201d": '"',
        }
    )
    return text.translate(table).lower()


def to_int(token: str) -> int:
    token = token.strip().lower()
    return NUMBER_WORDS[token] if token in NUMBER_WORDS else int(token)


def money_to_float(text: str) -> float:
    return float(text.replace(",", ""))


def mask(text: str, start: int, end: int) -> str:
    """Blank out a span, keeping the length, so later patterns do not see it."""
    return text[:start] + " " * (end - start) + text[end:]


# ---- things that only sound like a unit count ---------------------------------------------------

_UNITS_OR_PLEX = rf"(?:{NUM}\s*-?\s*units?|{NUM}\s*-?\s*plex|{_PLEX_SINGULAR}|{_PLEX_PLURAL})"
HYPOTHETICAL = re.compile(
    r"(?:"
    r"zoned\s+(?:for|as)|zoning\s+(?:allows|permits)(?:\s+for)?|"
    r"approved\s+(?:for|plans\s+for)|plans\s+(?:for|to\s+build|approved\s+for)|"
    r"permitted\s+for|potential\s+(?:for|to\s+(?:add|build|convert\s+(?:in)?to|be))|"
    r"possible|could\s+be(?:\s+(?:converted\s+(?:back\s+)?(?:in)?to|used\s+as|made\s+into))?|"
    r"can\s+be\s+(?:converted\s+(?:back\s+)?(?:in)?to|used\s+as)|"
    r"convert(?:ible)?\s+(?:back\s+)?(?:in)?to|conversion\s+to|"
    r"opportunity\s+to\s+(?:add|build|convert\s+(?:in)?to)|room\s+(?:for|to\s+add)|"
    r"(?:perfect|ideal|great|suitable|suited)\s+for|"
    r"(?:is\s+)?not|isn't|"
    r"former(?:ly)?|previously|once|originally|was|used\s+to\s+be|"
    r"adjacent|adjoining|neighbou?ring|next[-\s]door|nearby|"
    r"build|add"
    r")\s+(?:up\s+to\s+)?(?:an?\s+|another\s+|additional\s+|the\s+)?"
    rf"(?:{_UNITS_OR_PLEX}|(?:{'|'.join(ORDINALS)}|extra|additional)\s+units?)"
    rf"(?:\s*(?:or|and|/)\s*(?:an?\s+)?{_UNITS_OR_PLEX})*"
)

# "fourplex-zoned lot", "4 units possible", "duplex/triplex/fourplex allowed", "fourplex-style"
HYPOTHETICAL_AFTER = re.compile(
    rf"{_UNITS_OR_PLEX}(?:\s*(?:/|or)\s*{_UNITS_OR_PLEX})*"
    r"(?:[-\s]+(?:zoned|zoning|lot|site|style|alternative|potential)\b"
    r"|\s+(?:is\s+|are\s+)?(?:possible|allowed|permitted)\b)"
)

# ---- the building's total, said outright ---------------------------------------------------

_BUILDING = (
    r"(?:apartment\s+|multi-?family\s+|residential\s+|investment\s+|income\s+|rental\s+|brick\s+)*"
    r"(?:complex|building|community|property|portfolio|apartments?|development|multi-?family)"
)
TOTAL_PATTERNS = (
    re.compile(rf"(?P<n>{NUM})\s*-?\s*units?\s+{_BUILDING}"),
    re.compile(rf"(?P<n>{NUM})\s+units?\s+(?:in\s+)?(?:total|altogether|in\s+all)"),
    re.compile(rf"total\s+of\s+(?P<n>{NUM})\s+units?"),
    re.compile(rf"(?P<n>{NUM})\s+total\s+units?"),
    re.compile(rf"all\s+(?P<n>{NUM})\s+units?"),
    re.compile(rf"{NUM}\s+of\s+(?:the\s+)?(?P<n>{NUM})\s+units?"),
)

# "4 units available", "2 units vacant": some of the building, not all of it.
SUBSET = re.compile(
    rf"{NUM}\s+units?\s+(?:are\s+|is\s+|currently\s+)*"
    r"(?:available|vacant|remaining|left|for\s+(?:sale|rent|lease)|rented|occupied|leased|"
    r"updated|renovated|remodeled|have\s+been|has\s+been)"
)

PLEX_MULTIPLE = re.compile(
    rf"(?P<n>{NUM}|pair\s+of|couple\s+of)\s+(?:separate\s+|adjacent\s+|identical\s+|"
    rf"side-by-side\s+)*(?P<plex>{_PLEX_PLURAL}|\d+\s*-?\s*plexes)"
)
PLEX_SINGLE = re.compile(rf"(?<![a-z])(?P<plex>{_PLEX_SINGULAR})(?![a-z])")
PLEX_NUMERIC = re.compile(r"(?<![\d$.])(?P<n>\d{1,2})\s*-?\s*plex(?![a-z])")
FAMILY = re.compile(rf"(?<![\d$.])(?P<n>{NUM})\s*-?\s*(?:family|flat)\b(?!\s+room)")
UNITS_PLAIN = re.compile(
    rf"(?<![\d$.,/-])(?P<n>{NUM})\s*-?\s*(?:units?|apartments|apts)\b(?!\s+(?:complex|community))"
)

METERS = re.compile(
    rf"(?P<n>{NUM})\s+(?:separate\s+|individual\s+|electric\s+|electrical\s+|gas\s+|water\s+|"
    r"utility\s+)*meters?\b"
)

# "each unit 2bd/1ba", "all units are 2/1"
EACH_UNIT = re.compile(r"\b(?:each|every|both)\b|\ball\s+(?:of\s+the\s+)?(?:\w+\s+)?units?\b")
# "four 2 bed 1 bath units", "two 2br/1ba units"
COUNTED_UNITS = re.compile(
    rf"(?P<n>{NUM})\s+(?:identical\s+|spacious\s+|large\s+|updated\s+|renovated\s+)*"
    rf"{BEDS}{_JOIN}(?:(?P<baths>\d(?:\.5)?)\s*-?\s*{BATH}\b)?"
    r"\s*(?:units?|apartments?|apts?|flats?)\b"
)

# "two units are 2/1", "2 units: 1bd/1ba"
COUNTED_UNITS_AFTER = re.compile(
    rf"(?P<n>{NUM})\s+units?\s*(?:are|is|have|has|with|each|:|-)?\s*"
    rf"(?:{BEDS}{_JOIN}(?P<baths>\d(?:\.5)?)\s*-?\s*{BATH}\b"
    r"|(?P<sbeds>[0-5])\s*/\s*(?P<sbaths>[1-4](?:\.5)?)(?![\d/]))"
)

UNIT_LABEL = re.compile(r"\bunits?\s*#?\s*(?P<label>\d{1,2}|[a-h])\b(?!\s*(?:bed|br|bd|bath|ba)\b)")
UNIT_RANGE = re.compile(
    r"\bunits?\s*#?\s*(?P<first>\d{1,2})\s*(?:-|to|through|thru|&|and)\s*#?(?P<last>\d{1,2})\b"
)

LAND_START = re.compile(
    r"^\W*(?:(?:vacant|buildable|residential|commercial|large|level|cleared)\s+)*"
    r"(?:lot|land|parcel|acreage|\d+(?:\.\d+)?\s+acres?)\b"
    r"(?!\s+(?:size|dimensions|area|is\s+\d|features|includes))"
)
LAND_ANYWHERE = re.compile(
    r"\b(?:vacant\s+(?:lot|land)|land\s+only|no\s+structures?|buildable\s+lot|"
    r"(?:lot|land)\s+for\s+sale|zoned\s+lot|build\s+your\s+(?:dream|own))\b"
)
