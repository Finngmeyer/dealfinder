"""Tag a listing by the phrases in its description: as-is, cash only, motivated, and so on.

The phrases live in ``config/keywords.yaml`` so they can be changed without touching code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import lru_cache


@lru_cache(maxsize=512)
def _pattern(phrase: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive, with hyphens and spaces interchangeable."""
    parts = [re.escape(part) for part in re.split(r"[-\s]+", phrase.strip().lower()) if part]
    return re.compile(r"(?<![a-z0-9])" + r"[-\s]+".join(parts) + r"(?![a-z0-9])")


def find_flags(text: str, keywords: Mapping[str, Sequence[str]]) -> list[str]:
    """Flags whose phrases appear in ``text``, in the order the keyword file lists them."""
    lowered = text.lower()
    return [
        flag
        for flag, phrases in keywords.items()
        if any(_pattern(phrase).search(lowered) for phrase in phrases)
    ]
