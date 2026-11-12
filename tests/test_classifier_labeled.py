"""The classifier against real listing descriptions the owner has labeled.

The file is not in the repository: real MLS remarks stay on the owner's machine, under the
git-ignored data folder. Make it with ``dealfinder label-sheet`` after importing an MLS export,
fill in the blanks, and this test starts running. The bar is the one in the specification: of
the listings called a fourplex, at least 95% really are, on 50 or more descriptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dealfinder.classify.evaluate import evaluate, load_labeled

LABELED = Path(__file__).resolve().parents[1] / "data" / "labeled" / "descriptions.csv"
MINIMUM_ROWS = 50
MINIMUM_PRECISION = 0.95


@pytest.mark.skipif(
    not LABELED.is_file(),
    reason="no labeled descriptions yet: run `dealfinder label-sheet` after importing an export",
)
def test_fourplex_precision_on_real_descriptions(config):
    rows = load_labeled(LABELED)
    assert len(rows) >= MINIMUM_ROWS, (
        f"{len(rows)} labeled rows in {LABELED}; the test needs at least {MINIMUM_ROWS}"
    )
    report = evaluate(rows, config.keywords)
    assert report.fourplex_called > 0, "the classifier called nothing a fourplex:\n" + report.text()
    assert report.precision >= MINIMUM_PRECISION, report.text()
